from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from discord_ferry.parser.dce_parser import parse_single_export, stream_messages

_ROOT = Path(__file__).parent.parent
_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "dce_2_48"
_CAPTURED = _FIXTURE_ROOT / "captured"
_PROVENANCE = _CAPTURED / "provenance.json"
_MANIFEST = Path(__file__).parent / "provisioning" / "fixture-spec.json"
_TOKEN_PATTERNS = (
    re.compile(r"mfa\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r'(?i)authorization\s*[":=]+\s*(?:bot|bearer)\s+\S+'),
)
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"/Users/"),
    re.compile(r"/home/"),
    re.compile(r"[A-Za-z]:\\\\"),
)


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _provenance() -> dict[str, object]:
    value = _load(_PROVENANCE)
    assert isinstance(value, dict)
    return value


def _captured_exports() -> list[Path]:
    return sorted(path for path in _CAPTURED.glob("*.json") if path.name != "provenance.json")


def test_captured_provenance_records_the_verified_run() -> None:
    provenance = _provenance()
    assert provenance == {
        "evidenceClass": "captured",
        "dceVersion": "2.48",
        "releaseTarget": "osx-arm64",
        "releaseSha256": "623f9d2dce568e17a46b8fbd366a18dca49803d386216f4ba24507d2c000fee9",
        "captureDate": "2026-08-30",
        "fixtureManifestVersion": 1,
        "fixtureVerifyExitCode": 0,
        "dceExitCode": 0,
        "redactionReview": "passed",
        "guildId": "1505988963628879902",
        "applicationId": "1505990747130953738",
        "applicationFlagsBefore": 0,
        "applicationFlagsDuringCapture": 524288,
        "applicationFlagsRestored": 0,
        "includedChannelIds": [
            "1543572361285083187",
            "1543572368977428611",
            "1543572393384087642",
        ],
        "excludedChannelIds": [
            "1505988964585050175",
            "1505988964585050176",
        ],
        "exclusionReason": "not part of the fixture manifest",
    }


def test_every_dce_2_48_fixture_has_one_evidence_class() -> None:
    evidence_classes: list[str] = []
    for directory in sorted(path for path in _FIXTURE_ROOT.iterdir() if path.is_dir()):
        provenance = _load(directory / "provenance.json")
        assert isinstance(provenance, dict)
        evidence_class = provenance.get("evidenceClass")
        assert evidence_class in {"captured", "source-derived"}
        evidence_classes.append(str(evidence_class))

    assert evidence_classes.count("captured") == 1
    assert evidence_classes.count("source-derived") == 1


def test_captured_text_has_no_credentials_or_absolute_paths() -> None:
    text_files = sorted(_CAPTURED.rglob("*.json"))
    assert text_files
    for path in text_files:
        text = path.read_text(encoding="utf-8")
        for pattern in (*_TOKEN_PATTERNS, *_ABSOLUTE_PATH_PATTERNS):
            assert pattern.search(text) is None, f"unsafe text in {path.name}: {pattern.pattern}"


def test_captured_exports_match_provenance_and_replay_in_both_modes() -> None:
    provenance = _provenance()
    exports = _captured_exports()
    assert len(exports) == 3

    channel_ids: set[str] = set()
    for path in exports:
        eager = parse_single_export(path)
        streamed = list(stream_messages(path))
        assert eager.messages == streamed
        assert eager.message_count == len(eager.messages)
        assert eager.guild.id == provenance["guildId"]
        channel_ids.add(eager.channel.id)

    assert channel_ids == set(provenance["includedChannelIds"])


def test_capture_contains_exact_manifest_markers_and_only_fixture_authors() -> None:
    manifest = _load(_MANIFEST)
    assert isinstance(manifest, dict)
    expected_markers = {
        message["id"] for channel in manifest["text_channels"] for message in channel["messages"]
    }
    expected_markers.update(thread["id"] for thread in manifest["threads"])
    expected_markers.update(
        post["id"] for channel in manifest["forum_channels"] for post in channel["posts"]
    )

    actual_markers: set[str] = set()
    for path in _captured_exports():
        raw = _load(path)
        assert isinstance(raw, dict)
        for message in raw["messages"]:
            assert message["author"]["id"] == "1505990747130953738"
            markers = re.findall(r"\[ferry:([^\]]+)\]", message["content"])
            if markers:
                actual_markers.update(markers)
            else:
                assert message["type"] == "ThreadCreated"
                assert message["content"] == "Started a thread."

    assert actual_markers == expected_markers


def test_every_local_media_reference_stays_inside_capture() -> None:
    for path in _captured_exports():
        raw = _load(path)
        pending = [raw]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    pending.append(child)
                    if (
                        key.lower().endswith("url")
                        and isinstance(child, str)
                        and child
                        and not child.startswith(("http://", "https://"))
                    ):
                        media = (_CAPTURED / child).resolve()
                        assert media.is_relative_to(_CAPTURED.resolve())
                        assert media.is_file(), child
            elif isinstance(value, list):
                pending.extend(value)


@pytest.mark.parametrize("needle", ["provision_test_server", "dce_2_48/captured"])
def test_ci_does_not_run_live_fixture_operations(needle: str) -> None:
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (_ROOT / ".github" / "workflows").glob("*")
    )
    assert needle not in workflow_text
