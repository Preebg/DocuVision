import requests

from config import (
    EXTRACTION_JSON_SCHEMA,
    FALLBACK_MODEL,
    OLLAMA_INFERENCE_OPTIONS,
    PRIMARY_MODEL,
    get_ollama_config,
)
from observability import get_logger

logger = get_logger(__name__)


class ModelRecoverableError(RuntimeError):
    """Primary vision model failed due to OOM, timeout, or system crash."""


def list_ollama_models():
    config = get_ollama_config()
    try:
        response = requests.get(f"{config['url']}/api/tags", timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Could not reach Ollama at {config['url']}. "
            "Start the Ollama app or run `ollama serve` in a separate terminal."
        ) from e

    payload = response.json()
    return [model["name"] for model in payload.get("models", [])]


def suggest_ollama_model_name(requested_model, installed_models):
    if not installed_models:
        return None

    requested_lower = requested_model.lower()
    for model in installed_models:
        if model.lower() == requested_lower:
            return model

    normalized = requested_lower.replace("v1", "vl")
    for model in installed_models:
        if model.lower() == normalized:
            return model

    for model in installed_models:
        if requested_lower in model.lower() or model.lower() in requested_lower:
            return model

    return installed_models[0]


def _require_installed_model(model_name: str, installed_models: list[str], label: str) -> None:
    if model_name in installed_models:
        return

    suggestion = suggest_ollama_model_name(model_name, installed_models)
    suggestion_hint = f"\nTry instead: {suggestion}" if suggestion else ""
    raise SystemExit(
        f"{label} model '{model_name}' is not installed.{suggestion_hint}\n"
        f"Run: ollama pull {model_name}"
    )


def is_recoverable_primary_failure(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True

    message = str(exc).lower()
    recoverable_markers = (
        "out of memory",
        "oom",
        "cuda",
        "vram",
        "memory",
        "timeout",
        "timed out",
        "resource exhausted",
        "model runner has terminated",
        "crashed",
        "killed",
        "500 internal server error",
        "server error",
    )
    return any(marker in message for marker in recoverable_markers)


def call_ollama_chat(model: str, messages, response_format=None):
    config = get_ollama_config()
    ollama_payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "num_ctx": config["num_ctx"],
            **OLLAMA_INFERENCE_OPTIONS,
        },
    }
    if response_format:
        ollama_payload["format"] = response_format

    try:
        ollama_res = requests.post(
            f"{config['url']}/api/chat",
            json=ollama_payload,
            timeout=config["timeout"],
        )
    except requests.RequestException as e:
        if is_recoverable_primary_failure(e):
            raise ModelRecoverableError(
                f"Primary model '{model}' request failed: {e}"
            ) from e
        raise RuntimeError(
            f"Could not reach Ollama at {config['url']}. "
            "Make sure the Ollama app is running on this machine."
        ) from e

    try:
        response_data = ollama_res.json()
    except ValueError as e:
        raise RuntimeError(
            f"Ollama returned a non-JSON response (HTTP {ollama_res.status_code}): "
            f"{ollama_res.text[:500]}"
        ) from e

    if ollama_res.status_code != 200:
        error_message = response_data.get("error") or response_data
        wrapped = RuntimeError(
            f"Ollama request failed (HTTP {ollama_res.status_code}) for model "
            f"'{model}': {error_message}"
        )
        if is_recoverable_primary_failure(wrapped):
            raise ModelRecoverableError(str(wrapped)) from wrapped
        raise wrapped

    if "error" in response_data:
        wrapped = RuntimeError(f"Ollama error for model '{model}': {response_data['error']}")
        if is_recoverable_primary_failure(wrapped):
            raise ModelRecoverableError(str(wrapped)) from wrapped
        raise wrapped

    message = response_data.get("message")
    if not message or "content" not in message:
        raise RuntimeError(
            "Ollama returned an unexpected response shape. "
            f"Response keys: {list(response_data.keys())}. "
            f"Full response: {response_data}"
        )

    return message["content"]


def validate_ollama_access():
    config = get_ollama_config()
    installed_models = list_ollama_models()
    logger.info("Ollama endpoint: %s", config["url"])
    logger.info("Primary model: %s", PRIMARY_MODEL)
    logger.info("Fallback model: %s", FALLBACK_MODEL)
    logger.info("Context window (num_ctx): %s", config["num_ctx"])
    logger.info("Inference options: %s", OLLAMA_INFERENCE_OPTIONS)
    logger.info(
        "Installed models: %s",
        ", ".join(installed_models) if installed_models else "(none)",
    )

    _require_installed_model(PRIMARY_MODEL, installed_models, "Primary")
    _require_installed_model(FALLBACK_MODEL, installed_models, "Fallback")

    if not config["warmup"]:
        logger.info("Ollama warmup skipped.")
        return

    logger.info(
        "Warming up primary model '%s' (first load can take a minute)...",
        PRIMARY_MODEL,
    )
    warmup_text = call_ollama_chat(
        PRIMARY_MODEL,
        [{"role": "user", "content": 'Reply with exactly {"ok": true}'}],
        response_format=EXTRACTION_JSON_SCHEMA if config["json_mode"] else None,
    )
    logger.info("Ollama warmup succeeded: %s", warmup_text[:120])
