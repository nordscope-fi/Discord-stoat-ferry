from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from discord_ferry.parser.dce_parser import parse_single_export, stream_messages
from discord_ferry.parser.models import DCEMessage
from discord_ferry.parser.transforms import flatten_embed, handle_stickers

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dce_2_48" / "source-derived"
_EXPORT_PATH = _FIXTURE_DIR / "maximal-writer-shape.json"
_LEDGER_PATH = _FIXTURE_DIR / "field-dispositions.json"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _message() -> DCEMessage:
    return parse_single_export(_EXPORT_PATH).messages[0]


def _raw_message() -> dict[str, object]:
    fixture = _load_json(_EXPORT_PATH)
    assert isinstance(fixture, dict)
    messages = fixture["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    return message


def test_writer_paths_equal_disposition_paths() -> None:
    fixture_paths = _json_paths(_load_json(_EXPORT_PATH))
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(ledger, dict)
    assert len(ledger["paths"]) == len(set(ledger["paths"]))
    assert fixture_paths == set(ledger["paths"])


def test_every_path_has_an_exact_disposition() -> None:
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(ledger, dict)
    paths = set(ledger["paths"])
    dispositions = ledger["dispositions"]
    assert isinstance(dispositions, dict)
    assert set(dispositions) == paths
    assert {entry["status"] for entry in dispositions.values()} == {"consumed", "ignored"}
    for path, entry in dispositions.items():
        if entry["status"] == "ignored":
            assert entry.get("reason", "").strip(), path


def test_unknown_writer_path_is_detected_as_contract_drift() -> None:
    fixture = _load_json(_EXPORT_PATH)
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(fixture, dict)
    assert isinstance(ledger, dict)
    fixture["unknownWriterField"] = True

    assert _json_paths(fixture) - set(ledger["paths"]) == {"unknownWriterField"}


def test_source_provenance_pins_the_reviewed_writer() -> None:
    provenance = _load_json(_FIXTURE_DIR / "provenance.json")
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(provenance, dict)
    assert isinstance(ledger, dict)

    assert provenance["evidenceClass"] == "source-derived"
    assert provenance["dceVersion"] == provenance["sourceTag"] == ledger["sourceTag"] == "2.48"
    assert provenance["sourceCommit"] == "905489b01b5523719082275c34c232fc47f827c6"
    assert provenance["sourceFile"].endswith("JsonMessageWriter.cs")


def test_identity_content_author_and_roles_parse_from_fixture() -> None:
    export = parse_single_export(_EXPORT_PATH)
    message = export.messages[0]

    assert (export.guild.id, export.guild.name) == ("100000000000000001", "Contract Guild")
    assert export.guild.icon_url == "media/guild-icon.png"
    assert (export.channel.id, export.channel.name, export.channel.type) == (
        "200000000000000001",
        "contract-channel",
        0,
    )
    assert (export.channel.category_id, export.channel.category, export.channel.topic) == (
        "200000000000000000",
        "Contract Category",
        "Source-derived DCE writer contract",
    )
    assert export.exported_at == "2026-08-30T12:00:00+00:00"
    assert export.message_count == 1
    assert message.type == "PollResult"
    assert (message.id, message.timestamp) == (
        "300000000000000001",
        "2026-08-30T10:00:00+00:00",
    )
    assert message.content.startswith("Alice's poll Favourite colour? has closed.")
    assert message.timestamp_edited == "2026-08-30T10:05:00+00:00"
    assert message.is_pinned is True
    assert message.author.nickname == "Alice"
    assert (
        message.author.name,
        message.author.discriminator,
        message.author.color,
        message.author.is_bot,
        message.author.avatar_url,
    ) == ("alice", "0000", "#336699", False, "media/alice-avatar.png")
    role = message.author.roles[0]
    assert (role.id, role.name, role.color, role.position) == (
        "500000000000000001",
        "Maintainer",
        "#ff8800",
        7,
    )


def test_attachment_embed_and_sticker_behaviors_execute() -> None:
    message = _message()

    attachment = message.attachments[0]
    assert (attachment.id, attachment.url, attachment.file_name, attachment.file_size_bytes) == (
        "600000000000000001",
        "media/report.txt",
        "report.txt",
        128,
    )
    raw_message = _raw_message()
    assert message.embeds == raw_message["embeds"]
    assert message.stickers == raw_message["stickers"]
    flattened, media_path = flatten_embed(message.embeds[0])
    assert media_path is None
    assert flattened["title"] == "Poll closed"
    assert flattened["colour"] == "#5865f2"
    assert "42 votes" in str(flattened["description"])
    sticker_text, sticker_paths = handle_stickers(message.stickers)
    assert sticker_text == "\n[Sticker: Approved]"
    assert sticker_paths == []


def test_reaction_mention_reference_and_forward_parse() -> None:
    message = _message()

    assert message.reactions[0].emoji.name == "agree"
    assert (
        message.reactions[0].emoji.id,
        message.reactions[0].emoji.is_animated,
        message.reactions[0].emoji.image_url,
    ) == ("900000000000000003", False, "media/agree.png")
    assert message.reactions[0].count == 2
    assert message.mentions == _raw_message()["mentions"]
    assert message.mentions[0]["nickname"] == "Bob"
    assert message.reference is not None
    assert (message.reference.type, message.reference.message_id) == (
        "Default",
        "300000000000000000",
    )
    assert (message.reference.channel_id, message.reference.guild_id) == (
        "200000000000000001",
        "100000000000000001",
    )
    assert message.forwarded_message is not None
    assert message.forwarded_message.content == "Forwarded contract content"
    assert (
        message.forwarded_message.timestamp,
        message.forwarded_message.timestamp_edited,
    ) == ("2026-08-29T09:00:00+00:00", "2026-08-29T09:01:00+00:00")
    assert message.forwarded_message.attachments[0].file_name == "forwarded.txt"
    raw_forward = _raw_message()["forwardedMessage"]
    assert isinstance(raw_forward, dict)
    assert message.forwarded_message.embeds == raw_forward["embeds"]
    assert message.forwarded_message.stickers == raw_forward["stickers"]
    forwarded_embed, _ = flatten_embed(message.forwarded_message.embeds[0])
    assert forwarded_embed["title"] == "Forwarded embed"


def test_interaction_and_both_inline_emoji_locations_parse() -> None:
    message = _message()

    assert message.interaction is not None
    assert (message.interaction.id, message.interaction.name) == (
        "800000000000000001",
        "close-poll",
    )
    assert message.interaction.user.nickname == "Bob"
    assert (
        message.interaction.user.id,
        message.interaction.user.name,
        message.interaction.user.avatar_url,
        message.interaction.user.roles[0].name,
    ) == ("400000000000000002", "bob", "media/bob-avatar.png", "Member")
    assert (
        message.inline_emojis[0].id,
        message.inline_emojis[0].name,
        message.inline_emojis[0].is_animated,
        message.inline_emojis[0].image_url,
    ) == ("900000000000000001", "wave", False, "media/wave.png")
    assert message.embeds[0]["inlineEmojis"][0]["name"] == "chart"

    streamed = list(stream_messages(_EXPORT_PATH))
    assert streamed == [message]


def test_active_poll_is_unsupported_upstream_not_a_json_path() -> None:
    ledger = _load_json(_LEDGER_PATH)
    fixture_paths = _json_paths(_load_json(_EXPORT_PATH))
    poll_field = "po" + "ll"

    assert isinstance(ledger, dict)
    assert ledger["capabilities"]["active_poll"] == "unsupported_upstream"
    assert poll_field not in {field.name for field in fields(DCEMessage)}
    assert all(path.rsplit(".", 1)[-1] != poll_field for path in fixture_paths)


def test_fields_marked_ignored_are_not_retained_by_typed_models() -> None:
    export = parse_single_export(_EXPORT_PATH)
    message = export.messages[0]

    assert not hasattr(export.channel, "icon_url")
    assert not hasattr(export, "date_range")
    assert not hasattr(message, "call_ended_timestamp")
    assert not hasattr(message.reactions[0], "users")
    assert not hasattr(message.reactions[0].emoji, "code")
    assert not hasattr(message.inline_emojis[0], "code")


_FAMILY_HANDLERS = {
    "identity": test_identity_content_author_and_roles_parse_from_fixture,
    "content": test_identity_content_author_and_roles_parse_from_fixture,
    "author-role": test_identity_content_author_and_roles_parse_from_fixture,
    "attachment": test_attachment_embed_and_sticker_behaviors_execute,
    "embed": test_attachment_embed_and_sticker_behaviors_execute,
    "sticker": test_attachment_embed_and_sticker_behaviors_execute,
    "reaction": test_reaction_mention_reference_and_forward_parse,
    "mention": test_reaction_mention_reference_and_forward_parse,
    "reference": test_reaction_mention_reference_and_forward_parse,
    "forward": test_reaction_mention_reference_and_forward_parse,
    "interaction": test_interaction_and_both_inline_emoji_locations_parse,
    "inline-emoji": test_interaction_and_both_inline_emoji_locations_parse,
}


@pytest.mark.parametrize("family", sorted(_FAMILY_HANDLERS))
def test_every_ledger_family_runs_an_executable_behavior(family: str) -> None:
    ledger = _load_json(_LEDGER_PATH)
    assert isinstance(ledger, dict)
    ledger_families = {entry["family"] for entry in ledger["dispositions"].values()}
    assert ledger_families == set(_FAMILY_HANDLERS)

    _FAMILY_HANDLERS[family]()
