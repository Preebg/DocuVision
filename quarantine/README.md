# Quarantine — retired cloud / pre-migration runtime

Modules here are **not** imported by the on-prem air-gapped stack
(`run_api.py`, `run_ingestion.py`, `run_worker.py`).

| Artifact | Reason |
|----------|--------|
| `watcher.py` | Supabase Storage / `document_pipeline` cloud sync daemon |
| `document_processor.py` | Cloud-watcher Docling + Ollama extraction path |
| `ollama_client.py` | Root legacy Ollama helper (replaced by `docuvision.adapters.ollama_client`) |
| `schema_validation.py` | Legacy extraction JSON-schema gate for the cloud watcher |
| `observability.py` | Root structured-logging helpers for the cloud watcher |
| `temp_files.py` | Root temp-file helpers for the cloud watcher |
| `test_app.py` | Unit tests for the quarantined cloud stack |

**Active runtime:**

* `run_ingestion.py` → `docuvision.adapters.ingestion.watcher` + `config/ingestion_sources.yaml` + SQLite
* `run_worker.py` → `docuvision` pipeline (local FS + SQLite/Postgres)
* `run_api.py` → sole FastAPI entrypoint on `127.0.0.1:8000`

Do not add this folder to `PYTHONPATH` or import it from production scripts.
