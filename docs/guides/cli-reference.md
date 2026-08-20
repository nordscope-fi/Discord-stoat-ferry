# CLI Reference

Ferry's command-line interface provides the same migration capability as the GUI, without a browser. It is useful for running unattended overnight migrations, scripting, or running on a remote server.

!!! info "Prerequisites"
    A `pipx` or source install puts `ferry` on your PATH. Run `ferry --help` to confirm it is working.

    From version **2.12.0** the downloaded apps take the same commands:
    `Ferry-windows-x86_64.exe --help` on Windows, or the binary inside `Ferry.app` on macOS. Run
    them from a terminal so the output has somewhere to go. Launched with no console, from a
    service or a scheduled task, the app opens the GUI.

---

## Commands

Ferry has twelve top-level commands: `migrate`, `validate`, `build`, `export-blueprint`, `rollback`, `stats`, `check`, `repair`, `backfill-roles`, `retry`, `probe`, and `tls-check`.

---

## `ferry validate`

Parse a DiscordChatExporter export and report what was found. Makes **zero network calls** — nothing is sent to your Stoat server.

```
ferry validate EXPORT_DIR
```

`EXPORT_DIR` is the path to the folder containing your DCE `.json` files.

**Output includes:**

- Source server name and export date
- Counts: channels, categories, roles, messages, attachments, emoji, threads
- Warnings (for example, missing media files, or messages whose mentions were written as plain text instead of raw IDs)
- An estimated migration time at the default 1.0s rate limit

