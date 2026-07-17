"""Lifecycle and aggregation tests for the resource-gated extractor."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from docuvision.config.settings import Settings
from docuvision.stages.gated_extraction import (
    DeferredGatedLayoutStage,
    GatedExtractionError,
    GatedExtractionPipeline,
    GatedPipelineConfig,
    RawDocument,
    _spawn_and_read,
)
from run_worker import build_pipeline


def _sleeping_worker(_output_path: str) -> None:
    time.sleep(30)


def _crashing_worker(_output_path: str) -> None:
    os._exit(17)


def _fake_layout(
    calls: list[str],
    *,
    include_serial: bool,
):
    def run(
        documents: list[dict[str, str]],
        session: Any,
    ) -> dict[str, Any]:
        calls.append("layout")
        assert Path(documents[0]["path"]).read_bytes() == b"source-bytes"
        regions: list[dict[str, Any]] = []

        unchecked = Image.new("RGB", (48, 48), "white")
        ImageDraw.Draw(unchecked).rectangle((3, 3, 44, 44), outline="black", width=3)
        unchecked.save(session.box / "box-unchecked.png")

        checked = Image.new("RGB", (48, 48), "white")
        checked_draw = ImageDraw.Draw(checked)
        checked_draw.rectangle((3, 3, 44, 44), outline="black", width=3)
        checked_draw.line((11, 12, 37, 37), fill="black", width=5)
        checked_draw.line((37, 12, 11, 37), fill="black", width=5)
        checked.save(session.box / "box-checked.png")

        Image.new("RGB", (80, 24), "white").save(session.txt / "text-1.png")
        crop_ids = [
            ("box-unchecked", "box"),
            ("box-checked", "box"),
            ("text-1", "txt"),
        ]
        if include_serial:
            Image.new("RGB", (80, 24), "white").save(session.sn / "serial-1.png")
            crop_ids.append(("serial-1", "sn"))

        for index, (region_id, category) in enumerate(crop_ids, start=1):
            regions.append(
                {
                    "region_id": region_id,
                    "document_id": "doc-0001",
                    "document_name": "scan.png",
                    "page": 1,
                    "category": category,
                    "label": category,
                    "source_text": "",
                    "bbox_2d": [index, index, index + 10, index + 10],
                    "normalized_bbox": [10, 10, 20, 20],
                }
            )
        return {
            "version": 1,
            "documents": [
                {
                    "document_id": "doc-0001",
                    "name": "scan.png",
                    "page_count": 1,
                    "layout_markdown": "# Scan",
                }
            ],
            "regions": regions,
        }

    return run


def _fake_models(calls: list[str]):
    def run(
        wave: str,
        input_dir: Path,
        _output_path: Path,
        _model_config: Any,
        _timeout: float,
    ) -> dict[str, Any]:
        calls.append(wave)
        values = {
            path.stem: {
                "text": "handwritten body"
                if wave == "glm_ocr"
                else "SN-A19XZ"
            }
            for path in input_dir.glob("*.png")
        }
        return {"ok": True, "results": values, "failures": {}}

    return run


def test_waves_are_sequential_aggregated_and_session_is_deleted(tmp_path):
    calls: list[str] = []
    pipeline = GatedExtractionPipeline(
        GatedPipelineConfig(temp_root=tmp_path),
        layout_runner=_fake_layout(calls, include_serial=True),
        model_runner=_fake_models(calls),
    )

    result = pipeline.process_batch(
        [RawDocument(name="scan.png", content=b"source-bytes")]
    )

    assert calls == ["layout", "glm_ocr", "qwen_serial"]
    fields = result["extracted_payload"]["fields"]
    assert fields["box-unchecked"]["value"] == "[ ]"
    assert fields["box-checked"]["value"] == "[X]"
    assert fields["text-1"]["value"] == "handwritten body"
    assert fields["serial-1"]["value"] == "SN-A19XZ"
    assert "handwritten body" in result["extracted_payload"]["full_text"]
    assert result["diagnostics"]["serial_wave_skipped"] is False
    assert not (tmp_path / result["session_id"]).exists()


def test_qwen_wave_is_not_loaded_without_serial_crops(tmp_path):
    calls: list[str] = []
    pipeline = GatedExtractionPipeline(
        GatedPipelineConfig(temp_root=tmp_path),
        layout_runner=_fake_layout(calls, include_serial=False),
        model_runner=_fake_models(calls),
    )

    result = pipeline.process_batch(
        [RawDocument(name="scan.png", content=b"source-bytes")]
    )

    assert calls == ["layout", "glm_ocr"]
    assert result["diagnostics"]["serial_wave_skipped"] is True
    assert not (tmp_path / result["session_id"]).exists()


@pytest.mark.asyncio
async def test_async_entry_preserves_wave_order(tmp_path):
    calls: list[str] = []
    pipeline = GatedExtractionPipeline(
        GatedPipelineConfig(temp_root=tmp_path),
        layout_runner=_fake_layout(calls, include_serial=True),
        model_runner=_fake_models(calls),
    )

    result = await pipeline.aprocess_batch(
        [RawDocument(name="scan.png", content=b"source-bytes")]
    )

    assert calls == ["layout", "glm_ocr", "qwen_serial"]
    assert result["extracted_payload"]["fields"]


def test_failed_stage_cleans_session(tmp_path):
    def fail_layout(_documents: Any, _session: Any) -> dict[str, Any]:
        raise RuntimeError("layout exploded")

    pipeline = GatedExtractionPipeline(
        GatedPipelineConfig(temp_root=tmp_path),
        layout_runner=fail_layout,
    )

    with pytest.raises(GatedExtractionError, match="layout exploded"):
        pipeline.process_batch(
            [RawDocument(name="scan.png", content=b"source-bytes")]
        )

    assert list(tmp_path.iterdir()) == []


def test_worker_composition_selects_gated_backend_without_ollama(tmp_path):
    settings = Settings(
        _env_file=None,
        data_root=tmp_path / "data",
        gated_temp_root=tmp_path / "sessions",
        extraction_backend="gated",
    )

    pipeline = build_pipeline(settings)

    assert isinstance(pipeline._layout, DeferredGatedLayoutStage)


def test_spawn_timeout_terminates_and_reaps_worker(tmp_path):
    output = tmp_path / "never-written.json"

    with pytest.raises(GatedExtractionError, match="exceeded"):
        _spawn_and_read(
            target=_sleeping_worker,
            args=(str(output),),
            output_path=output,
            timeout_seconds=0.25,
            stage="timeout-test",
            memory_limit_mb=512,
        )

    assert not output.exists()


def test_spawn_hard_crash_is_reported_without_stale_result(tmp_path):
    output = tmp_path / "never-written.json"

    with pytest.raises(GatedExtractionError, match="code 17"):
        _spawn_and_read(
            target=_crashing_worker,
            args=(str(output),),
            output_path=output,
            timeout_seconds=20,
            stage="crash-test",
            memory_limit_mb=512,
        )
