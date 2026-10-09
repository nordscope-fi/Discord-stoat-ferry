from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import regenerate_dce_2_48_source_fixture as regenerator
from scripts.regenerate_dce_2_48_source_fixture import (
    PINNED_SOURCE_SHA256,
    SourceMismatchError,
    build_fixture,
    regenerate_fixture,
    validate_fixture_paths,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dce_2_48" / "source-derived"
_EXPORT_PATH = _FIXTURE_DIR / "maximal-writer-shape.json"
_LEDGER_PATH = _FIXTURE_DIR / "field-dispositions.json"
_PROVENANCE_PATH = _FIXTURE_DIR / "provenance.json"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_generator_rebuilds_committed_fixture_without_reading_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _load_json(_EXPORT_PATH)
    committed_path = _EXPORT_PATH.resolve()
    original_read_text = Path.read_text

    def reject_committed_fixture_read(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path.resolve() == committed_path:
            raise AssertionError("build_fixture read the committed fixture")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", reject_committed_fixture_read)
    monkeypatch.setattr(regenerator, "_COMMITTED_FIXTURE", tmp_path / "absent.json")

    generated = build_fixture()

    assert generated == expected
    validate_fixture_paths(generated, _load_json(_LEDGER_PATH))


def test_provenance_pins_authenticated_writer_bytes() -> None:
    provenance = _load_json(_PROVENANCE_PATH)

    assert isinstance(provenance, dict)
    assert provenance["sourceSha256"] == PINNED_SOURCE_SHA256


def test_regenerate_fixture_rejects_bytes_outside_pin_before_write(tmp_path: Path) -> None:
    source = tmp_path / "JsonMessageWriter.cs"
    source.write_text("altered writer", encoding="utf-8")
    output = tmp_path / "maximal-writer-shape.json"

    with pytest.raises(SourceMismatchError, match="does not match pinned SHA-256"):
        regenerate_fixture(source, output, _load_json(_LEDGER_PATH))

    assert not output.exists()


def test_regenerate_fixture_writes_scratch_artifact_after_source_check(tmp_path: Path) -> None:
    source = tmp_path / "JsonMessageWriter.cs"
    source.write_text("pinned writer test double", encoding="utf-8")
    output = tmp_path / "maximal-writer-shape.json"
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    regenerate_fixture(
        source,
        output,
        _load_json(_LEDGER_PATH),
        expected_source_sha256=expected_hash,
    )

    assert output.read_bytes() == _EXPORT_PATH.read_bytes()


def test_regenerate_fixture_rejects_generated_shape_missing_from_ledger(
    tmp_path: Path,
) -> None:
    source = tmp_path / "JsonMessageWriter.cs"
    source.write_text("pinned writer test double", encoding="utf-8")
    output = tmp_path / "maximal-writer-shape.json"
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(ledger, dict)
    ledger["paths"] = [path for path in ledger["paths"] if path != "messages[].interaction"]

    with pytest.raises(ValueError, match="ledger paths differ from generated fixture"):
        regenerate_fixture(
            source,
            output,
            ledger,
            expected_source_sha256=expected_hash,
        )

    assert not output.exists()
