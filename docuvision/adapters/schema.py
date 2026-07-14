"""Shared SQL migration loader for SQLite and PostgreSQL adapters."""

from __future__ import annotations

from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def migration_paths() -> list[Path]:
    """Return migration SQL files in lexical (version) order."""
    return sorted(_MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def load_migration_scripts() -> list[tuple[str, str]]:
    """Return ``(filename, sql_text)`` pairs for all migrations."""
    return [(path.name, path.read_text(encoding="utf-8")) for path in migration_paths()]


def split_sql_statements(migration_sql: str) -> list[str]:
    """Split a dual-engine migration script into executable statements.

    Strips leading comment-only chunks so PostgreSQL can run each statement
    independently. ``CREATE …`` bodies themselves must not contain ``;``.
    """
    statements: list[str] = []
    for part in migration_sql.split(";"):
        stripped = part.strip()
        if not stripped:
            continue
        if all(
            line.strip().startswith("--") or not line.strip()
            for line in stripped.splitlines()
        ):
            continue
        executable = "\n".join(
            line
            for line in stripped.splitlines()
            if not line.strip().startswith("--")
        ).strip()
        if executable:
            statements.append(executable)
    return statements
