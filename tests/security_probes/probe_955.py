"""Validation probe for audit issue #955 (export-folder path containment).

Runs inside an OS-enforced sandbox: no network, scratch-only writes, empty
allowlisted environment. Dummy data only; uploads and sends are mocks.
Exit code 0 means zero escapes reached the upload boundary and every
in-folder control uploaded; exit code 1 means regression.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.avatars import run_avatars
from discord_ferry.migrator.messages import _process_message, _resolve_attachment_path
from discord_ferry.parser.dce_parser import stream_messages
from discord_ferry.state import MigrationState

SCRATCH = Path(os.environ["PROBE_SCRATCH"])
EXPORT = (SCRATCH / "export").resolve()
MARKER = (SCRATCH / "outside-marker.txt").resolve()
RECORDS: list[dict[str, str]] = []
VARIANTS = {
    "parent": "../outside-marker.txt",
    "absolute": str(MARKER),
    "symlink": "link-marker.txt",
    "control": "inside/normal.png",
}
VARIANT_KEY = {v: k for k, v in VARIANTS.items()}


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
    return f"autumn-dummy-{len(RECORDS)}"


async def fake_send(
    session: object, stoat_url: str, token: str, channel_id: str, **kw: object
) -> dict[str, str]:
    return {"_id": "dummy-stoat-msg"}


def noop(event: object) -> None:
    pass


def setup_fixtures() -> None:
    (EXPORT / "inside").mkdir(parents=True, exist_ok=True)
    (EXPORT / "inside" / "normal.png").write_bytes(b"normal-in-folder-media")
    MARKER.write_text("MARKER-955-OUTSIDE-CONTENT")
    link = EXPORT / "link-marker.txt"
    if not link.is_symlink():
        link.symlink_to("../outside-marker.txt")


def write_case(name: str, variant: str) -> Path:
    msg: dict[str, object] = {
        "id": f"1000-{name}",
        "type": "Default",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "content": "probe",
        "author": {"id": "42", "name": "ProbeAuthor", "avatarUrl": ""},
        "attachments": [],
        "stickers": [],
        "embeds": [],
    }
    if name == "attachment":
        msg["attachments"] = [
            {"id": "a1", "fileName": "x.png", "url": variant, "fileSizeBytes": 10}
        ]
    elif name == "sticker":
        msg["stickers"] = [{"name": "s", "sourceUrl": variant}]
    elif name == "embed":
        msg["embeds"] = [{"title": "t", "thumbnail": {"url": variant}}]
    elif name in ("masquerade", "avatar-phase"):
        author = msg["author"]
        assert isinstance(author, dict)
        author["avatarUrl"] = variant
    path = SCRATCH / f"case-{name}-{VARIANT_KEY[variant]}.json"
    path.write_text(json.dumps({"messages": [msg]}))
    return path


def make_config() -> FerryConfig:
    return FerryConfig(
        export_dir=EXPORT,
        stoat_url="http://stoat.invalid",
        token="dummy-token",
        output_dir=SCRATCH / "out",
        message_rate_limit=0.0,
        upload_delay=0.0,
        pause_event=asyncio.Event(),
        cancel_event=asyncio.Event(),
    )


def make_state() -> MigrationState:
    state = MigrationState()
    state.autumn_url = "http://autumn.invalid"
    return state


def classify() -> dict[str, list[dict[str, str]]]:
    escaped = [r for r in RECORDS if not Path(r["resolved"]).is_relative_to(EXPORT)]
    inside = [r for r in RECORDS if Path(r["resolved"]).is_relative_to(EXPORT)]
    return {"escaped": escaped, "inside": inside}


async def run_message_case(name: str, variant: str) -> dict[str, object]:
    case_file = write_case(name, variant)
    msg = next(iter(stream_messages(case_file)))
    RECORDS.clear()
    error = ""
    try:
        await _process_message(
            msg=msg,
            stoat_channel_id="dummy-channel",
            config=make_config(),
            state=make_state(),
            session=None,
            on_event=noop,
            export_channel_id="c1",
            idempotency_salt="",
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    out: dict[str, object] = {
        "case": name,
        "variant": VARIANT_KEY.get(variant, variant),
        "error": error,
    }
    out.update(classify())
    return out


async def run_avatar_phase(variant: str) -> dict[str, object]:
    case_file = write_case("avatar-phase", variant)
    RECORDS.clear()
    error = ""
    exports = [SimpleNamespace(json_path=case_file, messages=[])]
    try:
        await run_avatars(make_config(), make_state(), exports, noop)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    out: dict[str, object] = {
        "case": "avatar-phase",
        "variant": VARIANT_KEY.get(variant, variant),
        "error": error,
    }
    out.update(classify())
    return out


async def main() -> int:
    setup_fixtures()
    direct = []
    for key, variant in VARIANTS.items():
        path = _resolve_attachment_path(EXPORT, variant)
        contained = path is not None and path.resolve().is_relative_to(EXPORT)
        direct.append({"variant": key, "returned": str(path), "contained": contained})
    results: list[dict[str, object]] = []
    with (
        patch("discord_ferry.migrator.messages.upload_with_cache", record_upload),
        patch("discord_ferry.migrator.avatars.upload_with_cache", record_upload),
        patch("discord_ferry.migrator.messages.api_send_message", fake_send),
    ):
        for name in ("attachment", "sticker", "embed", "masquerade"):
            for variant in VARIANTS.values():
                results.append(await run_message_case(name, variant))
        for variant in VARIANTS.values():
            results.append(await run_avatar_phase(variant))
    escapes = [
        {"case": r["case"], "variant": r["variant"], "escaped": r["escaped"]}
        for r in results
        if r["escaped"]
    ]
    controls = [
        {"case": r["case"], "variant": r["variant"], "inside": len(r["inside"])}
        for r in results
        if r["variant"] == "control"
    ]
    summary = {
        "issue": 955,
        "export_root": str(EXPORT),
        "marker": str(MARKER),
        "direct_resolve_attachment_path": direct,
        "results": results,
        "escapes_reaching_upload": escapes,
        "control_inside_root": controls,
    }
    (SCRATCH / "result-955.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    failed = bool(escapes)
    failed = failed or any(c["inside"] == 0 for c in controls)
    failed = failed or any(d["returned"] != "None" for d in direct if d["variant"] != "control")
    failed = failed or any(not d["contained"] for d in direct if d["variant"] == "control")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
