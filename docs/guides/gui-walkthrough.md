# GUI Walkthrough

This guide walks through every screen of the Discord Ferry web interface. Ferry runs a small local web server when you launch it — no data ever leaves your machine.

!!! info "Launching the GUI"
    Double-click `ferry.exe` (Windows) or `ferry` (macOS/Linux). Your browser will open automatically to `http://localhost:8765`. If it does not, open that URL manually.

---

## Setup Screen

The first screen collects the information Ferry needs before it can begin. Ferry has two modes — **1-Click Migration** (default) and **Offline mode**.

<!-- screenshot: setup screen with all fields visible -->

### 1-Click Migration (default)

In this mode, Ferry downloads and runs DiscordChatExporter for you automatically.

**Discord token** — paste your Discord user token (masked input). Click "How to find these?" for step-by-step instructions.

**Discord server ID** — paste the server ID (right-click the server name in Discord > Copy Server ID).

**ToS disclaimer** — check the checkbox to acknowledge that using a user token may violate Discord's Terms of Service.

**Stoat API URL** — select Official (`https://api.stoat.chat`) or enter your self-hosted domain.

**Stoat user token** — paste the token you copied from your browser's developer tools (masked input). See [how to find it](../getting-started/setup-stoat.md#2-get-your-stoat-user-token). No bot or app creation needed — this is a key your browser already has.

### Offline Mode ("I already have exports")

Toggle **"I already have exports"** to switch to offline mode. The Discord token and server ID fields are replaced with:

**Export folder** — paste or browse to your DiscordChatExporter export folder. The folder should contain one or more `.json` files and a `media/` subfolder.

!!! warning "Media folder required"
    If you exported without the `--media` flag, attachments will not migrate. Re-export with `--media` before continuing.

### Advanced Options

Click **Advanced Options** to expand the following settings. Defaults are safe for most migrations.

| Option | Default | Description |
|--------|---------|-------------|
| Rate limit (seconds) | 1.0 | Delay between messages. Range 0.5–3.0. Lower is faster but risks hitting Stoat's speed limit on how fast data can be sent. |
| Skip messages | Off | Import server structure only (channels, roles, categories). No messages will be sent. |
| Skip emoji | Off | Do not upload custom emoji. |
| Skip reactions | Off | Do not add message reactions. |
| Skip threads | Off | Do not migrate threads or forum posts. Useful when approaching the 200-channel limit. |
| Thread strategy | Flatten | How to handle threads and forum posts. **Flatten** (default) creates a dedicated channel for each thread. **Merge** appends thread messages into the parent channel. **Archive** exports the thread as a markdown attachment in the parent channel. |
| Dry run | Off | Run all migration phases without actually contacting the Stoat server. Useful for validating your export before committing to a full migration. |
| Existing server ID | *(empty)* | Paste a Stoat server ID to migrate into a server you have already created, rather than creating a new one. |
| Checkpoint interval | 50 | How often migration state is saved (every N messages). Lower = safer but more I/O. Minimum 1. |
| Skip avatars | Off | Skip the avatar pre-flight phase. Avatars will be uploaded on-demand during messages instead. |
| Reaction mode | Text | How to handle reactions: **Text** (default) appends `[Reactions: emoji count]` to message content — zero extra API calls. **Native** applies reactions via API (slower). **Skip** ignores reactions entirely. |
| Min thread messages | 0 | Exclude threads with fewer than this many messages. 0 includes all threads. Useful for servers with hundreds of low-activity threads. |
| Validate after | Off | Run a post-migration validation that compares Stoat server against the migration state. Reports discrepancies. |

!!! tip "Running into 'Too Many Requests' errors?"
    That error (code 429) means Stoat is asking Ferry to slow down. Increase the rate limit slider to 2.0 or 3.0 seconds. This slows the migration but eliminates the errors.

### Continue Button

Click **Continue** when all required fields are filled. In 1-Click mode, Ferry moves to the Export screen. In offline mode, Ferry parses your export locally and moves to the Validate screen.

---

## Validate Screen

Ferry has parsed your export and is showing you a summary before anything is sent to Stoat.

<!-- screenshot: validate screen showing counts table and warnings list -->

### Source Server Info

The server name and export date from the DCE export appear at the top of the screen.

### Counts Table

| Item | What it counts |
|------|----------------|
| Channels | Text, voice, and announcement channels |
| Categories | Channel categories (groupings) |
| Roles | Server roles |
| Messages | Total messages across all channels |
| Attachments | Files and images attached to messages |
| Custom emoji | Server-specific emoji |
| Threads | Forum posts and threaded conversations |

### Warnings List

Any issues found during parsing are listed with amber indicators. Common warnings:

- **Rendered markdown detected** — the export may have been made without `--markdown false`. Mention syntax may be lost.
- **Attachment files missing** — one or more attachment files were not found locally. Those files will be skipped.
- **Channel limit may be exceeded** — the combined channel and thread count exceeds 200.
- **Emoji limit will be reached** — the server has more than 100 custom emoji. Only the first 100 will be migrated.

!!! info "Warnings vs errors"
    Amber warnings allow migration to proceed. A red error (for example, no valid JSON files found) disables the **Start Migration** button until it is resolved.

### ETA Estimate

Based on your message count and the rate limit you chose, Ferry shows an estimated duration. Long migrations should be left to run overnight.

