# DCE Export Format

This page documents the DiscordChatExporter (DCE) JSON format as it matters to Discord Ferry. It is a
reference for developers working on the parser and for admins troubleshooting export problems.

---

## Required Export Flags

```bash
DiscordChatExporter export \
  --format Json \
  --markdown false \
  --media \
  --reuse-media \
  --include-threads All \
  --output /path/to/export/
```

| Flag | Why it is required |
|------|-------------------|
| `--format Json` | Ferry's parser reads JSON only |
| `--markdown false` | Without this flag DCE renders mention syntax (`<@123>`) as display names (`@Username`), destroying the IDs needed for remapping |
| `--media` | Downloads all attachments, avatars, and emoji locally. Discord CDN URLs expire within ~24 hours |
| `--reuse-media` | Skips redownloading media that already exists locally. Ferry's own DCE runner passes this so a re-export or resume-after-crash finishes in minutes rather than re-fetching every attachment |
| `--include-threads All` | Exports threads and forum posts as separate files alongside their parent channels |

!!! warning "--markdown false is critical"
    If an export was made without `--markdown false`, Ferry's VALIDATE phase will detect rendered
    mentions and warn. The migration can proceed, but all mentions will appear as plain text rather
    than being remapped to Stoat users.

!!! warning "--media is critical"
    If `--media` was omitted, attachment URLs in the export begin with `https://cdn.discordapp.com/`
    rather than a relative local path. Ferry cannot migrate these — Discord CDN URLs expire and Autumn
    cannot fetch URLs directly. Ferry's VALIDATE phase detects this condition and reports it as an error.

---

## File Naming Convention

DCE writes one JSON file per channel or thread, using a deterministic naming pattern:

| Export type | File name pattern |
|------------|-------------------|
| Text channel | `{Guild} - {Channel} [{channel_id}].json` |
| Forum thread | `{Guild} - {Forum Name} - {Thread Name} [{thread_id}].json` |
| Thread in channel | `{Guild} - {Channel} - {Thread Name} [{thread_id}].json` |

**Thread-to-parent relationship is not in the JSON.** Ferry infers it from the filename: a file with
three dash-separated segments (Guild, Parent, Thread) is a thread or forum post. The ID in brackets
is always the thread's own ID, not the parent channel's ID.

The ID in brackets is always at the end of the stem before the `.json` extension. Ferry extracts it
with a regex on the filename.

!!! info "Sanitisation for downstream file writes"
    The names above come straight from Discord and can contain any Unicode. When Ferry writes a
    file **it** owns — a thread archive under `--thread-strategy archive`, or a merged-thread
    attachment under `--thread-strategy merge` — `sanitize_filename()` in
    `migrator/messages.py` strips characters that are illegal on Windows (`< > : " \ | ? *`),
    replaces `/` with `-` so a name like `wip/2026` never creates a subdirectory, and rewrites
    Win32 reserved device names (`CON`, `NUL`, etc.). Two threads whose sanitised names collide
    are suffixed with the thread ID so neither file overwrites the other. This does not affect
    DCE's own output filenames — DCE writes those before Ferry sees them.

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

DCE 2.47 writes the channel type as its **own PascalCase enum name**, which is not the
SCREAMING_SNAKE constant the Discord API documentation uses. Ferry normalises both to Discord's
canonical integer on parse, in `_coerce_channel_type`, because every downstream branch works on the
integer.

Exports written before 2.47 carry the integer directly, and the parser still accepts that shape:
users hold old exports.

