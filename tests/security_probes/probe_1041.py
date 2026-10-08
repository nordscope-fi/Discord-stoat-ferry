"""Bounded runtime probe for audit issue #1041 (guild icon path containment).

Target: src/discord_ferry/migrator/structure.py, run_server, the guild icon
block at lines 191-205. It does Path(icon_url) with no export-root join and no
containment check, unlike the seven other media read sites which all go through
contained_media_path.

Two claims are tested:
  ESCAPE     an export-supplied iconUrl that is absolute or a parent traversal
             is read from outside the export root and handed to the uploader.
  FUNCTIONAL a realistic export-relative iconUrl ("media/guild_icon.png") never
             resolves, because Path() resolves it against the process cwd, so
             guild icons are not migrated from genuine DCE exports.

Runs under sandbox-exec: no network, scratch-only writes, empty env. Dummy data
only; every API call and the uploader are mocks.

Exit 0 means zero escapes reached the mocked upload boundary and the realistic
export-relative spelling uploaded, which is the healthy post-fix state. Exit 1
means regression: an escape reproduced, or the realistic spelling failed to
upload from a foreign working directory. This matches the convention the
harness README documents for run-955.sh and run-956.sh.

Run it with run-1041.sh, which renders the shared sandbox-955.sb profile and
applies the controls. There is deliberately no sandbox-1041.sb; run-956.sh
already renders the first probe's profile, and a third copy of a 13-line generic
profile would trace to nothing.

Read the verdicts as "escape reproduced", not as "probe passed". Against fixed
source a healthy run reports escape_verdict REFUTED and functional_verdict
REFUTED, and the in-export control case still uploads. Against pre-fix source
both read CONFIRMED. The run that confirmed issue #1041 on 2026-10-07 was
against pre-fix source, and its verdict is posted to that issue.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.structure import run_server
from discord_ferry.parser.media_paths import contained_media_path
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild
from discord_ferry.state import MigrationState

SCRATCH = Path(os.environ["PROBE_SCRATCH"]).resolve()
EXPORT = SCRATCH / "export"
CWD_DIR = SCRATCH / "cwd"
MARKER = SCRATCH / "outside-marker.txt"
MARKER_TEXT = "MARKER-1041-OUTSIDE-CONTENT"
RECORDS: list[dict[str, str]] = []


class _FakeSessionCtx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def record_upload(
    session: object,
    autumn_url: str,
    tag: str,
    path: Path,
    token: str,
    cache: dict[str, str],
    delay: float,
    **kw: object,
) -> str:
    target = Path(path)
    entry = {"tag": tag, "path": str(target), "resolved": str(target.resolve())}
    try:
        entry["first_bytes"] = target.read_bytes()[:64].decode("utf-8", "replace")
    except OSError as exc:
        entry["read_error"] = str(exc)
    RECORDS.append(entry)
    return "autumn-dummy-icon"


def make_export(icon_url: str) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="111", name="ProbeGuild", icon_url=icon_url),
        channel=DCEChannel(
            id="222", type=0, name="general", category_id="cat1", category="General"
        ),
        messages=[],
        message_count=0,
        is_thread=False,
        parent_channel_name="",
    )


def setup_fixtures() -> None:
    (EXPORT / "media").mkdir(parents=True, exist_ok=True)
    CWD_DIR.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "out").mkdir(parents=True, exist_ok=True)
    (EXPORT / "media" / "guild_icon.png").write_bytes(b"real-in-export-guild-icon")
    MARKER.write_text(MARKER_TEXT)


async def drive(icon_url: str, cwd: Path) -> None:
    """Run the real run_server against one iconUrl spelling, from one cwd."""
    RECORDS.clear()
    config = FerryConfig(
        export_dir=EXPORT,
        stoat_url="https://stoat.invalid",
        token="dummy-token",
        output_dir=SCRATCH / "out",
        server_id="01JPROBESRV0000000000AAA",
    )
    state = MigrationState(stoat_server_id="01JPROBESRV0000000000AAA")
    os.chdir(cwd)
    with (
        patch("discord_ferry.migrator.structure.get_session", lambda *_a: _FakeSessionCtx()),
        patch(
            "discord_ferry.migrator.structure.api_fetch_server",
            new=AsyncMock(return_value={"_id": "01JPROBESRV0000000000AAA"}),
        ),
        patch("discord_ferry.migrator.structure.save_state", lambda *_a, **_k: None),
        patch("discord_ferry.migrator.structure.upload_with_cache", new=record_upload),
        patch("discord_ferry.migrator.structure.api_edit_server", new=AsyncMock()),
    ):
        await run_server(config, state, [make_export(icon_url)], lambda _e: None)


def verdicts(results: dict[str, dict[str, object]]) -> tuple[bool, bool]:
    escaped = any(r["marker_reached_upload"] for r in results.values())  # type: ignore[index]
    realistic_broken = (
        not results["realistic_from_repo_cwd"]["uploaded"]  # type: ignore[index]
        and bool(results["realistic_from_export_cwd"]["uploaded"])  # type: ignore[index]
    )
    return escaped, realistic_broken


async def main() -> int:
    setup_fixtures()
    cases = {
        "absolute": (str(MARKER), CWD_DIR),
        "traversal": ("../outside-marker.txt", CWD_DIR),
        "realistic_from_repo_cwd": ("media/guild_icon.png", CWD_DIR),
        "realistic_from_export_cwd": ("media/guild_icon.png", EXPORT),
    }
    results: dict[str, dict[str, object]] = {}
    for name, (icon_url, cwd) in cases.items():
        await drive(icon_url, cwd)
        uploaded = len(RECORDS) > 0
        resolved = RECORDS[0]["resolved"] if uploaded else None
        first = RECORDS[0].get("first_bytes", "") if uploaded else ""
        results[name] = {
            "icon_url": icon_url,
            "cwd": str(cwd),
            "uploaded": uploaded,
            "resolved": resolved,
            "first_bytes": first,
            "marker_reached_upload": bool(uploaded) and MARKER_TEXT in str(first),
            "contained_media_path_says": str(contained_media_path(EXPORT, icon_url)),
        }

    escaped, realistic_broken = verdicts(results)
    report = {
        "issue": 1041,
        "target": "src/discord_ferry/migrator/structure.py run_server guild icon block",
        "cases": results,
        "verdict": {
            "escape_reproduced": escaped,
            "escape_verdict": "CONFIRMED" if escaped else "REFUTED",
            "realistic_icon_never_uploads": realistic_broken,
            "functional_verdict": "CONFIRMED" if realistic_broken else "REFUTED",
        },
    }
    out = SCRATCH / "result-1041.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["verdict"], indent=2))
    for name, r in results.items():
        print(f"  {name:26s} uploaded={str(r['uploaded']):5s} marker={r['marker_reached_upload']}")
        print(f"    {'':26s} icon_url={r['icon_url']}")
        print(f"    {'':26s} contained_media_path={r['contained_media_path_says']}")
    print(f"\nwrote {out}")
    # The harness contract is exit 1 on regression, so a reproduced escape or a
    # realistic icon that failed to upload must not exit 0.
    return 1 if (escaped or realistic_broken) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
