"""Validation probe for audit issue #956 (avatar phase writes).

Runs inside the same OS-enforced sandbox as probe_955: no network,
scratch-only writes, empty allowlisted environment. No service is contacted.
The avatars phase no longer downloads or writes any file (#1208), so this probe
now checks that a crafted author id still leaves the outside marker
byte-identical and creates nothing outside the scratch case files. Exit code 0
means no write escaped; exit code 1 means regression.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.avatars import run_avatars
from discord_ferry.state import MigrationState

SCRATCH = Path(os.environ["PROBE_SCRATCH"]) / "v956"
OUT = (SCRATCH / "out").resolve()
AVATARS = (OUT / "avatars").resolve()
MARKER = (SCRATCH / "outside-marker.png").resolve()
ORIGINAL = "MARKER-956-ORIGINAL"


def noop(event: object) -> None:
    pass


def setup() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(ORIGINAL)


def writes_outside_avatars() -> list[str]:
    out = []
    for path in SCRATCH.rglob("*"):
        if path.is_file() and not path.is_symlink():
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(AVATARS)
                and resolved != MARKER
                and not path.name.startswith("case-")
                and path.name not in ("probe_956.py", "result-956.json")
            ):
                out.append(str(resolved))
    return out


def write_case(author_id: str) -> Path:
    msg = {
        "id": "2000",
        "type": "Default",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "content": "probe",
        "author": {
            "id": author_id,
            "name": "ProbeAuthor",
            "avatarUrl": "https://cdn.discordapp.com/a.png",
        },
        "attachments": [],
        "stickers": [],
        "embeds": [],
    }
    path = SCRATCH / f"case-{author_id.replace('/', '_') or 'control'}.json"
    path.write_text(json.dumps({"messages": [msg]}))
    return path


async def phase_download(author_id: str) -> dict[str, object]:
    MARKER.write_text(ORIGINAL)
    before = MARKER.read_text()
    case_file = write_case(author_id)
    state = MigrationState()
    state.autumn_url = "http://autumn.invalid"
    config = FerryConfig(
        export_dir=SCRATCH / "export",
        stoat_url="http://stoat.invalid",
        token="dummy-token",
        output_dir=OUT,
        message_rate_limit=0.0,
        upload_delay=0.0,
        pause_event=asyncio.Event(),
        cancel_event=asyncio.Event(),
    )
    error = ""
    try:
        exports = [SimpleNamespace(json_path=case_file, messages=[])]
        await run_avatars(config, state, exports, noop)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    after = MARKER.read_text()
    return {
        "case": "avatar-phase",
        "author_id": author_id,
        "error": error,
        "marker_before": before,
        "marker_after": after,
        "marker_overwritten": after != ORIGINAL,
        "writes_outside_avatars_root": writes_outside_avatars(),
    }


async def main() -> int:
    setup()
    results: list[dict[str, object]] = []
    for author_id in ("../../outside-marker", "1234567890"):
        results.append(await phase_download(author_id))
    escapes = [
        r for r in results if r.get("marker_overwritten") or r.get("writes_outside_avatars_root")
    ]
    summary = {
        "issue": 956,
        "avatars_root": str(AVATARS),
        "marker": str(MARKER),
        "results": results,
        "escapes": escapes,
    }
    (SCRATCH / "result-956.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
