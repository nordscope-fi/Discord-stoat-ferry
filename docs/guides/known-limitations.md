# Known Limitations

Every migration involves trade-offs. Discord and Stoat are different platforms with different capabilities, and some Discord features have no direct equivalent in Stoat. This page documents what changes, what gets lost, and what workarounds are available.

!!! warning "Already migrated with an older version?"
    Everything on this page is a deliberate limitation. Ferry also had five **bugs** that lost
    content without any warning, fixed between v2.8.2 and v2.10.0 (missing forwarded messages, missing
    attachments, and voice channels nobody could use. If you migrated before v2.10.0, see
    [Was my earlier migration affected?](earlier-migrations.md).

---

## Structural

These limitations relate to how channels, threads, and server organization are represented after migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Threads | Flattened to text channels by default, prefixed with parent channel name (e.g. `general-my-thread`) | Use `--thread-strategy merge` to append thread messages into the parent channel, or `--thread-strategy archive` to export as a markdown attachment. Both are also available in the GUI's thread strategy setting. |
| Forum posts | Each post becomes a text channel inside a `forum-*` category, with an auto-generated index channel listing all posts | None — this is the closest structural equivalent |
| Stage Channels | Not migrated (no Stoat equivalent) | None |
| Scheduled Events | Not migrated | None |
| Channel ordering | Categories follow the original Discord order (since v2.3.0, requires a Discord token), but channels *inside* a category may appear in a different order | Manually reorder channels within categories in Stoat after migration |
| Channel topic longer than 1,024 characters | Stoat and Discord both cap channel topics at 1,024. Ferry truncates a longer topic to 1,024 characters before sending; the tail is discarded silently as a defensive measure for hand-edited or corrupt exports. | None — a valid Discord export cannot exceed 1,024 to begin with; shorten the topic in Discord if you need it faithfully preserved |

!!! note "Thread handling and channel limits"
    With the default `flatten` strategy, every thread becomes a channel. A busy Discord server with hundreds of threads can easily exceed Stoat's 200-channel limit. Use `--thread-strategy merge` or `archive` to avoid creating extra channels, or use `--skip-threads` to omit threads entirely. Self-hosted admins can raise the limit (see [Self-Hosted Tips](self-hosted-tips.md)).

---

## Content

These limitations affect how individual messages and their content appear after migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Embeds | Flattened to markdown text; inline fields use `\|` separators | None — Stoat embeds have a different structure and cannot replicate Discord embeds exactly |
| Polls | Active polls are unavailable because DCE 2.48 does not export them. Closed-poll notifications keep the fallback text and embeds that DCE writes. | Keep a separate record of active poll choices and results before migration if they matter. Stoat has no native poll endpoint used by Ferry. |
| Application command interactions | Ferry adds the command name and invoking user's display name above the message. Interaction IDs and the user's other profile fields are not sent. | None. Stoat has no matching command-interaction object. |
| Stickers | Uploaded as image attachments where the source file is available; Lottie (animated) stickers receive a text fallback | None — Lottie format is not supported by Stoat |
| Reactions | Text summary appended to the message by default. Shows emoji and count. Custom emoji migrated in the Emoji phase render inline using the Stoat emoji syntax (`<:id:>`), so they appear as the actual image; custom emoji that were **not** migrated fall back to `[:name:]` as plain text. Unicode emoji pass through unchanged. Stoat allows at most 20 reactions per message; anything beyond that is dropped and counted in the migration report. | Use `--reaction-mode native` (or the GUI's Reaction mode dropdown) for per-emoji reactions added via the API — slower, still capped at 20 per message, and native mode appends `[Original counts: ...]` when Stoat's cap truncates so nothing is silently lost |
| Embeds on very long messages | Stoat rejects a message whose combined content + embeds exceed a 2,000-character body ceiling. Ferry walks embeds in order and drops any that would push the total past 2,000, appending a compact `[Embed dropped: <title>]` note to the message body and incrementing `embeds_dropped` in the state file and migration report. Trailing embeds are the ones lost. | Split the source message manually before exporting, or accept the drop — the note in the migrated message flags exactly what went and where |
| Forwarded messages | Content, attachments, embeds and stickers are migrated, marked `[forwarded]`. They post under the name of whoever **forwarded** them, not the original author. | None for attribution — DiscordChatExporter's forwarded block carries no author field, so the original writer is not in the export at all. Exports made with DCE older than 2.47 have no forwarded content to recover and are still skipped with a warning; re-export to recover them. |
| Long messages (>2000 chars) | Split into sequential parts with `[continued K/N]` markers (e.g. `[continued 2/3]`). Original content is fully preserved across parts. | None needed — splitting is automatic and lossless |
| Author names >32 chars | Truncated to 29 chars with a `#XXXX` discriminator suffix derived from the author's Discord ID. Ensures uniqueness while fitting the Stoat masquerade name limit. | None — the suffix preserves attributability even after truncation |
| Unreferenced Autumn uploads | `--cleanup-orphans` detects Autumn file IDs referenced in state but not in the final server. Reported in the migration summary. | No automatic deletion — Stoat does not expose a DELETE endpoint for Autumn files. Clean up manually via the Stoat web interface if needed. |

---

## Permissions

These limitations affect role and permission migration.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Per-member channel overrides | Not supported by Stoat; only role-based overrides are migrated | Create a single-user role for each member who had individual overrides, then apply the override to that role |
| Managed roles (bot roles) | Not migrated — these are auto-created by Discord for each bot integration | None needed — bot integrations do not carry over |
| Role membership | Roles are recreated, but members are **not** assigned to them — there is no reliable way to map Discord accounts to Stoat accounts | Members re-join via invite and assign roles manually (or with a Stoat bot) |
| "Pin messages" permission | Not migrated on its own — Stoat has no pin-only permission. Pinning lives under `ManageMessages`, which also allows deletion, so Ferry will not grant it from a pin-only Discord role | Grant `ManageMessages` manually to roles that should be able to pin |
| Discord permissions with no Stoat counterpart | Dropped — threads, events, polls, soundboard, stickers, application commands, server insights, TTS, priority speaker and voice activity detection have no Stoat equivalent to grant | None needed — the underlying features do not exist on Stoat |

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

## Incremental Migration

`--incremental` copies messages that are **new** since the last completed run. Two things to know:

| Limitation | Detail | Workaround |
|------------|--------|------------|
| New messages only | Incremental detects messages *added* after the last run. Messages that were **edited or deleted** on Discord in the meantime are not updated on Stoat. | None — full edit/delete sync is out of scope |
| Failed messages | Messages that failed to send in a previous run are re-sent automatically by the next `--incremental` run. | None needed — this is automatic |

---

## Platform Features

These limitations relate to platform-level features that either work differently or have no equivalent in Stoat.

| Discord Feature | What Stoat Gets | Workaround |
|-----------------|----------------|------------|
| Voice channels | Created, but functionality may be limited due to a known upstream issue (Stoat Bug #194) | Verify channel types in the Stoat web interface after migration; voice requires the Vortex or LiveKit service. Run `ferry probe` to check whether your instance supports voice before migrating. |
| AutoMod | Not supported by Stoat | Configure moderation manually after migration |
| Welcome Screen | Not migrated | None |
| Soundboard | Not supported by Stoat | None |
| Role icons | Image icons are migrated (since v2.5.0, requires a Discord token). Emoji-only role icons cannot be migrated. | None for emoji icons — Stoat only supports image icons |
| Animated emoji | Static fallback uploaded where possible; some animated emoji may be skipped | None — Stoat does not support animated emoji |
| Server boosts | Not applicable — Stoat uses a different model | None |

---

## Rollback

| Limitation | Why | Workaround |
|------------|-----|------------|
| **Autumn objects are not removed by rollback** | Stoat's Autumn file store has no public DELETE endpoint. Uploaded attachments, avatars, and emoji images uploaded by a migration become orphan files on the Autumn server. | None — the orphans are inaccessible from Stoat's UI once their parent channels/messages/emoji are deleted, but they continue to consume Autumn storage on the host. Contact the Stoat instance admin if cleanup is critical. The rollback confirmation summary reports the orphan count. |
| **Editing categories during rollback may overwrite your changes** | The final category cleanup PATCH is last-write-wins. Rollback fetches the current categories array, filters out Ferry-owned entries, and PATCHes the remainder back. Any category edits you make between rollback's fetch and PATCH are overwritten. | The window is typically a few seconds; on large servers it can extend to the duration of the channel + role + emoji deletion phases (~minutes). Don't edit categories while a rollback is running. |
| **Rollback does not delete the Stoat server itself** | Out of scope per design — Ferry never calls `DELETE /servers/{id}`. Rollback's job is to remove what Ferry created inside the server, not to delete the user's container. | Delete the server manually in Stoat's UI if you want a clean slate. |
| **Rollback cannot run without a `state.json`** | The rollback engine reads entity IDs from `state.json` (`channel_map`, `role_map`, `emoji_map`, `category_map`). Without it, nothing to delete safely. | If you lose `state.json` but want to clean up, you'll need to identify and delete Ferry-created entities manually. |

---

## Verifying a Migration

`ferry check` confirms a finished migration against the live server. What it proves is narrower than it might appear, and the limits below are deliberate rather than unfinished work.

**A gap in the middle of a channel is invisible.** Check reads each channel's most recent 100 messages and confirms the last one Ferry recorded is still among them. If messages went missing earlier in the channel while the last one survived, Check reports `ok`. Read an `ok` as "the recorded last message is present", never as "this channel is complete".

**A channel that gained more than 100 messages since the migration cannot be checked.** The window no longer reaches back to the message Ferry recorded, so Check reports `unverifiable` rather than guessing. This is common on an active server.

**A renamed channel, role, or emoji is only detected for migrations run under 2.17.0 or later for channels and roles, and under 2.33.0 or later for emoji.** From those releases Ferry records the name it gave each entity, so `ferry check` can compare it against the current one and reports a rename as a warning. A migration run by an earlier version recorded no such names, and nothing can recover them after the fact, so a rename there stays invisible. Renamed *categories* have always been detected, because category names were recorded from the start.

**A message accepted as a duplicate cannot be confirmed.** When Stoat recognises a retry and refuses it, it does not say which message it already had, so Ferry has no identifier to check later. Channels affected this way report `unverifiable`.

**A channel your token cannot read reports `unverifiable`.** Check can tell this apart from a deleted channel, because the server lists every channel's identifier even when it will not return the channel itself.

**A forum index whose message id was lost is not refreshed.** If Stoat accepts the index message as a duplicate, it returns no identifier, so Ferry cannot edit that message on a later run. It records the index as present and leaves its counts as they were, rather than posting a second copy.

**Check refuses to run against a dry run.** A dry run records placeholders for channels and messages that were never created, so there is nothing on a server to compare them with.

## Repairing a Migration

`ferry repair` acts on what `ferry check` reports, so **it inherits every limit above**. It cannot
restore what the check cannot see, and the cases below are deliberate rather than unfinished.

**A gap in the middle of a channel cannot be repaired, because it cannot be found.** Check confirms
the recorded last message is present; it has no way to notice messages missing earlier in the
channel, so repair has nothing to act on.

**A message accepted as a duplicate cannot be restored.** Ferry holds no identifier for it, so
neither the check nor the repair can tell whether it is there.

**Merged thread content is not restored.** Under `--thread-strategy=merge` a thread's messages were
appended to its parent channel, and Ferry finds them by the parent's *name* rather than its
identifier. A repair working from a channel's own export cannot reach them. If repair recreates a
parent that absorbed thread content, it restores the parent's own messages. It records a warning
naming each thread it left behind. `flatten`, the default, gives every thread its own channel and
is restored completely.

**A missing forum index is recreated and rebuilt.** Repair recreates the index channel, puts it
back at the top of its forum category, and rebuilds its pinned index message from Ferry's own record
of the forum's posts. If the forum's category was also deleted and this run cannot restore it,
repair declines the index and says so.

**A missing custom emoji is declined.** An emoji's identifier on Stoat *is* its uploaded file, so
recreating one produces a different emoji that merely looks the same.

**A migration run before 2.18.0 may not be repairable.** Repair recreates an entity under the name
Ferry originally gave it, and that name is recorded only from 2.17.0 onward. Where it is missing,
repair declines and says so rather than inventing one: an entity restored under a different name
looks repaired and is not.

**A recreated role's rank is not restored by repair itself.** Repair restores a recreated role's
name, permissions, colour, hoist setting and icon when Discord metadata is present, and a role icon
is re-fetched from Discord and re-uploaded. Rank is the exception: the per-role edit cannot set it,
so a full `--incremental` re-run restores position. With no Discord metadata (no token at migration
time, or the role absent from it) colour, hoist and icon cannot be restored either, and repair
records a warning naming the role.

**Repair never renames anything.** A renamed channel, role or category is reported by the check as a
warning and left alone, because a rename almost always means you made it.

**Repair refuses a rolled-back migration.** Rollback deliberately keeps the identifier maps as an
audit trail, so a check against that state reports every channel missing. Repair stops rather than
rebuilding a server you chose to remove.

## See Also

- [Troubleshooting](troubleshooting.md) (solutions for common migration errors)
- [Self-Hosted Tips](self-hosted-tips.md) (raising limits and configuring your own Stoat instance)
- [Timestamp Preservation](timestamps.md) (detailed explanation of how timestamps work after migration)
