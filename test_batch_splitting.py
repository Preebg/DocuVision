"""Tests for physical PDF separator detection and child registration."""

from __future__ import annotations

from pathlib import Path

import fitz
import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import LOCAL_API_HEADERS
from docuvision.adapters.local_storage import LocalStorageAdapter
from docuvision.adapters.pdf_batch_splitter import PyMuPdfBatchSplitter
from docuvision.adapters.sqlite_repository import SqlitePipelineRepository
from docuvision.api.deps import (
    get_batch_split_engine,
    get_repository,
    get_storage,
)
from docuvision.api.main import create_app
from docuvision.config.settings import Settings
from docuvision.core.batch_splitting import (
    BarcodeStrategyConfig,
    BatchSplitEngine,
    BlankPageStrategyConfig,
    EncryptedPdfError,
    FixedIntervalStrategyConfig,
    NoContentPagesError,
)
from docuvision.core.models import PipelineStatus


def _pdf_bytes(
    blank_pages: set[int] | None = None,
    *,
    page_count: int = 4,
    title: str = "Synthetic batch",
) -> bytes:
    blank_pages = blank_pages or set()
    document = fitz.open()
    try:
        document.set_metadata({"title": title, "author": "DocuVision tests"})
        for page_number in range(1, page_count + 1):
            page = document.new_page(width=300, height=300)
            if page_number not in blank_pages:
                page.draw_rect(
                    fitz.Rect(20, 20, 280, 280),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
                page.insert_text((40, 150), f"PAGE {page_number}", color=(1, 1, 1))
        return document.tobytes()
    finally:
        document.close()


def _encrypted_pdf_bytes() -> bytes:
    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "secret")
        return document.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner-password",
            user_pw="user-password",
        )
    finally:
        document.close()


def _setup(
    tmp_path: Path,
    payload: bytes,
    *,
    before_and_after: bool = False,
) -> tuple[
    BatchSplitEngine,
    LocalStorageAdapter,
    SqlitePipelineRepository,
    str,
    str,
    Path,
]:
    settings = Settings(data_root=tmp_path / "data", database_url=None)
    storage = LocalStorageAdapter(settings)
    repository = SqlitePipelineRepository(tmp_path / "docuvision.db", actor="test")
    file_name = storage.write_incoming("packet.pdf", payload)
    source = repository.create_job(file_name)
    links: list[tuple[str, int]] = []
    if before_and_after:
        before = repository.create_job(storage.write_incoming("before.pdf", payload))
        after = repository.create_job(storage.write_incoming("after.pdf", payload))
        links = [(before.id, 1), (source.id, 2), (after.id, 3)]
    else:
        links = [(source.id, 1)]
    batch = repository.create_batch(name="scanner batch", documents=links)
    engine = BatchSplitEngine(
        repository=repository,
        storage=storage,
        splitter=PyMuPdfBatchSplitter(),
    )
    path = storage.resolve_document_path(file_name, prefer_verification=False)
    return engine, storage, repository, batch.id, source.id, path


def test_fixed_interval_replaces_source_and_preserves_order(tmp_path: Path) -> None:
    engine, storage, repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes(page_count=7), before_and_after=True
    )
    result = engine.process_batch_splits(
        batch_id,
        str(path),
        FixedIntervalStrategyConfig(
            strategy="fixed_interval", pages_per_document=3
        ),
        source_document_id=source_id,
    )

    assert [(part.start_page, part.end_page) for part in result.parts] == [
        (1, 3),
        (4, 6),
        (7, 7),
    ]
    batch = repository.get_batch(batch_id)
    assert batch is not None
    assert [link.sequence_no for link in batch.documents] == [1, 2, 3, 4, 5]
    assert source_id not in {link.document_id for link in batch.documents}
    assert repository.get_job(source_id).status == PipelineStatus.SPLIT_PARENT  # type: ignore[union-attr]
    for part in result.parts:
        child_path = storage.resolve_document_path(
            part.file_name, prefer_verification=False
        )
        with fitz.open(child_path) as child:
            assert child.page_count == part.end_page - part.start_page + 1
            assert child.metadata["title"] == "Synthetic batch"


def test_blank_and_consecutive_separators_never_create_empty_parts(
    tmp_path: Path,
) -> None:
    engine, _storage, _repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes({2, 3}, page_count=5)
    )
    result = engine.process_batch_splits(
        batch_id,
        str(path),
        BlankPageStrategyConfig(
            strategy="blank_page",
            non_white_threshold=0.01,
            white_cutoff=245,
            dpi=72,
        ),
        source_document_id=source_id,
    )
    assert result.separator_pages == [2, 3]
    assert [(part.start_page, part.end_page) for part in result.parts] == [
        (1, 1),
        (4, 5),
    ]


def test_no_separator_creates_byte_identical_single_child(tmp_path: Path) -> None:
    payload = _pdf_bytes(page_count=2)
    engine, storage, _repository, batch_id, source_id, path = _setup(
        tmp_path, payload
    )
    result = engine.process_batch_splits(
        batch_id,
        str(path),
        FixedIntervalStrategyConfig(
            strategy="fixed_interval", pages_per_document=10
        ),
        source_document_id=source_id,
    )
    assert len(result.parts) == 1
    child_path = storage.resolve_document_path(
        result.parts[0].file_name, prefer_verification=False
    )
    assert child_path.read_bytes() == payload


