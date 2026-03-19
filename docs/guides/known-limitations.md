# Known Limitations

Every migration involves trade-offs. Discord and Stoat are different platforms with different capabilities, and some Discord features have no direct equivalent in Stoat. This page documents what changes, what gets lost, and what workarounds are available.

---

## Structural

These limitations relate to how channels, threads, and server organization are represented after migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Threads | Flattened to text channels, prefixed with parent channel name (e.g. `general-my-thread`) | Use `--min-thread-messages` to filter out low-activity threads and reduce channel count |
| Forum posts | Each post becomes a text channel inside a `forum-*` category, with an auto-generated index channel listing all posts | None — this is the closest structural equivalent |
| Stage Channels | Not migrated (no Stoat equivalent) | None |
| Scheduled Events | Not migrated | None |
| Channel ordering | Display order may differ from the original Discord layout | Manually reorder channels in Stoat after migration |

!!! note "Thread flattening and channel limits"
    Every thread becomes a channel. A busy Discord server with hundreds of threads can easily exceed Stoat's 200-channel limit. Use `--min-thread-messages` to set a minimum message count for thread migration, or `--skip-threads` to omit threads entirely. Self-hosted admins can raise the limit — see [Self-Hosted Tips](self-hosted-tips.md).

---

## Content

These limitations affect how individual messages and their content appear after migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Embeds | Flattened to markdown text; inline fields use `\|` separators | None — Stoat embeds have a different structure and cannot replicate Discord embeds exactly |
| Polls | Rendered as plain text showing the question and options | None |
| Stickers | Uploaded as image attachments where the source file is available; Lottie (animated) stickers receive a text fallback | None — Lottie format is not supported by Stoat |
| Reactions | Text summary appended to the message by default (`reaction_mode="text"`). Shows emoji and count. | Set `--reaction-mode native` for per-emoji reactions added via the Stoat API (slower, limited to 20 per message) |
| Forwarded messages | Skipped entirely | None — this is a DiscordChatExporter limitation; forwarded messages export as empty content |

---

## Permissions

These limitations affect role and permission migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Per-member channel overrides | Not supported by Stoat; only role-based overrides are migrated | Create a single-user role for each member who had individual overrides, then apply the override to that role |
| Managed roles (bot roles) | Not migrated — these are auto-created by Discord for each bot integration | None needed — bot integrations do not carry over |

---

## Metadata

These limitations affect message metadata and server history.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Original timestamps | Preserved as an italic text prefix `*[2024-01-15 12:00 UTC]*`, not as message metadata. Stoat shows the import time as the "sent" time. | Self-hosted admins can use direct database insertion for true timestamps — see [Timestamp Preservation](timestamps.md) |
| Edit history | An `*(edited)*` indicator is shown on edited messages, but full edit history is lost | None |
| Audit logs | Not migrated | None |
| Pin order | Pins are restored, but their display order may differ from Discord | None |

---

## Scale and Compatibility

These limitations affect use cases involving large servers or non-standard export types.

| Limitation | Detail | Workaround |
|------------|--------|------------|
| GDPR export incompatibility | Ferry is designed for server migrations using DiscordChatExporter guild exports. GDPR personal data packages have a different structure and are not supported. | Use `DiscordChatExporter.Cli exportguild` instead of GDPR downloads |
| 1M+ message RAM usage | The in-memory `message_map` dict requires approximately 200 MB of RAM for servers with 1 million messages. This is held for the duration of the migration. | Split very large servers into batches, or use the `--incremental` flag to migrate in stages |
| No `X-RateLimit-*` headers | Stoat's API does not expose standard `X-RateLimit-Remaining` or `X-RateLimit-Reset` headers. Ferry uses a 429-response rolling window to adaptively tune its request rate. | None — this is a platform limitation. The adaptive rate limiter handles it automatically |

---

## Platform Features

These limitations relate to platform-level features that either work differently or have no equivalent in Stoat.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Voice channels | Created, but functionality may be limited due to a known upstream issue (Stoat Bug #194) | Verify channel types in the Stoat web interface after migration; voice requires the Vortex or LiveKit service |
| AutoMod | Not supported by Stoat | Configure moderation manually after migration |
| Welcome Screen | Not migrated | None |
| Soundboard | Not supported by Stoat | None |
| Role icons | Not migrated — Stoat roles do not support icons | None |
| Animated emoji | Static fallback uploaded where possible; some animated emoji may be skipped | None — Stoat does not support animated emoji |
| Server boosts | Not applicable — Stoat uses a different model | None |
| Slowmode | Not supported by Stoat | None |

---

## See Also

- [Troubleshooting](troubleshooting.md) — solutions for common migration errors
- [Self-Hosted Tips](self-hosted-tips.md) — raising limits and configuring your own Stoat instance
- [Timestamp Preservation](timestamps.md) — detailed explanation of how timestamps work after migration
