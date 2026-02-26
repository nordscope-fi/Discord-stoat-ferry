"""Dataclasses for parsed DCE export data."""

from dataclasses import dataclass, field


@dataclass
class DCERole:
    """Parsed role from author's role list."""

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
    reference: DCEReference | None = None


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
