import json
from pathlib import Path

import pytest

from discord_ferry.errors import ExportError
from discord_ferry.exporter.staging import create_stage, inspect_stage, validate_coverage
from discord_ferry.state import MigrationState


def test_create_stage_is_contained(tmp_path: Path) -> None:
    stage = create_stage(tmp_path, "run-1")
    assert stage == tmp_path / "staged-exports" / "run-1"
    assert stage.is_dir()
    owner_entries = tuple(stage.iterdir())
    assert len(owner_entries) == 1
    assert owner_entries[0].name == ".ferry-owned-stage"
    assert owner_entries[0].is_file()


def test_empty_channel_json_is_present_in_witness(tmp_path: Path) -> None:
    stage = create_stage(tmp_path, "run-1")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    payload["messages"] = []
    (stage / "changed-name.json").write_text(json.dumps(payload), encoding="utf-8")

    witness = inspect_stage(stage, tmp_path, "bounded")

    assert witness.source_ids == ("1543572361285083187",)
    assert validate_coverage(witness, witness.source_ids) is True


def test_stage_rejects_duplicate_source_json_and_invalid_json(tmp_path: Path) -> None:
    stage = create_stage(tmp_path, "run-1")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    (stage / "one.json").write_text(json.dumps(payload), encoding="utf-8")
    (stage / "two.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExportError, match="Duplicate"):
        inspect_stage(stage, tmp_path, "full")

    (stage / "two.json").write_text("not JSON", encoding="utf-8")
    with pytest.raises(ExportError, match="Invalid"):
        inspect_stage(stage, tmp_path, "full")


@pytest.mark.parametrize("invalid_shape", ["missing-guild", "missing-channel-name"])
def test_stage_inspection_refuses_semantically_invalid_dce_metadata(
    tmp_path: Path, invalid_shape: str
) -> None:
    stage = create_stage(tmp_path, "run-1")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    payload["messages"] = []
    payload["messageCount"] = 0
    if invalid_shape == "missing-guild":
        del payload["guild"]
    else:
        del payload["channel"]["name"]
    (stage / "invalid-dce.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExportError, match="invalid-dce.json"):
        inspect_stage(stage, tmp_path, "full")


def test_stage_inspection_refuses_malformed_json_after_message_metadata(tmp_path: Path) -> None:
    stage = create_stage(tmp_path, "run-1")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    encoded = json.dumps(payload)
    (stage / "malformed-tail.json").write_text(encoded[:-1], encoding="utf-8")

    with pytest.raises(ExportError, match="malformed-tail.json"):
        inspect_stage(stage, tmp_path, "bounded")


def test_stage_inspection_does_not_load_the_whole_json_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = create_stage(tmp_path, "run-1")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    json_path = stage / "streamed.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    original_read_text = Path.read_text

    def refuse_json_read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path.suffix == ".json":
            raise AssertionError("stage inspection loaded the whole JSON document")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", refuse_json_read_text)

    assert inspect_stage(stage, tmp_path, "bounded").source_ids == ("1543572361285083187",)


@pytest.mark.parametrize("marker_state", ["missing", "corrupt"])
def test_stage_operations_refuse_missing_or_corrupt_owner_marker(
    tmp_path: Path, marker_state: str
) -> None:
    stage = create_stage(tmp_path, "candidate")
    marker = stage / ".ferry-owned-stage"
    assert marker.is_file()
    if marker_state == "missing":
        marker.unlink()
    else:
        marker.write_text("not a Ferry owner marker", encoding="utf-8")

    with pytest.raises(ExportError, match="owned"):
        inspect_stage(stage, tmp_path, "full")
    assert stage.is_dir()


def test_stage_validation_allows_linked_ancestor_above_output_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    (actual / "nested").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    output_root = alias / "nested" / "output"

    stage = create_stage(output_root, "candidate")

    witness = inspect_stage(stage, output_root, "full")
    accepted = MigrationState(staged_input=witness).validated_stage_path(output_root)

    assert inspect_stage(accepted, output_root, "full").relative_path == (
        "staged-exports/candidate"
    )


def test_stage_validation_refuses_link_below_output_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    stage = create_stage(output_root, "candidate")
    marker = stage / ".ferry-owned-stage"
    assert marker.is_file()
    marker.unlink()
    external_marker = tmp_path / "external-marker"
    external_marker.write_text("ferry-owned-stage-v1:candidate\n", encoding="utf-8")
    marker.symlink_to(external_marker)

    with pytest.raises(ExportError, match="owned|Linked"):
        inspect_stage(stage, output_root, "full")