def test_all_separator_pages_create_no_empty_document(tmp_path: Path) -> None:
    engine, storage, repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes({1, 2}, page_count=2)
    )
    with pytest.raises(NoContentPagesError, match="no child was created"):
        engine.process_batch_splits(
            batch_id,
            str(path),
            BlankPageStrategyConfig(strategy="blank_page", dpi=72),
            source_document_id=source_id,
        )
    assert repository.get_batch_split_children(batch_id, source_id) == []
    assert repository.get_job(source_id).status == PipelineStatus.QUEUED  # type: ignore[union-attr]
    assert storage.list_incoming() == ["packet.pdf"]


def test_barcode_strategy_uses_exact_configured_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _storage, _repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes(page_count=3)
    )
    calls = 0

    def fake_decode(_image) -> list[str]:
        nonlocal calls
        calls += 1
        return ["DOC-BREAK"] if calls == 2 else ["OTHER"]

    monkeypatch.setattr(engine._splitter, "_decode_barcodes", fake_decode)
    result = engine.process_batch_splits(
        batch_id,
        str(path),
        BarcodeStrategyConfig(
            strategy="barcode", separator_values=["DOC-BREAK"], dpi=72
        ),
        source_document_id=source_id,
    )
    assert result.separator_pages == [2]
    assert [(part.start_page, part.end_page) for part in result.parts] == [
        (1, 1),
        (3, 3),
    ]


def test_zxing_decoder_smoke() -> None:
    import zxingcpp

    barcode = zxingcpp.create_barcode(
        "DOC-BREAK", zxingcpp.BarcodeFormat.QRCode
    )
    image = np.asarray(zxingcpp.write_barcode_to_image(barcode, scale=4))
    assert PyMuPdfBatchSplitter._decode_barcodes(image) == ["DOC-BREAK"]


def test_encrypted_pdf_is_quarantined_without_children(tmp_path: Path) -> None:
    engine, _storage, repository, batch_id, source_id, path = _setup(
        tmp_path, _encrypted_pdf_bytes()
    )
    with pytest.raises(EncryptedPdfError, match="password-protected"):
        engine.process_batch_splits(
            batch_id,
            str(path),
            FixedIntervalStrategyConfig(
                strategy="fixed_interval", pages_per_document=2
            ),
            source_document_id=source_id,
        )
    job = repository.get_job(source_id)
    assert job is not None and job.status == PipelineStatus.QUARANTINED
    quarantine = repository.get_active_quarantine(source_id)
    assert quarantine is not None
    assert quarantine.failed_stage == "BATCH_SPLIT"
    assert repository.get_batch_split_children(batch_id, source_id) == []


def test_storage_children_are_rolled_back_when_database_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, storage, repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes({2}, page_count=3)
    )

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "commit_batch_split", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        engine.process_batch_splits(
            batch_id,
            str(path),
            BlankPageStrategyConfig(strategy="blank_page", dpi=72),
            source_document_id=source_id,
        )
    assert storage.list_incoming() == ["packet.pdf"]


def test_split_is_idempotent(tmp_path: Path) -> None:
    engine, storage, _repository, batch_id, source_id, path = _setup(
        tmp_path, _pdf_bytes(page_count=2)
    )
    config = FixedIntervalStrategyConfig(
        strategy="fixed_interval", pages_per_document=1
    )
    first = engine.process_batch_splits(
        batch_id, str(path), config, source_document_id=source_id
    )
    archived_source = storage.resolve_document_path(
        "packet.pdf", prefer_verification=True
    )
    second = engine.process_batch_splits(
        batch_id,
        str(archived_source),
        config,
        source_document_id=source_id,
    )
    assert second.idempotent_replay is True
    assert [part.document_id for part in second.parts] == [
        part.document_id for part in first.parts
    ]


def test_batch_split_api(tmp_path: Path) -> None:
    engine, storage, repository, batch_id, source_id, _path = _setup(
        tmp_path, _pdf_bytes(page_count=3)
    )
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_batch_split_engine] = lambda: engine
    with TestClient(app) as client:
        response = client.post(
            f"/api/batches/{batch_id}/split",
            json={
                "source_document_id": source_id,
                "strategy_config": {
                    "strategy": "fixed_interval",
                    "pages_per_document": 2,
                },
            },
            headers=LOCAL_API_HEADERS,
        )
    assert response.status_code == 200, response.text
    assert [
        (part["start_page"], part["end_page"])
        for part in response.json()["parts"]
    ] == [(1, 2), (3, 3)]


def test_split_api_rejects_unknown_strategy(tmp_path: Path) -> None:
    engine, storage, repository, batch_id, source_id, _path = _setup(
        tmp_path, _pdf_bytes(page_count=1)
    )
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_batch_split_engine] = lambda: engine
    with TestClient(app) as client:
        response = client.post(
            f"/api/batches/{batch_id}/split",
            json={
                "source_document_id": source_id,
                "strategy_config": {"strategy": "magic"},
            },
            headers=LOCAL_API_HEADERS,
        )
    assert response.status_code == 422
