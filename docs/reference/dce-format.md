# DiscordChatExporter (DCE) Export Format

This page documents the DiscordChatExporter (DCE) JSON format as it matters to Discord Ferry. It is a
reference for developers working on the parser and for admins troubleshooting export problems.

Ferry's export-checking stage is called VALIDATE. A content delivery network (CDN) serves Discord's
remote media files.

---

## Required Export Flags

```bash
DiscordChatExporter export \
  --format Json \
  --markdown false \
  --media \
  --include-threads All \
  --output /path/to/export/
```

| Flag | Why it is required |
|------|-------------------|
| `--format Json` | Ferry's parser reads JSON only |
| `--markdown false` | Without this flag DCE renders mention syntax (`<@123>`) as display names (`@Username`), destroying the IDs needed for remapping |
| `--media` | Downloads all attachments, avatars, and emoji locally. Discord CDN URLs expire within ~24 hours |
| `--include-threads All` | Exports threads and forum posts as separate files alongside their parent channels |

`--reuse-media` is optional. It skips every media file already present, without checking whether an
interrupted download left the file incomplete.

!!! warning "Use `--reuse-media` only after a completed export"
    Ferry passes this flag only when its saved state records a completed export in the same Discord
    server cache. After an interrupted or failed export, Ferry omits the flag so DCE replaces
    existing media files.

!!! warning "--markdown false is critical"
    If an export was made without `--markdown false`, Ferry's VALIDATE phase will detect rendered
    mentions and warn. The migration can proceed, but all mentions will appear as plain text rather
    than being remapped to Stoat users.

!!! warning "--media is critical"
    If `--media` was omitted, attachment URLs in the export begin with `https://cdn.discordapp.com/`
    rather than a relative local path. Ferry cannot migrate these, Discord CDN URLs expire and Autumn
    cannot fetch URLs directly. Ferry's VALIDATE phase detects this condition and reports it as an error.

---

## File Naming Convention

DCE writes one JSON file per channel or thread, using a deterministic naming pattern:

| Export type | File name pattern |
|------------|-------------------|
| Text channel | `{Guild} - {Channel} [{channel_id}].json` |
| Forum thread | `{Guild} - {Forum Name} - {Thread Name} [{thread_id}].json` |
| Thread in channel | `{Guild} - {Channel} - {Thread Name} [{thread_id}].json` |

For a thread, DCE 2.48 writes its parent channel ID to `channel.categoryId` and the parent name to
`channel.category`. Ferry treats the parsed channel type as authoritative. When a parent ID is
present, Ferry reads the parent from those JSON fields. Without a parent ID, it falls back to the
three-segment filename. The ID in brackets is always the thread's own ID, not the parent channel's
ID.

The ID in brackets is always at the end of the stem before the `.json` extension. Ferry extracts it
with a regex on the filename.

!!! info "Sanitisation for downstream file writes"
    The names above come straight from Discord and can contain any Unicode. When Ferry writes a
    thread archive, it sanitises the parent and thread names. Archive mode makes no API calls and
    posts nothing to Stoat. It writes each file to
    `{output_dir}/threads/{parent_channel_name}/{thread_name}.md`.
    `sanitize_filename()` strips characters that are illegal on Windows (`< > : " \ | ? *`). It
    replaces `/` with `-` so a name like `wip/2026` never creates a subdirectory, and rewrites
    Win32 reserved device names (`CON`, `NUL`, etc.). During one run, colliding thread names in one
    parent folder receive numeric suffixes such as `_2` and `_3`. This does not affect DCE's own
    output filenames, DCE writes those before Ferry sees them.

---

## Top-Level JSON Schema

```json
{
  "guild": { "id": "...", "name": "...", "iconUrl": "..." },
  "channel": { "id": "...", "type": "GuildTextChat", "name": "...", "topic": "..." },
  "dateRange": { "after": null, "before": null },
  "exportedAt": "2024-01-15T12:00:00+00:00",
  "messages": [ ... ],
  "messageCount": 1234
}
```

The `channel` object at the top level describes the channel this file represents. `messages` is an
array of message objects. `messageCount` matches `messages.length` and is used as a sanity check
during validation.

---

## Channel Types

