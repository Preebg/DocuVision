"""Schema registry package — semantic document-class contracts."""

from docuvision.registry.schema_registry import (
    ClassificationResult,
    DocumentSchema,
    FieldDefinition,
    SchemaRegistry,
    get_schema_registry,
)

__all__ = [
    "ClassificationResult",
    "DocumentSchema",
    "FieldDefinition",
    "SchemaRegistry",
    "get_schema_registry",
]
