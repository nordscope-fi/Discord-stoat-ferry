import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from discord_ferry.errors import ExportError
from discord_ferry.exporter.staging import (
    accept_stage,
    create_stage,
    discard_candidate,
    inspect_stage,
    seed_stage_media,
    validate_coverage,
)
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


def test_bounded_discard_keeps_prior_accepted_path(tmp_path: Path) -> None:
    accepted = create_stage(tmp_path, "accepted-a")
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    (accepted / "source.json").write_text(json.dumps(payload), encoding="utf-8")
    state = MigrationState(staged_input=inspect_stage(accepted, tmp_path, "full"))
    candidate = create_stage(tmp_path, "candidate-b")

    discard_candidate(candidate, tmp_path)

    assert state.validated_stage_path(tmp_path).name == "accepted-a"
    assert not candidate.exists()


def test_accept_stage_persists_before_retaining_only_two_stages(tmp_path: Path) -> None:
    stages = [create_stage(tmp_path, name) for name in ("old", "previous", "current")]
    payload = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    payload["channel"]["id"] = "1543572361285083187"
    for stage in stages:
        (stage / "source.json").write_text(json.dumps(payload), encoding="utf-8")
    state = MigrationState(staged_input=inspect_stage(stages[1], tmp_path, "full"))
    saved: list[MigrationState] = []

    def persist(snapshot: MigrationState) -> None:
        assert stages[0].is_dir()
        saved.append(snapshot)

    accept_stage(state, tmp_path, inspect_stage(stages[2], tmp_path, "full"), persist)

    assert saved == [state]
    assert state.validated_stage_path(tmp_path).name == "current"
    assert stages[1].is_dir()
    assert not stages[0].exists()


def test_retention_preserves_unowned_directory_below_stage_root(tmp_path: Path) -> None:
    previous = create_stage(tmp_path, "previous")
    current = create_stage(tmp_path, "current")
    superseded = create_stage(tmp_path, "superseded")
    unrelated = tmp_path / "staged-exports" / "user-folder"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("user-owned", encoding="utf-8")
    state = MigrationState(staged_input=inspect_stage(previous, tmp_path, "full"))

    accept_stage(state, tmp_path, inspect_stage(current, tmp_path, "full"), lambda _state: None)

    assert not superseded.exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "user-owned"


@pytest.mark.parametrize("marker_state", ["missing", "corrupt"])
def test_stage_operations_refuse_missing_or_corrupt_owner_marker(
    tmp_path: Path, marker_state: str
) -> None:
    stage = create_stage(tmp_path, "candidate")
    witness = inspect_stage(stage, tmp_path, "full")
    marker = stage / ".ferry-owned-stage"
    assert marker.is_file()
    if marker_state == "missing":
        marker.unlink()
    else:
        marker.write_text("not a Ferry owner marker", encoding="utf-8")

    with pytest.raises(ExportError, match="owned"):
        inspect_stage(stage, tmp_path, "full")
    with pytest.raises(ExportError, match="owned"):
        discard_candidate(stage, tmp_path)
    with pytest.raises(ExportError, match="owned"):
        accept_stage(MigrationState(), tmp_path, witness, lambda _state: None)
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

    assert Path(inspect_stage(accepted, output_root, "full").relative_path).parts == (
        "staged-exports",
        "candidate",
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


def test_seed_rejects_different_byte_collision(tmp_path: Path) -> None:
    cache = tmp_path / "dce_cache" / "1543572361285083187"
    stage = create_stage(tmp_path, "run-1")
    (cache / "media").mkdir(parents=True)
    (stage / "media").mkdir()
    (cache / "media" / "1543572361285083187-1.png").write_bytes(b"old")
    (stage / "media" / "1543572361285083187-1.png").write_bytes(b"new")

    assert seed_stage_media(cache, stage, eligible=True) is False


def test_seed_copies_contained_regular_media_files(tmp_path: Path) -> None:
    previous = create_stage(tmp_path, "previous")
    stage = create_stage(tmp_path, "run-1")
    (previous / "media" / "nested").mkdir(parents=True)
    (previous / "media" / "nested" / "image.png").write_bytes(b"image")

    assert seed_stage_media(previous, stage, eligible=True) is True
    assert (stage / "media" / "nested" / "image.png").read_bytes() == b"image"


def test_seed_copies_legacy_per_export_media_without_copying_json(tmp_path: Path) -> None:
    previous = tmp_path / "dce_cache" / "1543572361285083187"
    stage = create_stage(tmp_path, "run-1")
    previous.mkdir(parents=True)
    (previous / "legacy-general.json").write_bytes(b"json-input")
    assets = previous / "legacy-general.json_Files"
    assets.mkdir()
    (assets / "generated-name.png").write_bytes(b"generated-media")

    assert seed_stage_media(previous, stage, eligible=True) is True
    assert (stage / "media" / "generated-name.png").read_bytes() == b"generated-media"
    assert not (stage / "media" / "legacy-general.json").exists()


def test_seed_without_reusable_media_disables_reuse(tmp_path: Path) -> None:
    previous = tmp_path / "dce_cache" / "1543572361285083187"
    previous.mkdir(parents=True)
    (previous / "general.json").write_bytes(b"json-input")
    stage = create_stage(tmp_path, "run-1")

    assert seed_stage_media(previous, stage, eligible=True) is False
    assert not (stage / "media").exists()


def test_seed_preflights_different_bytes_from_legacy_directories(tmp_path: Path) -> None:
    previous = tmp_path / "dce_cache" / "1543572361285083187"
    stage = create_stage(tmp_path, "run-1")
    first = previous / "general.json_Files"
    second = previous / "thread.json_Files"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "generated-name.png").write_bytes(b"first")
    (second / "generated-name.png").write_bytes(b"second")

    assert seed_stage_media(previous, stage, eligible=True) is False
    assert not (stage / "media").exists()


def test_seed_refuses_linked_legacy_media_file(tmp_path: Path) -> None:
    previous = tmp_path / "dce_cache" / "1543572361285083187"
    stage = create_stage(tmp_path, "run-1")
    assets = previous / "general.json_Files"
    assets.mkdir(parents=True)
    external = tmp_path / "external.png"
    external.write_bytes(b"external")
    (assets / "generated-name.png").symlink_to(external)

    assert seed_stage_media(previous, stage, eligible=True) is False
    assert not (stage / "media").exists()


def test_seed_copy_error_disables_reuse_and_cancellation_propagates(tmp_path: Path) -> None:
    previous = create_stage(tmp_path, "previous")
    stage = create_stage(tmp_path, "run-1")
    (previous / "media").mkdir()
    (previous / "media" / "generated-name.png").write_bytes(b"media")

    with patch("discord_ferry.exporter.staging.shutil.copyfile", side_effect=OSError("copy")):
        assert seed_stage_media(previous, stage, eligible=True) is False

    with (
        patch(
            "discord_ferry.exporter.staging.shutil.copyfile",
            side_effect=asyncio.CancelledError,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        seed_stage_media(previous, stage, eligible=True)
