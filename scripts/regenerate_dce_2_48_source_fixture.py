#!/usr/bin/env python3
"""Build a DCE 2.48 source-derived reconstruction from reviewed dummy values."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import NoReturn

PINNED_SOURCE_COMMIT = "905489b01b5523719082275c34c232fc47f827c6"
PINNED_SOURCE_SHA256 = "2b7d17c45f95c68437c598eaa2dec6b17317cc32dbc1416d4e02ab7c9a675127"
PINNED_SOURCE_FILE = "DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "dce_2_48" / "source-derived"
_COMMITTED_FIXTURE = _FIXTURE_DIR / "maximal-writer-shape.json"
_LEDGER = _FIXTURE_DIR / "field-dispositions.json"
_PROVENANCE = _FIXTURE_DIR / "provenance.json"


class SourceMismatchError(ValueError):
    """The supplied writer bytes are not the reviewed pinned source."""


def _role(*, role_id: str, name: str, color: str, position: int) -> dict[str, object]:
    return {"id": role_id, "name": name, "color": color, "position": position}


def _user(
    *,
    user_id: str,
    name: str,
    nickname: str,
    color: str,
    avatar: str,
    roles: list[dict[str, object]] | None,
) -> dict[str, object]:
    user: dict[str, object] = {
        "id": user_id,
        "name": name,
        "discriminator": "0000",
        "nickname": nickname,
        "color": color,
        "isBot": False,
    }
    if roles is not None:
        user["roles"] = roles
    user["avatarUrl"] = avatar
    return user


def _emoji(*, emoji_id: str, name: str, image: str) -> dict[str, object]:
    return {
        "id": emoji_id,
        "name": name,
        "code": f"<:{name}:{emoji_id}>",
        "isAnimated": False,
        "imageUrl": image,
    }


def _attachment(*, attachment_id: str, url: str, file_name: str, size: int) -> dict[str, object]:
    return {
        "id": attachment_id,
        "url": url,
        "fileName": file_name,
        "fileSizeBytes": size,
    }


def _image(*, stem: str, canonical_stem: str, width: int, height: int) -> dict[str, object]:
    return {
        "url": f"media/{stem}.png",
        "canonicalUrl": f"https://cdn.example.invalid/{canonical_stem}.png",
        "width": width,
        "height": height,
    }


def _video(*, stem: str) -> dict[str, object]:
    return {
        "url": f"https://media.example.invalid/{stem}.mp4",
        "canonicalUrl": f"https://cdn.example.invalid/{stem}.mp4",
        "width": 1280,
        "height": 720,
    }


def _embed(
    *,
    title: str,
    url_stem: str,
    timestamp: str,
    description: str,
    color: str,
    author_name: str,
    author_stem: str,
    media_prefix: str,
    thumbnail_size: tuple[int, int],
    footer_text: str,
    field_name: str,
    field_value: str,
    field_inline: bool,
    emoji_id: str,
    emoji_name: str,
) -> dict[str, object]:
    prefix = f"{media_prefix}-" if media_prefix else ""
    return {
        "title": title,
        "url": f"https://example.invalid/{url_stem}",
        "timestamp": timestamp,
        "description": description,
        "color": color,
        "author": {
            "name": author_name,
            "url": f"https://example.invalid/{author_stem}",
            "iconUrl": f"media/{prefix}author.png" if prefix else "media/embed-author.png",
            "iconCanonicalUrl": f"https://cdn.example.invalid/{prefix}author.png"
            if prefix
            else "https://cdn.example.invalid/embed-author.png",
        },
        "thumbnail": _image(
            stem=f"{prefix}thumbnail" if prefix else "thumbnail",
            canonical_stem=f"{prefix}thumbnail" if prefix else "thumbnail",
            width=thumbnail_size[0],
            height=thumbnail_size[1],
        ),
        "image": _image(
            stem=f"{prefix}image" if prefix else "image",
            canonical_stem=f"{prefix}image" if prefix else "image",
            width=640,
            height=360,
        ),
        "video": _video(stem=f"{prefix}video" if prefix else "video"),
        "footer": {
            "text": footer_text,
            "iconUrl": f"media/{prefix}footer.png" if prefix else "media/footer.png",
            "iconCanonicalUrl": f"https://cdn.example.invalid/{prefix}footer.png"
            if prefix
            else "https://cdn.example.invalid/footer.png",
        },
        "images": [
            _image(
                stem=f"{prefix}gallery" if prefix else "gallery",
                canonical_stem=f"{prefix}gallery" if prefix else "gallery",
                width=800,
                height=600,
            )
        ],
        "fields": [{"name": field_name, "value": field_value, "isInline": field_inline}],
        "inlineEmojis": [
            _emoji(
                emoji_id=emoji_id,
                name=emoji_name,
                image=f"media/{emoji_name}.png",
            )
        ],
    }


def _sticker(*, sticker_id: str, name: str, image: str) -> dict[str, object]:
    return {"id": sticker_id, "name": name, "format": "Png", "sourceUrl": image}


def build_fixture() -> dict[str, object]:
    """Build the maximal shape with invented values reviewed against the pinned writer."""
    maintainer = _role(role_id="500000000000000001", name="Maintainer", color="#ff8800", position=7)
    member = _role(role_id="500000000000000002", name="Member", color="#00aa88", position=2)
    alice = _user(
        user_id="400000000000000001",
        name="alice",
        nickname="Alice",
        color="#336699",
        avatar="media/alice-avatar.png",
        roles=[maintainer],
    )
    bob = _user(
        user_id="400000000000000002",
        name="bob",
        nickname="Bob",
        color="#00aa88",
        avatar="media/bob-avatar.png",
        roles=[member],
    )
    bob_without_roles = _user(
        user_id="400000000000000002",
        name="bob",
        nickname="Bob",
        color="#00aa88",
        avatar="media/bob-avatar.png",
        roles=None,
    )

    primary_embed = _embed(
        title="Poll closed",
        url_stem="poll",
        timestamp="2026-08-30T10:00:00+00:00",
        description="Favourite colour? <:chart:900000000000000002>",
        color="#5865f2",
        author_name="Poll bot",
        author_stem="bot",
        media_prefix="",
        thumbnail_size=(320, 180),
        footer_text="60 votes",
        field_name="Red",
        field_value="42 votes",
        field_inline=True,
        emoji_id="900000000000000002",
        emoji_name="chart",
    )
    forwarded_embed = _embed(
        title="Forwarded embed",
        url_stem="forwarded",
        timestamp="2026-08-29T09:00:00+00:00",
        description="Forwarded description <:forward:900000000000000004>",
        color="#112233",
        author_name="Forward author",
        author_stem="forward-author",
        media_prefix="forward",
        thumbnail_size=(160, 90),
        footer_text="Forward footer",
        field_name="Forward field",
        field_value="Forward value",
        field_inline=False,
        emoji_id="900000000000000004",
        emoji_name="forward",
    )

    message: dict[str, object] = {
        "id": "300000000000000001",
        "type": "PollResult",
        "timestamp": "2026-08-30T10:00:00+00:00",
        "timestampEdited": "2026-08-30T10:05:00+00:00",
        "callEndedTimestamp": "2026-08-30T10:10:00+00:00",
        "isPinned": True,
        "content": "Alice's poll Favourite colour? has closed. "
        "<@400000000000000002> <:wave:900000000000000001>",
        "author": alice,
        "attachments": [
            _attachment(
                attachment_id="600000000000000001",
                url="media/report.txt",
                file_name="report.txt",
                size=128,
            )
        ],
        "embeds": [primary_embed],
        "stickers": [
            _sticker(
                sticker_id="700000000000000001",
                name="Approved",
                image="media/approved.png",
            )
        ],
        "reactions": [
            {
                "emoji": _emoji(
                    emoji_id="900000000000000003",
                    name="agree",
                    image="media/agree.png",
                ),
                "count": 2,
                "users": [bob_without_roles],
            }
        ],
        "mentions": [bob],
        "reference": {
            "type": "Default",
            "messageId": "300000000000000000",
            "channelId": "200000000000000001",
            "guildId": "100000000000000001",
        },
        "forwardedMessage": {
            "timestamp": "2026-08-29T09:00:00+00:00",
            "timestampEdited": "2026-08-29T09:01:00+00:00",
            "content": "Forwarded contract content",
            "attachments": [
                _attachment(
                    attachment_id="600000000000000002",
                    url="media/forwarded.txt",
                    file_name="forwarded.txt",
                    size=64,
                )
            ],
            "embeds": [forwarded_embed],
            "stickers": [
                _sticker(
                    sticker_id="700000000000000002",
                    name="Forwarded",
                    image="media/forwarded-sticker.png",
                )
            ],
        },
        "interaction": {"id": "800000000000000001", "name": "close-poll", "user": bob},
        "inlineEmojis": [
            _emoji(emoji_id="900000000000000001", name="wave", image="media/wave.png")
        ],
    }

    return {
        "guild": {
            "id": "100000000000000001",
            "name": "Contract Guild",
            "iconUrl": "media/guild-icon.png",
        },
        "channel": {
            "id": "200000000000000001",
            "type": "GuildTextChat",
            "categoryId": "200000000000000000",
            "category": "Contract Category",
            "name": "contract-channel",
            "topic": "Source-derived DCE writer contract",
            "iconUrl": "media/channel-icon.png",
        },
        "dateRange": {
            "after": "2026-08-01T00:00:00+00:00",
            "before": "2026-08-31T00:00:00+00:00",
        },
        "exportedAt": "2026-08-30T12:00:00+00:00",
        "messages": [message],
        "messageCount": 1,
    }


def _json_paths(value: object, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        paths = {prefix} if prefix else set()
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            paths.update(_json_paths(child, child_prefix))
        return paths
    if isinstance(value, list):
        path = f"{prefix}[]"
        paths = {path}
        for child in value:
            paths.update(_json_paths(child, path))
        return paths
    return {prefix}


def verify_source(source: Path, *, expected_sha256: str = PINNED_SOURCE_SHA256) -> None:
    """Reject source bytes that differ from the reviewed pinned writer."""
    actual_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SourceMismatchError(
            f"{source} does not match pinned SHA-256: expected {expected_sha256}, "
            f"got {actual_sha256}"
        )


def validate_fixture_paths(fixture: object, ledger: object) -> None:
    """Check that generated paths match the reviewed disposition ledger."""
    if not isinstance(ledger, dict) or not isinstance(ledger.get("paths"), list):
        raise ValueError("ledger must contain a paths list")
    generated_paths = _json_paths(fixture)
    ledger_paths = set(ledger["paths"])
    if generated_paths != ledger_paths:
        missing = sorted(generated_paths - ledger_paths)
        extra = sorted(ledger_paths - generated_paths)
        raise ValueError(
            "ledger paths differ from generated fixture: "
            f"missing dispositions={missing}, paths absent from fixture={extra}"
        )


def _render_fixture(fixture: object) -> str:
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def _validated_fixture(
    source: Path, ledger: object, *, expected_source_sha256: str
) -> dict[str, object]:
    verify_source(source, expected_sha256=expected_source_sha256)
    fixture = build_fixture()
    validate_fixture_paths(fixture, ledger)
    return fixture


def regenerate_fixture(
    source: Path,
    output: Path,
    ledger: object,
    *,
    expected_source_sha256: str = PINNED_SOURCE_SHA256,
) -> None:
    """Authenticate the writer, validate paths, and write the reconstruction."""
    fixture = _validated_fixture(source, ledger, expected_source_sha256=expected_source_sha256)
    output.write_text(_render_fixture(fixture), encoding="utf-8")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_metadata() -> None:
    provenance = _load_json(_PROVENANCE)
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be a JSON object")
    expected = {
        "evidenceClass": "source-derived",
        "sourceCommit": PINNED_SOURCE_COMMIT,
        "sourceFile": PINNED_SOURCE_FILE,
        "sourceSha256": PINNED_SOURCE_SHA256,
    }
    actual = {key: provenance.get(key) for key in expected}
    if actual != expected:
        raise ValueError(f"provenance pin differs from generator: {actual!r}")


def _fail(parser: argparse.ArgumentParser, error: Exception) -> NoReturn:
    parser.error(str(error))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Read-only JsonMessageWriter.cs from the pinned DCE commit.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Write the rebuilt fixture to this scratch path.")
    mode.add_argument(
        "--check",
        action="store_true",
        help="Compare generated bytes with the committed fixture without writing.",
    )
    args = parser.parse_args()

    try:
        _validate_metadata()
        ledger = _load_json(_LEDGER)
        if args.check:
            fixture = _validated_fixture(
                args.source, ledger, expected_source_sha256=PINNED_SOURCE_SHA256
            )
            rendered = _render_fixture(fixture)
            if rendered.encode() != _COMMITTED_FIXTURE.read_bytes():
                raise ValueError("generated bytes differ from the committed fixture")
            print("Pinned source, generated fixture, and disposition ledger match.")
        else:
            regenerate_fixture(args.source, args.output, ledger)
            print(f"Wrote source-derived reconstruction to {args.output}")
    except (OSError, ValueError) as error:
        _fail(parser, error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
