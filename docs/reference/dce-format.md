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
  --include-threads All \
  --output /path/to/export/
```

| Flag | Why it is required |
|------|-------------------|
| `--format Json` | Ferry's parser reads JSON only |
| `--markdown false` | Without this flag DCE renders mention syntax (`<@123>`) as display names (`@Username`), destroying the IDs needed for remapping |
| `--media` | Downloads all attachments, avatars, and emoji locally. Discord CDN URLs expire within ~24 hours |
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

---

## Top-Level JSON Schema

```json
{
  "guild": { "id": "...", "name": "...", "iconUrl": "..." },
  "channel": { "id": "...", "type": "GUILD_TEXT", "name": "...", "topic": "..." },
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

DCE exports the Discord channel type as a string name. Ferry maps these to Stoat channel types:

| Discord type string | Type ID | Stoat target | Notes |
|--------------------|---------|-------------|-------|
| `GUILD_TEXT` | 0 | TextChannel | Direct mapping |
| `GUILD_VOICE` | 2 | VoiceChannel | May create text channel on some instances (bug #194) |
| `GUILD_CATEGORY` | 4 | Category | Two-step creation — see [Stoat API Notes](stoat-api-notes.md) |
| `GUILD_ANNOUNCEMENT` | 5 | TextChannel | Treated as text |
| `PUBLIC_THREAD` | 11 | TextChannel (flatten) | Becomes a standalone text channel |
| `PRIVATE_THREAD` | 12 | TextChannel (flatten) | Becomes a standalone text channel |
| `GUILD_FORUM` | 15 | TextChannel(s) per thread | One text channel per exported forum thread |
| `GUILD_MEDIA` | 16 | TextChannel(s) per thread | One text channel per exported media thread |

Stoat has exactly five channel types: SavedMessages, DirectMessage, Group, TextChannel, VoiceChannel.
There are no native threads or forums, so Discord threads are flattened into regular text channels.

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
  "reference": null,
  "isPinned": false
}
```

When `--media` is used, `avatarUrl` and `attachment.url` are relative paths within the export
directory (e.g. `media/attachments/image.png`). Ferry resolves these relative to the export root.

---

## Edge Cases

### Webhook and Bot Messages

Both webhook-originated and bot-authored messages have `author.isBot = true`. DCE does not include a
`webhook_id` field. Ferry treats both identically — they are imported using masquerade with the
bot/webhook's display name and avatar.

### Forwarded Messages

Discord message forwarding (introduced 2024) is not fully represented in DCE exports. A forwarded
message appears as:

- `content`: empty string
- `attachments`: empty array
- `reference`: non-null (points to the original message)

This combination is DCE bug #1322. Ferry detects it during the MESSAGES phase and logs a skip:
`"forwarded message skipped (DCE bug #1322)"`.

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
