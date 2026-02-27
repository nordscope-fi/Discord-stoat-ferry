---
globs:
  - "src/discord_ferry/parser/**"
  - "tests/test_parser*"
  - "tests/test_transforms*"
---

# DiscordChatExporter (DCE) Format Rules

## Critical Export Flags

The export MUST use `--markdown false`. Without it, DCE replaces raw mention syntax (`<@123>`) with rendered text (`@Username`), destroying data needed for mention remapping. The VALIDATE phase must detect and warn if exports appear to have rendered markdown.

The export MUST use `--media`. Discord CDN URLs expire within ~24h. If any `attachment.url` starts with `http`, the file was NOT downloaded and cannot be migrated.

## File Naming Convention

| Type | Pattern |
|------|---------|
| Text channel | `{Guild} - {Channel} [{channel_id}].json` |
| Forum thread | `{Guild} - {Forum Name} - {Thread Name} [{thread_id}].json` |
| Thread | `{Guild} - {Channel} - {Thread Name} [{thread_id}].json` |

Thread-to-parent-channel relationship is NOT in JSON metadata. Reconstruct from filename: a file with three dash-separated segments indicates a thread/forum post.

## Message Type Strings

DCE uses string names, NOT numeric IDs:

| Type String | Action |
|-------------|--------|
| "Default" | Import normally |
| "Reply" | Import with reply reference |
| "RecipientAdd" | Skip |
| "RecipientRemove" | Skip |
| "ChannelNameChange" | Skip |
| "ChannelPinnedMessage" | Import, mark for re-pinning |
| "GuildMemberJoin" | Skip (system noise, no useful content) |
| "UserPremiumGuildSubscription" | Skip (boost) |
| "ThreadCreated" | Skip (thread header injected by ferry instead) |
| "ThreadStarterMessage" | Import as first message in thread |

## Channel Type Mapping

| Discord Type | ID | Stoat Target |
|-------------|-----|-------------|
| GUILD_TEXT | 0 | TextChannel |
| GUILD_VOICE | 2 | VoiceChannel (may fail — Bug #194) |
| GUILD_CATEGORY | 4 | Category |
| GUILD_ANNOUNCEMENT | 5 | TextChannel |
| PUBLIC_THREAD | 11 | TextChannel (flatten) |
| PRIVATE_THREAD | 12 | TextChannel (flatten) |
| GUILD_FORUM | 15 | TextChannel(s) per thread |
| GUILD_MEDIA | 16 | TextChannel(s) per thread |

Stoat has exactly 5 channel types: SavedMessages, DirectMessage, Group, TextChannel, VoiceChannel. No threads or forums.

## Edge Cases

- **Webhook/bot messages**: Both have `author.isBot = true`. No `webhook_id` field in DCE export. Treat identically.
- **Forwarded messages**: Export as empty content + empty attachments + non-null `reference` (DCE bug #1322). Detect and log as "forwarded message skipped".
- **System messages**: May have empty `content`. Always check `type` field, not just `content`.
- **Reply references**: Contain only IDs, not referenced message content. Cross-reference within exported messages.
