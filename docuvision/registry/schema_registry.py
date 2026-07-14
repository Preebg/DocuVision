"""Code-first semantic schema registry for document-class routing.

Loads YAML field contracts from ``config/schemas/`` at startup. These are
semantic extraction contracts — not legacy pixel layout templates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS_DIR = _REPO_ROOT / "config" / "schemas"
DEFAULT_PROMPTS_DIR = _REPO_ROOT / "config" / "prompts"
FALLBACK_CLASS_ID = "unclassified"
CLASSIFY_PROMPT_NAME = "classify_router.md"

_FIELD_TYPE_TO_JSON: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "array": {"type": "array", "items": {"type": "string"}},
}


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """One semantic field the vision model must extract."""

    name: str
    type: str
    description_prompt: str


@dataclass(frozen=True, slots=True)
class DocumentSchema:
    """Semantic contract for a document class."""

    class_id: str
    display_name: str
    classification_keywords: tuple[str, ...]
    fields: tuple[FieldDefinition, ...]
    source_path: str = ""
    is_fallback: bool = False

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def fields_as_dicts(self) -> list[dict[str, str]]:
        return [
            {
                "name": f.name,
                "type": f.type,
                "description_prompt": f.description_prompt,
            }
            for f in self.fields
        ]


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Outcome of the pre-pass router."""

    class_id: str
    schema: DocumentSchema
    method: str  # "keyword" | "llm" | "fallback"
    confidence: float = 1.0
    matched_keywords: tuple[str, ...] = field(default_factory=tuple)


def _coerce_field(raw: dict[str, Any], source: Path) -> FieldDefinition:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"Field missing name in {source}")
    ftype = str(raw.get("type") or "string").strip().lower()
    prompt = str(raw.get("description_prompt") or "").strip()
    if not prompt:
        raise ValueError(f"Field '{name}' missing description_prompt in {source}")
    return FieldDefinition(name=name, type=ftype, description_prompt=prompt)


def _load_schema_file(path: Path) -> DocumentSchema:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Schema root must be a mapping: {path}")

    class_id = str(data.get("class_id") or "").strip()
    if not class_id:
        raise ValueError(f"Schema missing class_id: {path}")

    display_name = str(data.get("display_name") or class_id).strip()
    keywords_raw = data.get("classification_keywords") or []
    if not isinstance(keywords_raw, list):
        raise ValueError(f"classification_keywords must be a list: {path}")
    keywords = tuple(str(k).strip().lower() for k in keywords_raw if str(k).strip())

    fields_raw = data.get("fields") or []
    if not isinstance(fields_raw, list) or not fields_raw:
        raise ValueError(f"Schema must declare a non-empty fields list: {path}")
    fields = tuple(_coerce_field(item, path) for item in fields_raw)

    is_fallback = path.name.startswith("_") or class_id == FALLBACK_CLASS_ID
    return DocumentSchema(
        class_id=class_id,
        display_name=display_name,
        classification_keywords=keywords,
        fields=fields,
        source_path=str(path),
        is_fallback=is_fallback,
    )


