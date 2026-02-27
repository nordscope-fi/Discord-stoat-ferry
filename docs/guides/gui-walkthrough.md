# GUI Walkthrough

This guide walks through every screen of the Discord Ferry web interface. Ferry runs a small local web server when you launch it — no data ever leaves your machine.

!!! info "Launching the GUI"
    Double-click `ferry.exe` (Windows) or `ferry` (macOS/Linux). Your browser will open automatically to `http://localhost:8765`. If it does not, open that URL manually.

---

## Setup Screen

The first screen collects the information Ferry needs before it can begin.

<!-- screenshot: setup screen with all fields visible -->

### Export Folder

Paste or type the path to your DiscordChatExporter export folder, or click **Browse** to open a file picker. The folder should contain one or more `.json` files and a `media/` subfolder.

!!! warning "Media folder required"
    If you exported without the `--media` flag, attachments will not migrate. Re-export with `--media` before continuing.

### Stoat API URL

The base URL of your Stoat server. For the official hosted service, use `https://api.stoat.chat`. For a self-hosted instance, enter your own domain (for example, `https://stoat.example.com`).

### Stoat Token

Your personal account token. This field is masked — the characters are hidden as you type.

!!! warning "Use a user token, not a bot token"
    Bot tokens cannot create servers. Open your Stoat web client, open your browser developer tools (F12), go to **Application > Local Storage**, and copy the `token` value.

### Advanced Options

Click **Advanced Options** to expand the following settings. Defaults are safe for most migrations.

| Option | Default | Description |
|--------|---------|-------------|
| Rate limit (seconds) | 1.0 | Delay between messages. Range 0.5–3.0. Lower is faster but risks hitting rate limits. |
| Skip messages | Off | Import server structure only (channels, roles, categories). No messages will be sent. |
| Skip emoji | Off | Do not upload custom emoji. |
| Skip reactions | Off | Do not add message reactions. |
| Dry run (no API calls) | Off | Run all migration phases without making any API calls. Useful for validating structure mapping before committing to a full migration. |
| Existing server ID | *(empty)* | Paste a Stoat server ID to migrate into a server you have already created, rather than creating a new one. |

!!! tip "Running into 429 errors?"
    Increase the rate limit slider to 2.0 or 3.0 seconds. This slows the migration but eliminates rate-limit errors.

### Validate Export Button

Click **Validate Export** when all required fields are filled. Ferry parses your export locally and moves to the Validate screen. No network calls are made at this stage.

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

## Migrate Screen

The main migration screen. Ferry works through 11 sequential phases.

<!-- screenshot: migrate screen mid-migration showing phase indicators and progress bar -->

### Phase Indicator

The 11 phases are shown in order, with a checkmark as each completes:

1. **Validate** — confirm export is readable
2. **Connect** — verify Stoat credentials
3. **Server** — create or connect to the target server
4. **Roles** — create all server roles
5. **Categories** — create channel categories
6. **Channels** — create all channels
7. **Emoji** — upload custom emoji
8. **Messages** — send all messages
9. **Reactions** — add message reactions
10. **Pins** — pin messages
11. **Report** — write summary report

### Progress Bar

During the **Messages** phase, a per-channel progress bar shows how many messages have been sent in the current channel and how many remain.

### Running Totals

A live counter in the top-right area shows:

- **Messages sent** — total messages delivered to Stoat
- **Attachments uploaded** — files successfully uploaded to Autumn
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

When all phases finish, the Completion screen shows a summary card with final statistics: messages sent, attachments uploaded, errors, and elapsed time.

<!-- screenshot: completion screen with summary card -->

Click **Open Report** to open the migration report in your browser. The report is also saved as `migration_report.json` in the `ferry-output/` folder next to your export.
