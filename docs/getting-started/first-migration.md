# Your First Migration

This guide walks you through migrating a Discord server to Stoat from start to finish. It assumes you have already followed the [install guide](install.md) and the [Stoat setup guide](setup-stoat.md).

---

## Prerequisites checklist

Before you start, confirm you have these ready:

**For 1-Click Migration (recommended):**

- [ ] Your **Discord user token** — a secret key from your browser that gives Ferry temporary access to your Discord account ([how to find it](https://github.com/Tyrrrz/DiscordChatExporter/wiki/Obtaining-Token))
- [ ] Your **Discord server ID** — right-click the server name in Discord and choose "Copy Server ID" (you may need to enable Developer Mode in Discord settings first)
- [ ] Your **Stoat API URL** — `https://api.stoat.chat` for the official service, or your self-hosted domain
- [ ] Your **Stoat user token** — a secret key your browser saves when you log in to Stoat. No bot or app creation needed. ([how to find it](setup-stoat.md#2-get-your-stoat-user-token))

**For Offline Migration (advanced):**

- [ ] Your **Discord export folder** — produced by DiscordChatExporter, a free tool described in the [export guide](export-discord.md)
- [ ] Your **Stoat API URL** and **Stoat user token** — same as above

!!! warning "Discord token security"
    Your Discord token gives full access to your account. Never share it. Ferry does not store it to disk — it is held in memory only during the export.

!!! warning "Discord Terms of Service"
    Using a user token with third-party tools may violate Discord's Terms of Service. Ferry displays a disclaimer checkbox before proceeding. Use at your own risk.

---

## Step 1: Open Ferry

=== "GUI (Windows / macOS)"

    1. Double-click **Ferry.exe** (Windows) or open **Ferry.app** (macOS).
    2. Your default browser opens automatically at `http://localhost:8765`.
    3. You will see the Setup screen.

    <!-- screenshot: ferry-setup-screen -->

    !!! info "Browser did not open?"
        Open your browser manually and go to `http://localhost:8765`.

=== "CLI (Linux / advanced)"

    1. Open a terminal (the text command window — search "Terminal" in your applications).
    2. Run the migrate command with your credentials:

    ```
    ferry migrate \
      --discord-token YOUR_DISCORD_TOKEN \
      --discord-server YOUR_SERVER_ID \
      --stoat-url https://api.stoat.chat \
      --token YOUR_STOAT_TOKEN
    ```

    Required flags (`--stoat-url` and `--token`) must be passed on the command line or set as environment variables (`STOAT_URL`, `STOAT_TOKEN`). See Step 2 for full options.

---

## Step 2: Configure

=== "GUI (Windows / macOS)"

    Fill in the fields on the Setup screen:

    **1-Click Migration mode (default):**

    1. **Discord token** — paste your Discord user token (masked input).
    2. **Discord server ID** — paste the server ID.
    3. **Acknowledge the ToS disclaimer** — check the checkbox.
    4. **Stoat API URL** — select Official or Self-hosted.
    5. **Stoat user token** — paste the token you copied from your browser (see [how to find it](setup-stoat.md#2-get-your-stoat-user-token)).

    **Offline mode ("I already have exports"):**

    1. Toggle to **"I already have exports"**.
    2. **Export folder** — browse to your DCE export folder.
    3. **Stoat API URL** and **Stoat user token** — same as above.

    <!-- screenshot: ferry-setup-filled -->

    !!! tip "Advanced Options"
        Expand the **Advanced Options** section if you need to:

        - Adjust the rate limit delay (default 1.0 second between messages — increase if you see rate limit warnings)
        - Skip specific phases: emoji, messages, reactions, or threads/forum posts
        - Run a dry run to validate structure mapping without making API calls
        - Migrate into an existing Stoat server instead of creating a new one

=== "CLI (Linux / advanced)"

    **Orchestrated mode (recommended):**

    ```
    ferry migrate \
      --discord-token YOUR_DISCORD_TOKEN \
      --discord-server YOUR_SERVER_ID \
      --stoat-url https://api.stoat.chat \
      --token YOUR_STOAT_TOKEN
    ```

    **Offline mode (with existing exports):**

    ```
    ferry migrate \
      --export-dir ./path/to/export/ \
      --stoat-url https://api.stoat.chat \
      --token YOUR_STOAT_TOKEN
    ```

    Additional flags:

    | Flag | Effect |
    |------|--------|
    | `--skip-messages` | Import structure only, no messages |
    | `--skip-emoji` | Do not migrate custom emoji |
    | `--skip-reactions` | Do not migrate message reactions |
    | `--skip-threads` | Do not migrate threads or forum posts |
    | `--rate-limit 2.0` | Set seconds between messages (default `1.0`) |
    | `--server-id EXISTING_ID` | Migrate into an existing Stoat server |
    | `--dry-run` | Run all phases without making any API calls |
    | `--max-channels N` | Channel limit (default `200`; raise for self-hosted) |
    | `--max-emoji N` | Emoji limit (default `100`; raise for self-hosted) |
    | `--thread-strategy` | `flatten` (default), `merge`, or `archive` |
    | `--incremental` | Delta migration — only new messages since last run |
    | `--force` | Override freshness and soft error checks |
    | `--verify-uploads` | Post-upload file size verification |
    | `--cleanup-orphans` | Report unreferenced Autumn uploads |
    | `--force-unlock` | Clear stale migration lock from server |
    | `--skip-dce-verify` | Skip DCE binary SHA-256 verification |

---

## Step 2.5: Export (1-Click mode only)

=== "GUI (Windows / macOS)"

    After clicking **Continue**, the Export screen appears. Ferry:

    1. Validates your Discord token
    2. Downloads DiscordChatExporter if not already present
    3. Exports all channels, threads, and media from your Discord server
    4. Shows per-channel progress

    This step is automatic. When it finishes, Ferry moves to the Validate screen.

    !!! info ".NET Runtime required on macOS and Linux"
        DCE requires the .NET 8 runtime (a software framework from Microsoft that DCE needs to run). If Ferry detects it is missing, it will show an error with a download link. Windows users are not affected — the Windows version of DCE includes everything it needs.

=== "CLI (Linux / advanced)"

    In orchestrated mode, the export runs automatically before validation. You will see progress output as DCE exports each channel.

---

## Step 3: Validate

Ferry checks your export files locally before making any API calls. Nothing is sent to Stoat during this step.

=== "GUI (Windows / macOS)"

    1. Click **Validate Export**.
    2. Ferry parses all files and displays:
        - Server name and export date
        - Counts: channels, messages, attachments, emoji
        - Warnings (amber) for any issues it finds
        - Estimated migration time
        - A green, amber, or red status indicator

    <!-- screenshot: ferry-validate-screen -->

    !!! info "What amber warnings mean"
        Amber warnings are not blockers. Common examples: some attachments were not downloaded with the export, or some message types will be skipped. Ferry will still proceed — check the details so you know what to expect.

    3. If the status is green or amber, click **Start Migration** to continue.
    4. If the status is red, the export is missing required data. Return to the export guide and re-export with the correct flags.

=== "CLI (Linux / advanced)"

    Run the standalone validate command for a validate-only check:

    ```
    ferry validate ./export/
    ```

    This prints the same summary to your terminal without starting a migration.

    !!! tip
        If you run `ferry migrate` without validating first, Ferry runs validation automatically before starting.

---

## Step 3.5: Review what will be created

Before creating anything on Stoat, Ferry shows a review summary.

=== "GUI (Windows / macOS)"

    A dialog appears showing how many roles, categories, channels, emoji, and messages will be migrated. Review the numbers and any warnings, then click **Proceed** to continue or **Cancel** to go back.

=== "CLI (Linux / advanced)"

    The CLI prints a summary table before proceeding. In orchestrated mode this happens automatically; in offline mode it appears after validation.

!!! info "Permission migration"
    When you provide a Discord token (1-Click mode), Ferry fetches role permissions and channel overrides directly from the Discord API and translates them to Stoat equivalents. In offline mode (no Discord token), roles are created without permissions — you will need to set them manually on Stoat.

---

## Step 4: Start the migration

=== "GUI (Windows / macOS)"

    1. Click **Proceed** on the review dialog (or **Start Migration** if review was skipped).
    2. The Progress screen appears and shows:
        - Phase indicator — 12 phases, each with a checkmark when complete
        - Progress bar during the message import phase
        - Running totals: messages sent, attachments uploaded, errors
        - Live log stream at the bottom
        - **Pause / Resume** button
        - **Cancel** button (saves progress so you can resume later)

    <!-- screenshot: ferry-migrate-progress -->

    !!! warning "Do not close Ferry during migration"
        Closing the app window or the terminal stops the migration. Use the **Pause** button if you need to stop temporarily. Ferry saves its progress so you can resume from where it left off.

=== "CLI (Linux / advanced)"

    The migration starts immediately after validation. You will see formatted progress output in your terminal:

    - One progress bar per channel, updated in real time
    - Live stats: messages sent, attachments uploaded, errors
    - Any warnings or skipped items are printed as they occur

    !!! info "Parallel channel sends"
        The Messages phase processes multiple channels concurrently (default: 3 at a time). You will see progress bars for several channels running simultaneously. To adjust the concurrency level, use the GUI's Advanced Options or see the [large-servers guide](../guides/large-servers.md).

    To pause and resume later, press `Ctrl+C`. Run the same command again with `--resume` to continue from the last checkpoint.

---

## Step 5: Done

=== "GUI (Windows / macOS)"

    When all 12 phases complete, Ferry shows a summary card with:

    - Total channels created
    - Total messages migrated
    - Total attachments uploaded
    - Error count (click to view details)
    - **Fidelity score** — a quantified measure of migration quality

    Click **Open Report** to view the full migration report in your browser.

    <!-- screenshot: ferry-complete-screen -->

=== "CLI (Linux / advanced)"

    Ferry prints a summary table when it finishes, including a **fidelity score** — a quantified measure of migration quality (messages migrated vs. source total, attachment success rate, and other factors). The full report is saved to:

    ```
    ferry-output/report.json
    ```

    Other files written to the output directory:

    | File | Contents |
    |------|----------|
    | `state.json` | Channel, role, and emoji ID mappings (used for resume and incremental runs) |
    | `message_map.json` | Discord message ID → Stoat message ID mapping (used for reply linking and incremental runs) |
    | `discord_metadata.json` | Server structure and permission data fetched from Discord API |
    | `migration_report.md` | Human-readable summary with fidelity score and per-channel stats |
    | `report.json` | Machine-readable full report including error details |

---

## What your Stoat server will look like

After a successful migration:

- All channels and categories are created in the same structure as Discord
- NSFW channels are correctly flagged
- Channel permission overrides (@everyone and per-role) are applied
- Forum posts are grouped into dedicated categories named after the parent forum
- Roles are recreated with colours, rank ordering, and permissions preserved
- Messages appear under the original author's name and avatar, so conversations look natural
- Original timestamps appear at the start of each message: `*[2024-01-15 14:30 UTC]*`
- Embeds are preserved with uploaded thumbnails and images
- Polls are rendered as formatted text in the message body
- Sticker images are uploaded as attachments (with text fallback for unsupported formats)
- Pinned messages are re-pinned in their channels
- Custom emoji are available in the server
- All messages are sent silently (no notification spam during migration)

!!! info "Why do messages show a timestamp at the start?"
    Stoat does not support importing historical message timestamps. Ferry embeds the original date and time as the first line of each message so the conversation history stays readable.

---

## What if something goes wrong?

**Migration paused or crashed?**

=== "GUI (Windows / macOS)"

    Reopen Ferry. On the Setup screen, check **Resume previous migration** and click **Start**. Ferry picks up from the last checkpoint.

=== "CLI (Linux / advanced)"

    Add `--resume` to your original command:

    ```
    ferry migrate --discord-token ... --discord-server ... --stoat-url ... --token ... --resume
    ```

**Seeing errors in the log?**

Most errors are non-fatal. Ferry logs them and continues. A failed attachment upload or a skipped message does not stop the whole migration. Check `ferry-output/report.json` for a list of everything that was skipped and why.

**Rate limit warnings?**

Slow Ferry down to give the Stoat server more breathing room:

=== "GUI (Windows / macOS)"

    Go back to the Setup screen, expand **Advanced Options**, and increase the rate limit delay slider.

=== "CLI (Linux / advanced)"

    Add `--rate-limit 2.0` (or higher) to your command.

!!! tip "Need more help?"
    See the [troubleshooting guide](../guides/troubleshooting.md) for a full list of error messages and fixes.