| What DCE writes | Type ID | Stoat target | Notes |
|-----------------|---------|-------------|-------|
| `GuildTextChat` | 0 | TextChannel | Direct mapping |
| `GuildVoiceChat` | 2 | VoiceChannel | May create a text channel on some instances (bug #194) |
| `GuildCategory` | 4 | Category | Two-step creation, see [Stoat API Notes](stoat-api-notes.md) |
| `GuildNews` | 5 | TextChannel | An announcement channel, treated as text |
| `GuildNewsThread` | 10 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildPublicThread` | 11 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildPrivateThread` | 12 | TextChannel (flatten) | Becomes a standalone text channel |
| `GuildStageVoice` | 13 | Not migrated | Stoat has no stage channel |
| `GuildDirectory` | 14 | Not migrated | No Stoat equivalent |
| `GuildForum` | 15 | TextChannel(s) per thread | One text channel per thread, grouped in a category named after the forum |
| `GuildMedia` | 16 | TextChannel(s) per thread | One text channel per thread, grouped in a category named after the media channel |

Stoat has exactly five channel types: SavedMessages, DirectMessage, Group, TextChannel, VoiceChannel.
There are no native threads or forums, so Discord threads are flattened into regular text channels.
Forum and media channel threads are grouped into dedicated Stoat categories named after the parent
forum, preserving the organisational structure.

---

## Message Types

DCE uses **string names** for message types, not the numeric IDs that the Discord API returns. Ferry
matches on these strings:

| Type string | Ferry action |
|-------------|-------------|
| `"Default"` | Import normally |
| `"Reply"` | Import with reply reference to the target message |
| `"RecipientAdd"` | Skip |
| `"RecipientRemove"` | Skip |
| `"ChannelNameChange"` | Skip |
| `"ChannelPinnedMessage"` | Import and mark for re-pinning in the PINS phase |
| `"GuildMemberJoin"` | Skip (system noise, no useful content) |
| `"UserPremiumGuildSubscription"` | Skip (boost notification) |
| `"ThreadCreated"` | Skip (thread header injected by Ferry instead) |
| `"ThreadStarterMessage"` | Import as the first message in the thread |
| `"Call"` | Skip |
| `"ChannelIconChange"` | Skip |

Unknown type strings are logged as warnings and the message is skipped.

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
  "stickers": [
    { "name": "wave", "sourceUrl": "media/stickers/wave.png" }
  ],
  "poll": {
    "question": { "text": "Favourite colour?" },
    "answers": [
      { "text": "Red", "votes": 12 },
      { "text": "Blue", "votes": 8 }
    ]
  },
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

**Polls**: Ferry renders poll data as formatted text in the message body (Stoat has no native poll
support). The output looks like: `**Poll: Favourite colour?**` followed by bullet-pointed options
with vote counts.

---

## Edge Cases

### Webhook and Bot Messages

Both webhook-originated and bot-authored messages have `author.isBot = true`. DCE does not include a
`webhook_id` field. Ferry treats both identically — they are imported using masquerade with the
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

The message's own `content` is usually empty — the payload lives entirely in the block.

**There is no author field.** Ferry therefore posts recovered content under whoever forwarded it;
the original writer is not present in the export.

Ferry merges the block into the message and marks it `[forwarded]`, so the ordinary attachment,
embed and sticker paths handle it.

**Exports older than DCE 2.47** wrote neither `forwardedMessage` nor `reference.type`. Those
messages carry no recoverable content and are still skipped with a warning naming the cause.
An empty `reference.type` is the signal for "this export predates 2.47" and is meaningfully
different from `"Default"` — Ferry falls back to the old empty-content heuristic only in that case,
because the heuristic alone also matches an ordinary reply carrying just a sticker or an embed.

### System Messages with Empty Content

System message types (GuildMemberJoin, ChannelPinnedMessage, etc.) often have `content: ""`. Ferry
always checks the `type` field first, never skipping a message solely because `content` is empty.

### Reply References

When `type` is `"Reply"`, the `reference` object contains only the original message's ID:

```json
"reference": { "messageId": "1122334455", "channelId": "...", "guildId": "..." }
```

It does not embed the referenced message's content. Ferry cross-references this ID against the
Discord→Stoat message ID map built during the MESSAGES phase. If the referenced message was not
migrated (e.g. it predates the export date range), the reply is imported as a regular message
without a reply reference, and a warning is logged.
