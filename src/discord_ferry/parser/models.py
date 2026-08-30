"""Dataclasses for parsed DCE export data."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DCERole:
    """A Discord role record — from a DCE export author list or a live guild fetch."""

    id: str
    name: str
    color: str | None = None
    position: int = 0


@dataclass
class DCEAuthor:
    """Parsed message author."""

    id: str
    name: str
    discriminator: str = "0000"
    nickname: str = ""
    color: str | None = None
    is_bot: bool = False
    avatar_url: str = ""
    roles: list[DCERole] = field(default_factory=list)


@dataclass
class DCEAttachment:
    """Parsed message attachment."""

    id: str
    url: str
    file_name: str
    file_size_bytes: int = 0


@dataclass
class DCEEmoji:
    """Parsed emoji (used in reactions and custom emoji references)."""

    id: str
    name: str
    is_animated: bool = False
    image_url: str = ""


@dataclass
class DCEReaction:
    """Parsed message reaction."""

    emoji: DCEEmoji
    count: int = 1


@dataclass
class DCEReference:
    """Parsed message reference (for replies and forwarded messages)."""

    message_id: str
    channel_id: str = ""
    guild_id: str = ""
    #: DCE's ``MessageReferenceKind``: "Default" (a reply) or "Forward". Empty string
    #: means the export predates DCE 2.47, which never wrote this field -- which is a
    #: meaningfully different case from "Default", so do not default it to that.
    type: str = ""


@dataclass
class DCEForwardedMessage:
    """The payload of a forwarded message (DCE 2.47+, PR #1451).

    Carries **no author**: upstream writes exactly these six fields
    (``JsonMessageWriter.cs:550-592`` at 2.47.1). Recovered content is therefore
    necessarily attributed to whoever forwarded it, not to whoever wrote it.
    """

    timestamp: str = ""
    timestamp_edited: str | None = None
    content: str = ""
    attachments: list[DCEAttachment] = field(default_factory=list)
    embeds: list[dict[str, object]] = field(default_factory=list)
    stickers: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DCEInteraction:
    """Application command interaction attached to a Discord message."""

    id: str
    name: str
    user: DCEAuthor


@dataclass
class DCEMessage:
    """Parsed Discord message."""

    id: str
    type: str
    timestamp: str
    content: str
    author: DCEAuthor
    is_pinned: bool = False
    timestamp_edited: str | None = None
    attachments: list[DCEAttachment] = field(default_factory=list)
    embeds: list[dict[str, object]] = field(default_factory=list)
    stickers: list[dict[str, str]] = field(default_factory=list)
    reactions: list[DCEReaction] = field(default_factory=list)
    mentions: list[dict[str, str]] = field(default_factory=list)
    inline_emojis: list[DCEEmoji] = field(default_factory=list)
    interaction: DCEInteraction | None = None
    reference: DCEReference | None = None
    forwarded_message: DCEForwardedMessage | None = None


@dataclass
class DCEChannel:
    """Parsed channel metadata."""

    id: str
    type: int
    name: str
    category_id: str = ""
    category: str = ""
    topic: str = ""


@dataclass
class DCEGuild:
    """Parsed guild (server) metadata."""

    id: str
    name: str
    icon_url: str = ""


@dataclass
class DCEExport:
    """A single parsed DCE JSON export file."""

    guild: DCEGuild
    channel: DCEChannel
    messages: list[DCEMessage] = field(default_factory=list)
    message_count: int = 0
    exported_at: str = ""
    is_thread: bool = False
    parent_channel_name: str = ""
    json_path: Path | None = None