!!! warning "Exit code"
    `ferry validate` prints everything above and then exits **1** when any message has mentions
    written as plain text, so a script that gates on its exit status stops there. `ferry migrate`
    is not affected by this warning. See
    [Mentions written as plain text](troubleshooting.md#mentions-written-as-plain-text).

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--rate-limit FLOAT` | 1.0 | Seconds per message for ETA calculation |

Use this command to check your export before committing to a full migration.

**Example:**

```bash
ferry validate ~/exports/my-discord-server/
```

---

## `ferry migrate`

Run the full migration. Creates or connects to a Stoat server, then imports structure and messages.

```
ferry migrate [OPTIONS]
```

!!! info "Mode selection"
    Provide either `--discord-token` + `--discord-server` (orchestrated mode) or `--export-dir` (offline mode). You cannot use both.

### Options

| Flag | Environment Variable | Default | Description |
|------|----------------------|---------|-------------|
| `--discord-token TEXT` | `DISCORD_TOKEN` | | Discord user token (orchestrated mode) |
| `--discord-server TEXT` | `DISCORD_SERVER_ID` | | Discord server ID (orchestrated mode) |
| `--export-dir PATH` | | | Path to DCE exports (offline mode) |
| `--stoat-url TEXT` | `STOAT_URL` | *(required)* | Stoat API base URL (e.g. `https://api.stoat.chat`) |
| `--token TEXT` | `STOAT_TOKEN` | *(required)* | Your Stoat user token (copied from your browser — see [setup guide](../getting-started/setup-stoat.md#2-get-your-stoat-user-token)) |
| `--server-id TEXT` | | | Migrate into an existing Stoat server by ID |
| `--server-name TEXT` | | | Name for the new server (defaults to the Discord server name) |
| `--create-invite` / `--no-create-invite` | | on | Create an invite link to the migrated server when the migration finishes. The invite URL is printed in the completion summary and included in the reports. |
| `--invite-channel-id TEXT` | | | Discord channel ID to base the invite on. By default Ferry picks the first eligible text channel. |
| `--skip-messages` | | false | Import structure only — no messages sent |
| `--skip-emoji` | | false | Do not upload custom emoji |
| `--skip-reactions` | | false | Do not add reactions |
| `--skip-threads` | | false | Do not migrate threads or forum posts |
| `--thread-strategy TEXT` | | `flatten` | Thread handling: `flatten` (each thread becomes a channel), `merge` (thread messages merged into parent channel), or `archive` (exported as markdown attachment) |
| `--rate-limit FLOAT` | | 1.0 | Seconds between messages (0.5–3.0 recommended) |
| `--upload-delay FLOAT` | | 0.5 | Seconds between Autumn file uploads |
| `--output-dir TEXT` | | `./ferry-output` | Directory for the migration report and state file |
| `--resume` | | false | Resume an interrupted migration using the saved state file |
| `--incremental` | | false | Delta migration — only migrate messages newer than the last completed run per channel. Cannot be combined with `--resume`. |
| `--force` | | false | Override DCE export freshness errors (>30 days old) and other soft warnings |
| `--dry-run` | | false | Run all phases without making API calls; produces synthetic IDs for validation |
| `--max-channels INT` | | 200 | Channel limit; raise for self-hosted instances with custom limits |
| `--max-emoji INT` | | 100 | Emoji limit; raise for self-hosted instances with custom limits |
| `--verify-uploads` | | false | Post-upload file size verification for Autumn uploads |
| `--reaction-mode [text\|native\|skip]` | | `text` | How reactions migrate: `text` appends a summary to the message (fast); `native` adds per-emoji reactions via API (slow, Stoat caps 20 per message); `skip` drops them |
| `--min-thread-messages INT` | | 0 | Exclude threads with fewer messages (0 = include all). Applies to every thread strategy. |
| `--checkpoint-interval INT` | | 50 | Save state every N messages. Lower = safer but more disk I/O; on resume Ferry replays at most this many messages. |
| `--max-concurrent-channels INT` | | 3 | Channels migrated in parallel. Raise only on self-hosted instances. |
| `--max-concurrent-requests INT` | | 5 | Concurrent API calls across all channel workers. Raise only on self-hosted instances. |
| `--skip-avatars` | | false | Skip the avatar pre-flight phase; avatars still upload on demand during messages |
| `--validate-after` | | false | After migration, fetch the server and compare channel/role counts against expectations (results in `state.json` and the run log) |
| `--cleanup-orphans` | | false | Detect and report unreferenced Autumn uploads after migration (report-only; no files are deleted) |
| `--force-unlock` | | false | Override a stale migration lock on the target Stoat server |
| `--skip-dce-verify` | | false | Skip SHA-256 verification of DCE binary downloads (for self-built binaries) |
| `--verbose` / `-v` | | false | Enable debug output (per-message logging) |
| `--yes` / `-y` | | false | Skip the terms-of-service confirmation prompt (for scripted runs) |

!!! warning "Token security"
    Avoid passing `--token` or `--discord-token` directly on the command line — they may appear in shell history. Use environment variables or a `.env` file instead.

### Environment Variables

You can set credentials in a `.env` file in your working directory. Ferry loads this file automatically.

```dotenv
# .env
DISCORD_TOKEN=your_discord_token_here
DISCORD_SERVER_ID=123456789012345678
STOAT_URL=https://api.stoat.chat
STOAT_TOKEN=your_stoat_token_here
```

!!! tip
    Add `.env` to your `.gitignore` if you keep your project under version control.

Ferry also reads a handful of environment variables outside the `.env` file:

| Variable | Effect |
|----------|--------|
| `FERRY_NO_NATIVE` | Set to any value to force the browser-based GUI even when the desktop window toolkit is available |
| `FERRY_DISABLE_PROXY` | Set to any value to ignore system proxy settings entirely |
| `FERRY_STORAGE_SECRET` | Overrides the local key Ferry uses to encrypt saved tokens (useful for headless deployments) |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Honoured through the standard library; embedded credentials are stripped before any error message is rendered |

---

!!! warning "Concurrency on the official service"
    Raising `--max-concurrent-channels` / `--max-concurrent-requests` against `api.stoat.chat` usually makes runs **slower** — the official rate limits trigger 429 backoff. Ferry prints a warning when you try. These flags are for self-hosted instances with relaxed limits.

!!! note "Two flags named --max-concurrent-requests"
    `ferry migrate --max-concurrent-requests` controls migration API calls. `ferry rollback` has an unrelated flag of the same name that controls concurrent channel deletions.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Migration completed successfully |
| `1` | An error occurred (details in the log) |
| `130` | Interrupted by Ctrl+C |

You can use these in scripts:

```bash
ferry migrate --export-dir ~/exports/my-server/ && echo "Migration complete!"
```

---

## Examples

**1-Click migration (orchestrated):**

```bash
ferry migrate \
  --discord-token "$DISCORD_TOKEN" \
  --discord-server 123456789012345678 \
  --stoat-url https://api.stoat.chat \
  --token "$STOAT_TOKEN"
```

**Validate an export before migrating:**

```bash
ferry validate ~/exports/my-discord-server/
```

**Run a full offline migration using environment variables for credentials:**

```bash
export STOAT_URL=https://api.stoat.chat
export STOAT_TOKEN=your_token_here
ferry migrate --export-dir ~/exports/my-discord-server/
```

**Migrate into an existing Stoat server:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --server-id 01ABCDEF234567890ABCDEFGH
```

**Import structure only (no messages), useful for a test run:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --skip-messages \
  --skip-emoji \
  --skip-reactions
```

**Validate the full migration pipeline without making any API calls:**

```bash
ferry migrate --export-dir ./export --stoat-url https://api.stoat.chat --token "$TOKEN" --dry-run
```

**Resume an interrupted migration:**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://api.stoat.chat \
  --token your_token_here \
  --resume
```

**Run with a faster rate (use with caution on the official hosted service):**

```bash
ferry migrate --export-dir ~/exports/my-discord-server/ \
  --stoat-url https://stoat.example.com \
  --token your_token_here \
  --rate-limit 0.5
```

!!! info "Verbose mode"
    Add `-v` or `--verbose` to any `migrate` command to see a line of output for every message sent. This is useful for diagnosing problems but produces a large amount of output for large servers.

---

## `ferry build`

Create a new Stoat server from a preset template or a custom blueprint file.

```
ferry build [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `--template TEXT` | Use a preset template: `gaming`, `community`, or `education` |
| `--blueprint PATH` | Path to a custom blueprint JSON file |
| `--stoat-url TEXT` | Stoat API base URL *(required)* |
| `--token TEXT` | Your Stoat user token *(required)* |
| `--name TEXT` | Override the server name from the template/blueprint |

You must provide either `--template` or `--blueprint`, but not both.

### Preset Templates

Ferry includes three built-in server templates:

- **gaming** — Admin, Moderator, and Member roles with General, Voice, and Gaming categories
- **community** — Admin, Moderator, Helper, and Member roles with Welcome, General, and Voice categories
- **education** — Instructor, TA, and Student roles with Announcements, Coursework, and Discussion categories

Each template includes appropriate role permissions and channel structures.

**Examples:**

```bash
# Create a gaming server from a preset template
ferry build --template gaming --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"

# Create from a custom blueprint with a custom name
ferry build --blueprint my-server.json --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN" --name "My Server"
```

---

## `ferry export-blueprint`

Convert a DiscordChatExporter export directory into a reusable server blueprint JSON file. The blueprint captures server structure (roles, categories, channels) but not messages.

```
ferry export-blueprint [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `--from PATH` | Path to DCE export directory *(required)* |
| `--output PATH` / `-o` | Output path for the blueprint JSON file *(required)* |
| `--name TEXT` | Override the server name stored in the blueprint |

**Example:**

```bash
# Export a blueprint from an existing DCE export
ferry export-blueprint --from ~/exports/my-discord-server/ --output my-server-blueprint.json

# Then use it to create a new server
ferry build --blueprint my-server-blueprint.json --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"
```

!!! tip "Blueprints use names, not IDs"
    Blueprints store role and channel names rather than Discord IDs, making them portable across different Stoat instances.

---

## `ferry rollback`

Reverse a recorded migration by deleting Ferry-created channels, roles, custom emoji, and Ferry-owned categories from the Stoat target server. Reads `state.json` for the entity IDs to delete. Idempotent: 404 responses are treated as "already deleted" — re-runs are clean no-ops. **Autumn-hosted attachments are not removed** (no public DELETE endpoint for Autumn files; see `known-limitations.md`).

```
ferry rollback --output-dir <path> [OPTIONS]
```

### Options

| Flag | Description |
|------|-------------|
| `--output-dir PATH` | Directory containing the migration's `state.json` *(required)* |
| `--stoat-url URL` | Stoat API base URL (or env `STOAT_URL`) *(required)* |
| `--token TOKEN` | Stoat session token (or env `STOAT_TOKEN`) *(required)* |
| `--server-id ID` | Override the Stoat server ID from `state.json` (rarely needed) |
| `--yes` / `-y` | Skip the confirmation prompt and per-item opt-in for suspect channels |
| `--force-unlock` | Override a stale `[FERRY_LOCK:...]` marker on the target server |
| `--max-concurrent-requests INT` | Max concurrent channel DELETEs (default 5) |
| `--verbose` / `-v` | Verbose output |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Rollback completed with no failures |
| 1 | Engine error, or one or more entities failed to delete (DLQ — see `state.rollback_progress.failures`) |
| 2 | `state.json` missing or unreadable |
| 130 | Ctrl+C or user aborted at the confirmation prompt |

### Confirmation Gate

Before any DELETE call, rollback prints a Rich table with the counts of entities to delete plus any **untracked-Ferry-suspect** channels — channels present on the Stoat server but absent from `state.channel_map` (likely orphans from a crashed prior migration). Each suspect row shows: name, creation time (decoded from the channel's ULID), and the Stoat channel ID. You opt in to deleting each suspect individually; `--yes` skips opt-in and proceeds with mapped entities only (safe default — never auto-deletes suspects).

### Examples

```bash
# Roll back the migration recorded in ./ferry-output, with confirmation prompt
ferry rollback --output-dir ./ferry-output --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"

# Same, but skip the confirmation prompt (CI / automation)
ferry rollback --output-dir ./ferry-output --yes

# Override a stale lock marker from a previous crashed rollback
ferry rollback --output-dir ./ferry-output --force-unlock
```

!!! note "Forensic preservation"
    Rollback never mutates `state.channel_map`, `state.role_map`, or `state.emoji_map`. The migration's audit trail is preserved. Deletions are tracked in a new `state.rollback_progress.rolled_back_ids` set instead. A failed rollback can be re-run; already-deleted entities are skipped via the `rolled_back_ids` set (or via 404 idempotency on a re-attempt).

!!! warning "Categories cleanup is last-write-wins"
    The final category cleanup PATCH is a "fetch then replace" operation. If you edit categories in Stoat's UI while rollback is running, your edits in the window between rollback's fetch and PATCH may be overwritten. The window is typically a few seconds; longer on large servers. See `known-limitations.md`.

---

## `ferry stats`

Print aggregate stats for a completed (or in-progress) migration. Read-only — loads `state.json` and `message_map.json` from the output directory and renders a Rich-table summary to the console. Makes **zero network calls** — no Stoat, Discord, or Autumn API.

Use this after a migration has finished (or after a crash) to see what got migrated, what failed, and how close you came to a perfect fidelity score, without re-running anything.

```
ferry stats OUTPUT_DIR
```

`OUTPUT_DIR` is the path to a directory containing `state.json` (typically `./ferry-output/`).

### What gets rendered

A single Rich table titled `Migration Stats — Stoat ID: <id>` with these sections:

- **Entities** — counts of channels, roles, categories, emojis, and messages migrated.
- **Counters** — attachments uploaded/skipped, pins applied, reactions applied, replies linked/total, embeds total/dropped, failed messages, prior messages total.
- **Fidelity** — the overall fidelity score plus five sub-scores (messages, attachments, embeds, replies, reactions). Sub-scores render as `n/a` when the corresponding category had no items in the migration (e.g., a server with no custom embeds).
- **Errors / Warnings** — counts plus a truncated 80-character preview of the most recent message in each list.
- **Timing** — elapsed wall-clock as `HH:MM:SS` when the migration completed, `in progress` when it was interrupted before completion, or `unknown` when timestamps are unavailable.

Two optional sub-sections render only when present in the state:

- **Per-Channel Messages** — top 20 channels by message count, with a `+N more` footer if applicable. Omitted when no per-channel counts are recorded.
- **Rollback** — counters from `state.rollback_progress` (channels deleted, roles deleted, categories cleaned, failures). Omitted when no rollback was performed.

When the migration was a dry-run, the title carries a `[DRY-RUN]` badge.

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Stats rendered successfully |
| `1` | `state.json` is missing inside `OUTPUT_DIR` or contains invalid JSON |
| `2` | `OUTPUT_DIR` does not exist (Click validation error) |

### Examples

```bash
# Stats from the default output directory
ferry stats ./ferry-output/

# Stats from a specific run's output
ferry stats ~/migrations/my-server-2026-05-15/
```

!!! note "What stats does NOT do"
    `ferry stats` is read-only. It does not write to `state.json`, does not call any API, and does not modify the Stoat server. For a richer Markdown report including the original Discord export context, use the `migration_report.md` file written into the output directory by `ferry migrate` — it includes fields that require the original DCE exports to compute.

---

## `ferry check`

Verify a migration you have already run. Check reads the state file Ferry wrote and asks the live Stoat server whether everything it recorded is still there: every channel, role, category and emoji by id, and each channel's most recent messages. It is read-only, creating, editing and deleting nothing.

```
ferry check <output-dir> [OPTIONS]
```

Point it at the same output directory the migration used. Use it after a migration finishes, after a resume, or weeks later to confirm nothing has been lost.

This is not the same command as [`ferry validate`](#ferry-validate), which inspects a Discord export before you migrate it and never contacts a server.

### Options

| Flag | Environment Variable | Description |
|------|----------------------|-------------|
| `--stoat-url TEXT` | `STOAT_URL` | Stoat API base URL *(required)* |
| `--token TEXT` | `STOAT_TOKEN` | Your Stoat user token *(required)* |
| `--json` | | Print the report as a single JSON document instead of a table |

### What the four statuses mean

| Status | Meaning |
|--------|---------|
| `ok` | Found, and it matches what Ferry recorded |
| `warn` | It exists and its content is intact, but a name differs: a channel, a role or a category was renamed on the server |
| `fail` | Something Ferry recorded is gone, or a channel lost messages |
| `unverifiable` | Ferry cannot answer, and says why. Not a pass and not a failure |

`unverifiable` is worth reading rather than skimming past. It is the honest answer when Ferry never recorded what it would need to confirm something, which happens for good reasons: you migrated with `--thread-strategy=merge`, a send was accepted as a duplicate, or the token you gave Check cannot see a particular channel.

From 2.17.0 Ferry records which thread strategy a migration ran under, so Check names the cause that applies rather than listing all three. A migration run by an earlier version recorded no strategy, and Check still lists the possibilities for those.

### Exit codes

| Code | When |
|------|------|
| `0` | Every result was `ok`, `warn` or `unverifiable` |
| `1` | Any result was `fail`, or the migration could not be checked at all |

A `warn` on its own does not fail the command, because nothing has been lost. Use the exit code in a script, and `--json` when you want the individual results rather than a pass or fail.

### What Check cannot tell you

Check reads the most recent 100 messages in each channel and confirms the last one Ferry recorded sending is still among them. That is cheap enough to run on any server, and it is honest about what it misses:

- **A gap in the middle of a channel is invisible.** An `ok` means the recorded last message is present. It does not mean the channel is complete.
- **If more than 100 messages arrived after your migration**, Check can no longer see far enough back and reports `unverifiable` rather than guessing.
- **A renamed channel or role is only detected for migrations run under 2.17.0 or later.** Ferry records the names it gives channels and roles from that release on. A migration run by an earlier version recorded none, so there is nothing to compare against and a rename there stays invisible. Renamed *categories* have always been detected.
- **A duplicate forum index** is not detected.
- **Check will not run against a dry run.** A dry run records placeholders for things that were never created, so there is nothing on a server to compare with.

### Examples

```bash
# Check a migration you have just run
ferry check ./ferry-output --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"

# In a script: fail the job if anything is missing
if ! ferry check ./ferry-output; then
  echo "migration verification failed" >&2
  exit 1
fi
```

## `ferry repair`

Restore what `ferry check` found missing. Repair runs the check itself, then acts only on what it
reported as a failure.

```bash
ferry repair <output-dir> --export-dir <export-dir> [OPTIONS]
```

| Option | Description |
|---|---|
| `--export-dir PATH` | **Required.** The DCE export the original migration used |
| `--stoat-url URL` | Stoat API base URL. Or set `STOAT_URL` |
| `--token TOKEN` | Stoat user token. Prefer the `STOAT_TOKEN` environment variable |
| `--dry-run` | Report what would be repaired, and change nothing |

The export is required because the content lives there and not in the state file. Point repair at
the same export the migration used: a narrower one leaves repair unable to type a recreated channel
or to find a lost message, and it says so rather than guessing.

### What it repairs

- A **missing channel, role or category**, recreated under the name Ferry originally gave it, with
  its recorded permission overrides.
- A recreated channel's **category placement**, its permission overrides, its slowmode and its
  voice user limit.
- The **messages a recreated channel held**, re-sent in their original order, with the origin
  header if it was a thread.
- A **channel's lost last message**, re-sent into the channel that is still there.
- Anything left in the **dead-letter queue**, the same work `ferry retry` does.

### What it will not touch

- **A renamed channel, role or category.** A rename almost always means you renamed it. Repair fixes
  failures, not the edits you made on purpose.
- **Anything the check could not verify.** If Check could not look, repair has nothing to act on and
  guessing at a live server is worse than stopping.
- **A rolled-back migration.** Rollback keeps the identifier maps as an audit trail, so a check
  against a rolled-back state reports every channel missing. Repair refuses outright rather than
  rebuilding a server you chose to remove.
- **A missing forum index channel.** Its index message is derived content with nothing in the export
  to restore it from.
- **A recreated role's colour, rank, hoist setting or icon.** The name and the permissions come
  back; the rest does not, and repair says so.
- **A missing custom emoji.** An emoji's identifier *is* its uploaded file, so recreating one mints
  a different emoji rather than restoring the old one.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing is left failing |
| 1 | Something remains: a message would not send, or repair declined a defect it cannot fix |
| 2 | The state file or the export directory could not be read |
| 130 | Interrupted. State is saved after each repair, so re-running continues |

`--dry-run` always exits 0. It reports on a plan rather than an outcome, so a script that treats a
non-zero code as "act now" is not misled by a preview.

### Examples

```bash
# See what is wrong, then fix it
ferry check ./ferry-output --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN"
ferry repair ./ferry-output --export-dir ./export --stoat-url https://api.stoat.chat

# Preview first
ferry repair ./ferry-output --export-dir ./export --dry-run

# In a script: repair, then confirm against the server rather than trusting the tool
ferry repair ./ferry-output --export-dir ./export
ferry check ./ferry-output --json > result.json
```

## `ferry backfill-roles`

Set role ordering on a server that was migrated before the ordering fix (#380, shipped in v2.19.2).
Those servers have their roles at Stoat's default order. This command reads the same roles the
migration built and puts them back in Discord order, in one call.

```bash
ferry backfill-roles <output-dir> --export-dir <export-dir> [OPTIONS]
```

| Option | Description |
|---|---|
| `--export-dir PATH` | **Required.** The DCE export the original migration used |
| `--stoat-url URL` | Stoat API base URL. Or set `STOAT_URL` |
| `--token TOKEN` | Stoat user token. Prefer the `STOAT_TOKEN` environment variable |
| `--dry-run` | Report what would be reordered, and change nothing |

The export is required because the role positions live there and in `discord_metadata.json`, not in
the state file. Point it at the same export the migration used.

It is safe to re-run. A server that is already in order gets no write. Roles you added to the server
by hand after the migration keep their place; only the roles Ferry created are moved.

### What it does not touch

- **A role's colour, rank-independent hoist setting or icon.** This command sets ordering only. Those
  attributes are tracked separately in issue #344.
- **The forward migration.** Migrations run on v2.19.2 or later already order roles correctly and do
  not need this.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Ordering was applied, or the server was already in order |
| 1 | Ordering was refused: Ferry lacks the ManageRole permission, holds no role above the ones it tried to move, or a read-back failed |
| 2 | The state file or the export directory could not be read |

`--dry-run` always exits 0.

### Examples

```bash
# Correct the role order on a server migrated by an older Ferry
ferry backfill-roles ./ferry-output --export-dir ./export --stoat-url https://api.stoat.chat

# Preview first
ferry backfill-roles ./ferry-output --export-dir ./export --dry-run
```

## `ferry retry`

Re-send the messages that failed during a migration. These are the ones Ferry recorded in its
dead-letter queue: sends that were refused or timed out, and were never retried.

```bash
ferry retry <output-dir> --export-dir <export-dir> [OPTIONS]
```

| Option | Description |
|---|---|
| `--export-dir PATH` | **Required.** The DCE export the original migration used |
| `--stoat-url URL` | Stoat API base URL. Or set `STOAT_URL` |
| `--token TOKEN` | Stoat user token. Prefer the `STOAT_TOKEN` environment variable |

Cheaper than `ferry repair`, and narrower. It contacts the server only to send, where repair spends
a request per channel checking first. Use `retry` when you already know messages failed, and
`repair` when you want to find out what is wrong.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The queue is empty |
| 1 | Something is still failing |
| 2 | The state file or the export directory could not be read |
| 130 | Interrupted |

## `ferry probe`

Check what a live Stoat instance actually supports before you migrate to it. Probe measures upload size limits, checks whether voice channels can be created (Stoat Bug #194), checks webhook availability, and inspects rate-limit behaviour. It is most useful for self-hosted instances, where limits differ from the official service.

```
ferry probe --test-server-id <id> [OPTIONS]
```

Probe creates its test entities (channels, uploads) on the server you point it at, and deletes everything it created before exiting. Use a throwaway server, not your production server. It never writes a migration state file.

### Options

| Flag | Environment Variable | Description |
|------|----------------------|-------------|
| `--stoat-url TEXT` | `STOAT_URL` | Stoat API base URL *(required)* |
| `--token TEXT` | `STOAT_TOKEN` | Your Stoat user token *(required)* |
| `--test-server-id TEXT` | | ID of a throwaway Stoat server to create test entities on *(required)* |
| `--deep` | | Upload test files at each Autumn size boundary and report where enforced limits disagree with advertised ones. Creates and tears down probe entities (channels, emoji, server icon/banner). Leaves a small number of orphaned files in Autumn storage. |
| `--json` | | Print results as machine-readable JSON instead of a table |
| `--verbose` / `-v` | | Verbose output |

### What it checks

- **Upload size limits** — the limits the instance's file server (Autumn) advertises per upload category, compared against the values Ferry assumes. A mismatch is reported as a warning.
- **Upload boundary enforcement** *(with `--deep`)* — uploads test files at the exact advertised limit and one byte over for each upload category. Reports whether the server accepts or rejects each. Non-attachment tags require valid image files, which the probe generates as padded PNGs. Probe entities are torn down afterwards, but uploaded bytes persist in Autumn.
- **Voice channels** — creates a test voice channel and checks whether the instance actually supports voice (Stoat Bug #194). If not, Discord voice channels will become text channels.
- **Webhooks** — creates a test channel and checks whether webhooks can be created on the instance.
- **Rate limiting** — what rate-limit information the instance exposes in its responses.

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Probe ran and results were printed |
| `1` | Missing `--stoat-url` or `--token` |

### Examples

```bash
# Probe the official hosted service
ferry probe --stoat-url https://api.stoat.chat --token "$STOAT_TOKEN" \
  --test-server-id 01ABCDEF234567890ABCDEFGH

# Probe a self-hosted instance and save the results as JSON
ferry probe --stoat-url https://stoat.example.com --token "$STOAT_TOKEN" \
  --test-server-id 01ABCDEF234567890ABCDEFGH --json > probe-results.json
```

---

## `ferry tls-check`

Report which certificate authorities Ferry currently trusts for outbound HTTPS. Read-only: it makes no network calls itself. Use it to diagnose certificate errors such as `unable to get local issuer certificate`. See [Certificate Errors](troubleshooting.md#unable-to-get-local-issuer-certificate) in the troubleshooting guide.

```
ferry tls-check
```

No options or flags.

### What it prints

A block of `key: value` lines. The trust keys are always present; the proxy keys reflect whatever Ferry resolved from the environment on this run.

| Key | Meaning |
|------|-------------|
| `ca-bundle` | Path to the certifi CA bundle Ferry carries |
| `ca-bundle-readable` | `true` if that bundle exists on disk and could be read, `false` otherwise |
| `trust-source` | `union` if Ferry loaded the bundle successfully on top of the operating system's trust store, `fallback` if it could not and is relying on the OS store alone |
| `ca-visible` | Number of CA certificates the resulting SSL context reports |
| `proxy-http` | The `HTTP_PROXY` value Ferry resolved for plain-HTTP requests (credentials redacted), or `none` |
| `proxy-https` | The `HTTPS_PROXY` value Ferry resolved for HTTPS requests (credentials redacted), or `none` |
| `proxy-source` | Where the proxy came from: `env`, `system`, or `none` |
| `proxy-disabled` | `true` when `FERRY_DISABLE_PROXY` is set and Ferry is ignoring any configured proxy |

**Example output:**

```
ca-bundle: /path/to/certifi/cacert.pem
ca-bundle-readable: true
trust-source: union
ca-visible: 179
proxy-http: none
proxy-https: none
proxy-source: none
proxy-disabled: false
```

!!! info "A low ca-visible count is not necessarily a problem"
    On Linux, trust often resolves through OpenSSL's hashed capath directory rather than a single readable bundle. `ca-visible` cannot enumerate certificates trusted that way, so it can read `0` on a machine where TLS handshakes work fine. Treat `ca-visible` as a diagnostic hint, not a health check. `trust-source: union` is the line that confirms Ferry's own bundle loaded.

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Always. A missing or unreadable bundle is reported as `trust-source: fallback`, not a nonzero exit. |

### Examples

```bash
ferry tls-check
```

Run this first when a migration fails with a certificate error, before changing proxy or antivirus settings. `trust-source: fallback` means Ferry's own bundle did not load and you are relying on the operating system's trust store alone.