### Overall Status

- **Green** — everything looks good, migration can proceed.
- **Amber** — warnings present, migration can proceed but review the warnings above.
- **Red** — a blocking error was found. Migration is disabled until you resolve it.

Use the **Back** button to return to the Setup screen and adjust settings, or click **Start Migration** to begin.

---

## Export Screen (1-Click Mode Only)

This screen appears only when you use 1-Click Migration. Ferry downloads and runs DiscordChatExporter automatically.

<!-- screenshot: export screen showing progress -->

### What Happens

Ferry runs through three steps automatically:

1. **Token validation** — confirms your Discord token works via the Discord API.
2. **DCE download** — if DiscordChatExporter is not cached locally, Ferry downloads the correct version for your operating system.
3. **Channel export** — DCE exports all channels, threads, and media from your Discord server. Progress is shown per-channel.

### Cached Exports

If Ferry detects cached export files from a previous run, it shows a summary (file count and total size) and offers two choices:

- **Use cached exports** — skip re-exporting and go straight to validation.
- **Re-export** — discard cached files and export fresh.

This is useful when resuming after a crash or when you want to re-run the migration without re-downloading everything.

### .NET Runtime

DCE requires the .NET 8 runtime on macOS and Linux. If Ferry detects it is missing, it shows an error with a download link. Windows users are not affected — the Windows DCE build is self-contained.

When the export completes, Ferry automatically moves to the Validate screen.

---

## Review Dialog

Before creating anything on Stoat, Ferry shows a confirmation dialog summarising what will be created.

<!-- screenshot: review dialog showing summary table -->

### What It Shows

The dialog displays a summary table:

| Item | Description |
|------|-------------|
| Roles | Number of roles to create (excluding @everyone) |
| Categories | Number of channel categories |
| Channels | Number of text and voice channels |
| Custom emoji | Number of emoji to upload |
| Messages | Total messages to migrate |
| Threads | Number of threads/forum posts |

### Warnings

If potential issues are detected, they appear below the summary:

- **No Discord token provided** — permissions and NSFW flags will not be migrated (these require the Discord API)
- **Channel limit may be exceeded** — combined channel and thread count is close to or over 200
- **Emoji limit may be exceeded** — more than 100 custom emoji detected

### Actions

- **Proceed** — start creating the server on Stoat
- **Cancel** — return to the Validate screen without creating anything

!!! info "Why review before creating?"
    Server creation on Stoat is not easily undone. The review step lets you verify the scope of the migration before Ferry contacts the Stoat server. This is especially useful for large servers where mistakes are costly.

---

## Migrate Screen

The main migration screen. Ferry works through 12 sequential phases.

<!-- screenshot: migrate screen mid-migration showing phase indicators and progress bar -->

### Phase Indicator

The 12 phases are shown in order, with a checkmark as each completes:

1. **Export** — run DiscordChatExporter (skipped in offline mode)
2. **Validate** — confirm export is readable
3. **Connect** — verify Stoat credentials
4. **Server** — create or connect to the target server
5. **Roles** — create all server roles, then apply Discord permissions (translated to Stoat equivalents)
6. **Categories** — create channel categories
7. **Channels** — create all channels with NSFW flags, then apply per-channel permission overrides
8. **Emoji** — upload custom emoji
9. **Messages** — send all messages
10. **Reactions** — add message reactions
11. **Pins** — pin messages
12. **Report** — write summary report with post-migration checklist

### Progress Bar

During the **Messages** phase, a per-channel progress bar shows how many messages have been sent in the current channel and how many remain. In v2.0.0+, up to 3 channels are processed concurrently by default — the progress bar reflects the active channel workers simultaneously.

### Running Totals

A live counter in the top-right area shows:

- **Messages sent** — total messages delivered to Stoat
- **Attachments uploaded** — files successfully uploaded to Stoat's file storage
- **Errors** — messages or items that could not be migrated
- **Warnings** — non-fatal issues logged

### Live Log Stream

The lower half of the screen shows a scrolling log of activity. The log auto-scrolls to the latest entry. You can scroll up to review earlier entries.

### ETA Countdown

A live estimate of time remaining updates as messages are sent.

### Pause / Resume

Click **Pause** to temporarily stop the migration after the current message finishes. Click **Resume** to continue. Pausing is useful if you need to reduce load on your machine temporarily.

### Cancel

Click **Cancel** to stop the migration entirely. Ferry saves its state to disk before stopping. To continue later, re-launch Ferry with the same export folder. On the Migrate screen, Ferry will detect the previous migration state and offer a **Resume** or **Start Fresh** choice (or use `--resume` on the CLI).

!!! warning "Do not close the browser tab during migration"
    Closing the tab while migration is running does not stop Ferry — it continues in the background. However, you will lose visibility into progress. Leave the tab open, or use the CLI if you need a more robust background process.

---

## Completion Screen

When all phases finish, the Completion screen shows a summary card with final statistics: messages sent, attachments uploaded, errors, elapsed time, and a **fidelity score** — a 0–100 measure of migration quality based on messages, attachments, embeds, replies, and reactions.

<!-- screenshot: completion screen with summary card -->

Click **Open Report** to open the migration report in your browser. Two report files are saved to the `ferry-output/` folder:

- `migration_report.md` — a human-readable summary you can share with your community
- `report.json` — a machine-readable report with full error details and ID mappings