class SchemaRegistry:
    """Loads YAML semantic schemas and routes documents to a class_id."""

    def __init__(
        self,
        schemas_dir: Path | str | None = None,
        *,
        prompts_dir: Path | str | None = None,
    ) -> None:
        self._schemas_dir = Path(schemas_dir) if schemas_dir else DEFAULT_SCHEMAS_DIR
        self._prompts_dir = Path(prompts_dir) if prompts_dir else DEFAULT_PROMPTS_DIR
        self._by_id: dict[str, DocumentSchema] = {}
        self._ordered: list[DocumentSchema] = []
        self._fallback: DocumentSchema | None = None
        self._classify_prompt_template: str | None = None
        self.reload()

    @property
    def schemas_dir(self) -> Path:
        return self._schemas_dir

    @property
    def fallback(self) -> DocumentSchema:
        if self._fallback is None:
            raise RuntimeError(
                f"No fallback schema (_fallback.yaml) in {self._schemas_dir}"
            )
        return self._fallback

    def reload(self) -> None:
        """(Re)load all ``*.yaml`` / ``*.yml`` schema files from disk."""
        if not self._schemas_dir.is_dir():
            raise FileNotFoundError(
                f"Schema directory not found: {self._schemas_dir}. "
                "Create config/schemas/ with invoice, medical_intake, and _fallback."
            )

        by_id: dict[str, DocumentSchema] = {}
        ordered: list[DocumentSchema] = []
        fallback: DocumentSchema | None = None

        paths = sorted(
            [
                *self._schemas_dir.glob("*.yaml"),
                *self._schemas_dir.glob("*.yml"),
            ]
        )
        if not paths:
            raise FileNotFoundError(f"No schema YAML files in {self._schemas_dir}")

        for path in paths:
            schema = _load_schema_file(path)
            if schema.class_id in by_id:
                raise ValueError(
                    f"Duplicate class_id '{schema.class_id}' in {path} "
                    f"(already loaded from {by_id[schema.class_id].source_path})"
                )
            by_id[schema.class_id] = schema
            ordered.append(schema)
            if schema.is_fallback:
                fallback = schema

        if fallback is None:
            raise RuntimeError(
                f"Missing fallback schema in {self._schemas_dir} "
                "(expected _fallback.yaml with class_id=unclassified)"
            )

        self._by_id = by_id
        self._ordered = ordered
        self._fallback = fallback
        self._classify_prompt_template = None
        logger.info(
            "schema_registry.loaded count=%s classes=%s dir=%s",
            len(ordered),
            ",".join(s.class_id for s in ordered),
            self._schemas_dir,
        )

    def list_schemas(self) -> list[DocumentSchema]:
        return list(self._ordered)

    def list_class_ids(self, *, include_fallback: bool = True) -> list[str]:
        return [
            s.class_id
            for s in self._ordered
            if include_fallback or not s.is_fallback
        ]

    def get(self, class_id: str) -> DocumentSchema:
        schema = self._by_id.get(class_id)
        if schema is None:
            raise KeyError(f"Unknown document class_id: {class_id}")
        return schema

    def get_or_fallback(self, class_id: str | None) -> DocumentSchema:
        if class_id and class_id in self._by_id:
            return self._by_id[class_id]
        return self.fallback

    def classify_by_keywords(
        self,
        text: str,
        *,
        min_hits: int = 1,
    ) -> ClassificationResult | None:
        """Fast text pre-pass — no GPU. Returns None when inconclusive."""
        haystack = (text or "").lower()
        if not haystack.strip():
            return None

        scored: list[tuple[int, DocumentSchema, tuple[str, ...]]] = []
        for schema in self._ordered:
            if schema.is_fallback or not schema.classification_keywords:
                continue
            matched = tuple(
                kw for kw in schema.classification_keywords if kw in haystack
            )
            if len(matched) >= min_hits:
                scored.append((len(matched), schema, matched))

        if not scored:
            return None

        scored.sort(key=lambda item: (-item[0], item[1].class_id))
        best_hits, best_schema, matched = scored[0]
        # Require a unique winner (or a clear margin) to avoid wrong routing.
        if len(scored) > 1 and scored[1][0] == best_hits:
            return None

        confidence = min(1.0, 0.45 + 0.15 * best_hits)
        return ClassificationResult(
            class_id=best_schema.class_id,
            schema=best_schema,
            method="keyword",
            confidence=confidence,
            matched_keywords=matched,
        )

    def classify(
        self,
        text: str,
        *,
        llm_classify: Any | None = None,
        use_llm_fallback: bool = True,
    ) -> ClassificationResult:
        """Route text → class_id via keywords, optional LLM, then fallback.

        ``llm_classify`` is a callable ``(prompt: str) -> str`` that returns
        model text (JSON). Kept injectable so the registry stays GPU-optional.
        """
        keyword_hit = self.classify_by_keywords(text)
        if keyword_hit is not None and keyword_hit.confidence >= 0.6:
            return keyword_hit

        if use_llm_fallback and llm_classify is not None:
            try:
                prompt = self.build_classify_prompt(text)
                raw = llm_classify(prompt)
                parsed = _parse_classify_response(raw)
                class_id = str(parsed.get("class_id") or "").strip()
                schema = self.get_or_fallback(class_id)
                confidence = float(parsed.get("confidence") or 0.5)
                if schema.is_fallback and class_id and class_id != schema.class_id:
                    logger.warning(
                        "schema_registry.llm_unknown_class id=%s → fallback",
                        class_id,
                    )
                return ClassificationResult(
                    class_id=schema.class_id,
                    schema=schema,
                    method="llm",
                    confidence=max(0.0, min(1.0, confidence)),
                )
            except Exception as exc:
                logger.warning("schema_registry.llm_classify_failed error=%s", exc)

        if keyword_hit is not None:
            return keyword_hit

        return ClassificationResult(
            class_id=self.fallback.class_id,
            schema=self.fallback,
            method="fallback",
            confidence=0.0,
        )

    def build_classify_prompt(self, layout_text: str, *, max_chars: int = 2500) -> str:
        """Render ``classify_router.md`` with the allowed class catalog."""
        template = self._load_classify_prompt_template()
        specialized = [s for s in self._ordered if not s.is_fallback]
        catalog_lines: list[str] = []
        for schema in specialized:
            kws = ", ".join(schema.classification_keywords[:8]) or "(none)"
            catalog_lines.append(
                f"- `{schema.class_id}` — {schema.display_name} "
                f"(keywords: {kws})"
            )
        catalog_lines.append(
            f"- `{self.fallback.class_id}` — {self.fallback.display_name} "
            "(use when uncertain)"
        )
        class_ids = ", ".join(
            f"`{s.class_id}`" for s in [*specialized, self.fallback]
        )
        text = (layout_text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"

        return (
            template.replace("{{CLASS_IDS}}", class_ids)
            .replace("{{CLASS_CATALOG}}", "\n".join(catalog_lines))
            .replace("{{LAYOUT_TEXT}}", text or "(empty)")
        )

    def build_extraction_system_prompt(self, schema: DocumentSchema) -> str:
        field_lines = "\n".join(
            f"- `{f.name}` ({f.type}): {f.description_prompt}"
            for f in schema.fields
        )
        return (
            "You are a document OCR and structured extraction engine "
            "(Qwen2.5-VL). Read the attached page image(s) and bundled layout "
            "text together.\n"
            f"Document class: `{schema.class_id}` ({schema.display_name}).\n"
            "Extract ONLY the fields listed below. Do not invent extra keys.\n"
            "Return valid JSON only that matches the response schema exactly.\n\n"
            f"Fields to extract:\n{field_lines}"
        )

    def build_extraction_user_prompt(
        self,
        schema: DocumentSchema,
        layout_markdown: str,
        *,
        layout_max_chars: int = 8000,
    ) -> str:
        layout_text = (layout_markdown or "").strip()
        if len(layout_text) > layout_max_chars:
            layout_text = layout_text[:layout_max_chars] + "\n...[truncated]"

        keys = ", ".join(f'"{n}"' for n in schema.field_names())
        return (
            "Use BOTH the attached image(s) and the bundled layout text.\n"
            f'Put every extracted field under "fields" using keys: {keys}.\n'
            'Also populate "full_text" (exhaustive OCR) and "summary" '
            "(1-2 sentences).\n"
            "Empty string is allowed only when a field is truly absent.\n\n"
            "Bundled Docling structural markdown layout:\n"
            f"{layout_text if layout_text else '(no layout text extracted)'}"
        )

    def build_extraction_json_schema(self, schema: DocumentSchema) -> dict[str, Any]:
        """Ollama ``format`` schema — fields match the matched document class."""
        field_props: dict[str, Any] = {}
        for fdef in schema.fields:
            prop = dict(_FIELD_TYPE_TO_JSON.get(fdef.type, {"type": "string"}))
            prop["description"] = fdef.description_prompt
            field_props[fdef.name] = prop

        required_fields = list(schema.field_names())
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "properties": field_props,
                    "required": required_fields,
                    "additionalProperties": False,
                },
                "full_text": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["fields", "full_text", "summary"],
            "additionalProperties": False,
        }

    def normalize_extraction(
        self,
        schema: DocumentSchema,
        parsed: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize model JSON into the stable pipeline envelope."""
        raw_fields = parsed.get("fields")
        if not isinstance(raw_fields, dict):
            # Model may have returned flat schema keys at the top level.
            raw_fields = {
                name: parsed[name]
                for name in schema.field_names()
                if name in parsed
            }

        fields: dict[str, Any] = {}
        for fdef in schema.fields:
            value = raw_fields.get(fdef.name, "")
            if value is None:
                value = ""
            if fdef.type == "string":
                fields[fdef.name] = str(value)
            else:
                fields[fdef.name] = value

        full_text = parsed.get("full_text")
        if full_text is None and "full_text_ocr" in fields:
            full_text = fields.get("full_text_ocr", "")
        summary = parsed.get("summary")
        if summary is None and "summary" in fields:
            summary = fields.get("summary", "")

        envelope: dict[str, Any] = {
            "class_id": schema.class_id,
            "form_type": schema.class_id,
            "display_name": schema.display_name,
            "extracted_payload": {
                "full_text": str(full_text or ""),
                "summary": str(summary or ""),
                "fields": fields,
            },
        }

        # Promote familiar medical keys for legacy HITL / tests when present.
        for legacy_key in ("patient_name", "dob"):
            if legacy_key in fields and fields[legacy_key]:
                envelope[legacy_key] = str(fields[legacy_key])

        return envelope

    def _load_classify_prompt_template(self) -> str:
        if self._classify_prompt_template is not None:
            return self._classify_prompt_template
        path = self._prompts_dir / CLASSIFY_PROMPT_NAME
        if not path.is_file():
            raise FileNotFoundError(f"Missing classify prompt: {path}")
        self._classify_prompt_template = path.read_text(encoding="utf-8")
        return self._classify_prompt_template


def _parse_classify_response(ai_text: str) -> dict[str, Any]:
    import json

    text = (ai_text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


@lru_cache(maxsize=1)
def get_schema_registry(
    schemas_dir: str | None = None,
    prompts_dir: str | None = None,
) -> SchemaRegistry:
    """Process-wide registry singleton (optional override paths as strings)."""
    return SchemaRegistry(
        Path(schemas_dir) if schemas_dir else None,
        prompts_dir=Path(prompts_dir) if prompts_dir else None,
    )


def schemas_to_typescript(schemas: Sequence[DocumentSchema]) -> str:
    """Render a clean TypeScript module from loaded schemas (for codegen)."""
    lines: list[str] = [
        "/**",
        " * AUTO-GENERATED by scripts/generate_field_types.py",
        " * Source: config/schemas/*.yaml",
        " * Do not edit by hand — re-run the generator after schema changes.",
        " */",
        "",
    ]

    class_ids = [s.class_id for s in schemas]
    union = " | ".join(f'"{cid}"' for cid in class_ids) or "string"
    lines.append(f"export type DocumentClassId = {union};")
    lines.append("")

    lines.append("export interface SchemaFieldDefinition {")
    lines.append("  name: string;")
    lines.append("  type: string;")
    lines.append("  descriptionPrompt: string;")
    lines.append("}")
    lines.append("")

    lines.append("export interface DocumentClassSchema {")
    lines.append("  classId: DocumentClassId;")
    lines.append("  displayName: string;")
    lines.append("  fields: readonly SchemaFieldDefinition[];")
    lines.append("}")
    lines.append("")

    all_keys: list[str] = []
    seen: set[str] = set()
    per_class_const: list[str] = []

    for schema in schemas:
        interface_name = _to_pascal(schema.class_id) + "Fields"
        lines.append(f"/** Fields for `{schema.class_id}` ({schema.display_name}). */")
        lines.append(f"export interface {interface_name} {{")
        for fdef in schema.fields:
            ts_type = _yaml_type_to_ts(fdef.type)
            lines.append(f"  {fdef.name}: {ts_type};")
            if fdef.name not in seen:
                seen.add(fdef.name)
                all_keys.append(fdef.name)
        lines.append("}")
        lines.append("")

        key_const = _to_screaming(schema.class_id) + "_FIELD_KEYS"
        key_list = ", ".join(f'"{n}"' for n in schema.field_names())
        per_class_const.append(
            f'  "{schema.class_id}": {key_const},'
        )
        lines.append(
            f"export const {key_const} = [{key_list}] as const;"
        )
        lines.append("")

    lines.append("export const DOCUMENT_CLASS_SCHEMAS = {")
    for schema in schemas:
        lines.append(f'  "{schema.class_id}": {{')
        lines.append(f'    classId: "{schema.class_id}",')
        lines.append(f'    displayName: "{_escape_ts(schema.display_name)}",')
        lines.append("    fields: [")
        for fdef in schema.fields:
            lines.append("      {")
            lines.append(f'        name: "{fdef.name}",')
            lines.append(f'        type: "{fdef.type}",')
            lines.append(
                f'        descriptionPrompt: "{_escape_ts(fdef.description_prompt)}",'
            )
            lines.append("      },")
        lines.append("    ],")
        lines.append("  },")
    lines.append("} as const satisfies Record<DocumentClassId, DocumentClassSchema>;")
    lines.append("")

    lines.append("export const SCHEMA_FIELD_KEYS = {")
    lines.extend(per_class_const)
    lines.append("} as const satisfies Record<DocumentClassId, readonly string[]>;")
    lines.append("")

    all_literal = ", ".join(f'"{k}"' for k in all_keys)
    lines.append(
        f"/** Union of every field name across registered document classes. */"
    )
    lines.append(f"export const ALL_FORM_FIELD_KEYS = [{all_literal}] as const;")
    lines.append("")
    lines.append(
        "export type FormFieldKey = (typeof ALL_FORM_FIELD_KEYS)[number];"
    )
    lines.append("")
    lines.append(
        "/** HITL clerk form keys — mirrors the semantic registry (all classes). */"
    )
    lines.append(
        "export const VERIFICATION_FIELD_KEYS = ALL_FORM_FIELD_KEYS;"
    )
    lines.append(
        "export type VerificationFieldKey = FormFieldKey;"
    )
    lines.append("")

    return "\n".join(lines) + "\n"


def _yaml_type_to_ts(ftype: str) -> str:
    return {
        "string": "string",
        "number": "number",
        "integer": "number",
        "boolean": "boolean",
        "array": "string[]",
    }.get(ftype.lower(), "string")


def _to_pascal(value: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", value)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _to_screaming(value: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", value)
    return "_".join(p.upper() for p in parts if p)


def _escape_ts(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
    )


__all__ = [
    "ClassificationResult",
    "DEFAULT_PROMPTS_DIR",
    "DEFAULT_SCHEMAS_DIR",
    "DocumentSchema",
    "FALLBACK_CLASS_ID",
    "FieldDefinition",
    "SchemaRegistry",
    "get_schema_registry",
    "schemas_to_typescript",
]
