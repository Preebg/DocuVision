"""Spawn-safe local Hugging Face vision/OCR worker.

The parent process imports no torch/transformers modules.  A worker loads one
model, processes one crop directory, writes an atomic JSON result, explicitly
clears accelerator caches, and exits.  Process exit is the final memory-release
boundary even when a backend retains native allocations.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import tempfile
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def transformer_crop_worker(
    input_dir: str,
    output_path: str,
    model_config: dict[str, Any],
) -> None:
    """Load a multimodal model and OCR every image in ``input_dir``.

    This is a top-level function by design: Windows ``multiprocessing`` uses
    ``spawn`` and therefore requires a pickleable target.
    """
    output = Path(output_path)
    model = None
    processor = None
    torch_module = None
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}

    try:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        import torch
        from PIL import Image
        from transformers import AutoProcessor

        torch_module = torch
        model_id = str(model_config["model_id"])
        trust_remote_code = bool(model_config.get("trust_remote_code", True))
        quantization = str(model_config.get("quantization", "none")).lower()

        load_kwargs: dict[str, Any] = {
            "device_map": model_config.get("device_map", "auto"),
            "low_cpu_mem_usage": True,
            "trust_remote_code": trust_remote_code,
        }
        dtype = model_config.get("torch_dtype", "auto")
        if dtype != "auto":
            load_kwargs["torch_dtype"] = getattr(torch, str(dtype))
        else:
            load_kwargs["torch_dtype"] = "auto"

        if quantization in {"4bit", "8bit"}:
            from transformers import BitsAndBytesConfig

            if quantization == "4bit":
                load_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_use_double_quant=True,
                )
            else:
                load_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_8bit=True
                )

        processor = AutoProcessor.from_pretrained(
            model_id,
            trust_remote_code=trust_remote_code,
        )
        model = _load_image_text_model(model_id, load_kwargs)
        model.eval()

        prompt = str(
            model_config.get("prompt")
            or "Read the image exactly. Return only the extracted text."
        )
        max_new_tokens = int(model_config.get("max_new_tokens", 256))
        fail_fast = bool(model_config.get("fail_fast", False))

        image_paths = sorted(
            path
            for path in Path(input_dir).iterdir()
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        )
        for image_path in image_paths:
            try:
                with Image.open(image_path) as opened:
                    image = opened.convert("RGB")
                    text = _generate_text(
                        model=model,
                        processor=processor,
                        image=image,
                        prompt=prompt,
                        max_new_tokens=max_new_tokens,
                        torch_module=torch,
                    )
                results[image_path.stem] = {"text": text}
            except Exception as exc:
                failures[image_path.stem] = f"{type(exc).__name__}: {exc}"
                if fail_fast:
                    raise

        _write_json_atomic(
            output,
            {
                "ok": not (fail_fast and failures),
                "results": results,
                "failures": failures,
                "model_id": model_id,
            },
        )
    except BaseException as exc:
        _write_json_atomic(
            output,
            {
                "ok": False,
                "results": results,
                "failures": failures,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
    finally:
        # Native CUDA allocators may retain pools after Python references die.
        # Explicit cleanup reduces the peak before process termination; process
        # exit remains the authoritative release boundary.
        try:
            del model
            del processor
        except Exception:
            pass
        gc.collect()
        if torch_module is not None:
            try:
                if torch_module.cuda.is_available():
                    torch_module.cuda.synchronize()
                    torch_module.cuda.empty_cache()
                    torch_module.cuda.ipc_collect()
            except Exception:
                logger.exception("model_worker.cuda_cleanup_failed")
        gc.collect()


def _load_image_text_model(model_id: str, load_kwargs: dict[str, Any]) -> Any:
    """Load once; never retry an OOM through another auto-model class."""
    from transformers import AutoModelForImageTextToText

    return AutoModelForImageTextToText.from_pretrained(model_id, **load_kwargs)


def _generate_text(
    *,
    model: Any,
    processor: Any,
    image: Any,
    prompt: str,
    max_new_tokens: int,
    torch_module: Any,
) -> str:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    if hasattr(processor, "apply_chat_template"):
        rendered_prompt = processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
    else:
        rendered_prompt = prompt

    try:
        inputs = processor(
            text=[rendered_prompt],
            images=[image],
            padding=True,
            return_tensors="pt",
        )
    except TypeError:
        inputs = processor(images=image, text=rendered_prompt, return_tensors="pt")

    device = _model_input_device(model, torch_module)
    if hasattr(inputs, "to"):
        inputs = inputs.to(device)
    else:
        inputs = {
            key: value.to(device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }

    input_ids = inputs.get("input_ids")
    with torch_module.inference_mode():
        generated = model.generate(
            **inputs,
            do_sample=False,
            max_new_tokens=max_new_tokens,
        )

    if input_ids is not None and getattr(generated, "ndim", 0) == 2:
        generated = generated[:, input_ids.shape[1] :]
    decoded = processor.batch_decode(
        generated,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )
    return (decoded[0] if decoded else "").strip()


def _model_input_device(model: Any, torch_module: Any) -> Any:
    device = getattr(model, "device", None)
    if device is not None and str(device) != "meta":
        return device
    device_map = getattr(model, "hf_device_map", None)
    if isinstance(device_map, dict):
        for mapped in device_map.values():
            if mapped not in {"cpu", "disk"}:
                if isinstance(mapped, int):
                    return torch_module.device(f"cuda:{mapped}")
                return mapped
    return torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


__all__ = ["transformer_crop_worker"]
