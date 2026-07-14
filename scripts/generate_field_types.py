#!/usr/bin/env python3
"""Generate TypeScript form field types from ``config/schemas/*.yaml``.

Offline, zero-network. Run from the repo root after editing semantic schemas::

    python scripts/generate_field_types.py

Writes ``src/lib/form-types.gen.ts`` so the React/Tauri HITL UI stays in sync
with the backend schema registry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from docuvision.registry.schema_registry import (  # noqa: E402
    DEFAULT_SCHEMAS_DIR,
    SchemaRegistry,
    schemas_to_typescript,
)

DEFAULT_OUT = _REPO_ROOT / "src" / "lib" / "form-types.gen.ts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate src/lib/form-types.gen.ts from config/schemas/"
    )
    parser.add_argument(
        "--schemas-dir",
        type=Path,
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Schema YAML directory (default: {DEFAULT_SCHEMAS_DIR})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output TypeScript path (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    registry = SchemaRegistry(args.schemas_dir)
    typescript = schemas_to_typescript(registry.list_schemas())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(typescript, encoding="utf-8", newline="\n")

    classes = ", ".join(s.class_id for s in registry.list_schemas())
    print(f"Wrote {args.out}")
    print(f"  classes: {classes}")
    print(f"  schemas_dir: {args.schemas_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