DCE 2.47 writes the channel type as its own enum name, with each word capitalised and no
separators. This style is called PascalCase, and it is not the
SCREAMING_SNAKE constant the Discord API documentation uses. Ferry normalises both to Discord's
canonical integer on parse, in `_coerce_channel_type`, because every downstream branch works on the
integer.

Exports written before 2.47 carry the integer directly, and the parser still accepts that shape:
users hold old exports.

| What DCE writes | Type ID | Stoat target | Notes |
|-----------------|---------|-------------|-------|
| `GuildTextChat` | 0 | TextChannel | Direct mapping |
| `GuildVoiceChat` | 2 | VoiceChannel | Requires LiveKit on the target instance. Ferry retries a failed creation as text. |
| `GuildCategory` | 4 | Category | Two-step creation, see [Stoat API Notes](stoat-api-notes.md) |
| `GuildNews` | 5 | TextChannel | An announcement channel, treated as text |
| `GuildNewsThread` | 10 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildPublicThread` | 11 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildPrivateThread` | 12 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildStageVoice` | 13 | VoiceChannel | Treated like `GuildVoiceChat`. Stoat has no stage channel, so stage-only features are lost. |
| `GuildDirectory` | 14 | Not migrated | No Stoat equivalent |
| `GuildForum` | 15 | Parser compatibility only | DCE 2.48 removes forum containers before export. Their posts arrive as `GuildPublicThread` (11), so normal 2.48 exports do not reach Ferry's type-15 delivery path. |
| `GuildMedia` | 16 | Parser compatibility only | DCE 2.48's `ChannelKind` enum does not define this value. Ferry can parse it if present, but normal 2.48 output does not produce it. |

Stoat has exactly five channel types. `SavedMessages` stores a user's own notes, `DirectMessage`
and `Group` are private chats, and `TextChannel` and `VoiceChannel` are server channels.
There are no native threads or forums, so Discord threads are flattened into regular text channels.

The 2.48 field-disposition ledger uses `consumed` to mean that Ferry parses a field. It does not
promise that the field controls destination delivery. In normal DCE 2.48 output, forum containers
are not exported directly, and their posts follow the ordinary thread strategy as type 11.

---

## Message Types

DCE 2.48 writes the named values in its `MessageKind` enum through
`message.Kind.ToString()`. Ferry matches those strings:

| Type string | Ferry action |
|-------------|-------------|
| `"Default"` | Import normally |
| `"Reply"` | Import with reply reference to the target message |
| `"PollResult"` | Import its exported content normally |
| `"RecipientAdd"` | Skip |
| `"RecipientRemove"` | Skip |
| `"ChannelNameChange"` | Skip |
| `"ChannelPinnedMessage"` | Import and mark for re-pinning in the PINS phase |
| `"GuildMemberJoin"` | Skip (system noise, no useful content) |
| `"ThreadCreated"` | Skip (thread header injected by Ferry instead) |
| `"ThreadStarterMessage"` | Import as the first message in the thread |
| `"Call"` | Skip |
| `"ChannelIconChange"` | Skip |

Ferry also skips the legacy descriptive alias `"UserPremiumGuildSubscription"` for boost
notifications. DCE 2.48 does not define or emit that name.

Ferry skips two numeric system-message forms:

| Type string | Source meaning | Ferry action |
|-------------|----------------|--------------|
| `"8"` | Boost notification. DCE 2.48 has no `MessageKind` name for this Discord value, so `ToString()` keeps the number. | Skip |
| `"21"` | `ThreadStarterMessage`. Older DCE output wrote its empty placeholder as a number. | Skip |

Other numeric values are not mapped as known system events. Named `"ThreadStarterMessage"` remains
supported for exports that contain its message content.

---

## Message Object Schema (abridged)

```json
{
  "id": "1234567890",
  "type": "Default",
  "timestamp": "2024-01-15T10:30:00+00:00",
  "timestampEdited": null,
  "content": "Hello, world!",
  "author": {
    "id": "987654321",
    "name": "Alice",
    "discriminator": "0001",
    "nickname": "ali",
    "isBot": false,
    "avatarUrl": "media/avatars/alice.png"
  },
  "attachments": [
    { "id": "...", "url": "media/attachments/image.png", "fileName": "image.png", "fileSizeBytes": 204800 }
  ],
  "embeds": [],
  "reactions": [
    { "emoji": { "id": null, "name": "👍" }, "count": 3 }
  ],
  "mentions": [
    { "id": "...", "name": "Bob" }
  ],
  "inlineEmojis": [
    { "id": "...", "name": "wave", "isAnimated": false, "imageUrl": "media/wave.png" }
  ],
  "interaction": {
    "id": "...",
    "name": "status",
    "user": { "id": "...", "name": "Bob", "nickname": "Bobby" }
  },
  "stickers": [
    { "name": "wave", "sourceUrl": "media/stickers/wave.png" }
  ],
  "reference": null,
  "isPinned": false
}
```

When `--media` is used, `avatarUrl` and `attachment.url` are relative paths within the export
directory (e.g. `media/attachments/image.png`). Ferry resolves these relative to the export root.

**`timestampEdited`**: If present (non-null), indicates the message was edited after original send.
Ferry prepends `*(edited)*` to the migrated message content, after the timestamp prefix. Example:
`*[2024-01-15 12:00 UTC]* *(edited)* Hello everyone!`

**Stickers**: If `sourceUrl` is a local relative path (downloaded via `--media`), Ferry uploads the
image as a message attachment. Lottie stickers and missing files fall back to a text placeholder
like `[Sticker: wave]`.

**Inline emoji metadata**: DCE 2.48 writes `inlineEmojis` on messages and embeds. Ferry uses the
local image paths as emoji asset sources. These entries enrich the emoji records already found in
message content and do not increase reaction or message counts.

**Interactions**: An `interaction` records an application command name and the Discord user who
invoked it. Ferry adds a context line such as `*[Discord command /status invoked by Bobby]*` before
the message content. The interaction ID and the user's other profile fields are parsed but are not
sent to Stoat because Stoat has no matching command-interaction object.

**Polls**: DCE 2.48 does not write an active `poll` object to JSON. Ferry therefore cannot recreate
active polls or their choices. A closed poll can appear as a `PollResult` system message. DCE writes
that notification as ordinary `content` and `embeds`, which Ferry migrates through the normal
message and embed paths. Stoat has no native poll endpoint used by Ferry.

---

## Edge Cases

### Webhook and Bot Messages

Both webhook-originated and bot-authored messages have `author.isBot = true`. DCE does not include a
`webhook_id` field. Ferry treats both identically, they are imported using masquerade with the
bot/webhook's display name and avatar.

### Forwarded Messages

Since **DCE 2.47** (February 2026, PR #1451) a forwarded message exports its full payload in a
`forwardedMessage` object, and `reference.type` distinguishes a forward from a reply:

```jsonc
"reference": {
  "type": "Forward",          // MessageReferenceKind: "Default" (a reply) or "Forward"
  "messageId": "...", "channelId": "...", "guildId": "..."
},
"forwardedMessage": {
  "timestamp": "...",
  "timestampEdited": "..." | null,
  "content": "...",
  "attachments": [ /* same shape as a top-level attachment */ ],
  "embeds":      [ /* same shape as a top-level embed */ ],
  "stickers":    [ /* same shape as a top-level sticker */ ]
}
```

The message's own `content` is usually empty, the payload lives entirely in the block.

**There is no author field.** Ferry therefore posts recovered content under whoever forwarded it;
the original writer is not present in the export.

Ferry merges the block into the message and marks it `[forwarded]`, so the ordinary attachment,
embed and sticker paths handle it.

**Exports older than DCE 2.47** wrote neither `forwardedMessage` nor `reference.type`. Those
messages carry no recoverable content and are still skipped with a warning naming the cause.
An empty `reference.type` signals that the export predates 2.47. It is different from `"Default"`.
Ferry uses the old empty-content test only for that older shape, because the test also matches an
ordinary reply carrying just a sticker or an embed.

### System Messages with Empty Content

System message types often have `content: ""`. These include member joins (`GuildMemberJoin`) and
pin events (`ChannelPinnedMessage`). Ferry always checks the `type` field first, never skipping a
message solely because `content` is empty.

### Reply References

When `type` is `"Reply"`, the `reference` object contains only the original message's ID:

```json
"reference": { "messageId": "1122334455", "channelId": "...", "guildId": "..." }
```

It does not embed the referenced message's content. Ferry cross-references this ID against the
Discord→Stoat message ID map built during the message-copying stage, named MESSAGES. If the referenced message was not
migrated (e.g. it predates the export date range), the reply is imported as a regular message
without a reply reference, and a warning is logged.
