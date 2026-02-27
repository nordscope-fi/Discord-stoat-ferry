# Your First Migration

This guide walks you through migrating a Discord server to Stoat from start to finish. It assumes you have already followed the [install guide](install.md), the [export guide](export-discord.md), and the [setup guide](setup-stoat.md).

---

## Prerequisites checklist

Before you start, confirm you have all three of these ready:

- [ ] Your Discord export folder (produced by DiscordChatExporter — contains `.json` files and a `media/` subfolder)
- [ ] Your Stoat API URL (e.g. `https://api.stoat.chat` for the official host, or your self-hosted address)
- [ ] Your Stoat user token (from your Stoat account settings)

!!! warning "Token security"
    Your Stoat token gives full access to your account. Never share it, and never paste it into a chat message or public document.

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

    1. Open a terminal.
    2. Run the migrate command, pointing at your export folder:

    ```
    ferry migrate ./path/to/export/
    ```

    Ferry will prompt for any missing options interactively, or you can pass them all as flags (see Step 2).

---

## Step 2: Configure

=== "GUI (Windows / macOS)"

    Fill in the three required fields on the Setup screen:

    1. **Export folder** — click **Browse** and select the folder, or paste the full path directly.
    2. **Stoat API URL** — paste the URL from your setup guide (e.g. `https://api.stoat.chat`).
    3. **Stoat Token** — paste your token. It will be masked as you type.

    <!-- screenshot: ferry-setup-filled -->

    !!! tip "Advanced Options"
        Expand the **Advanced Options** section if you need to:

        - Adjust the rate limit delay (default 1.0 second between messages — increase if you see rate limit warnings)
        - Skip specific phases: emoji, messages, or reactions
        - Migrate into an existing Stoat server instead of creating a new one

=== "CLI (Linux / advanced)"

    Pass options as flags on the command line:

    ```
    ferry migrate ./export/ \
      --stoat-url https://api.your-stoat.com \
      --token YOUR_TOKEN_HERE
    ```

    Additional flags:

    | Flag | Effect |
    |------|--------|
    | `--skip-messages` | Import structure only, no messages |
    | `--skip-emoji` | Do not migrate custom emoji |
    | `--skip-reactions` | Do not migrate message reactions |
    | `--rate-limit 2.0` | Set seconds between messages (default `1.0`) |
    | `--server-id EXISTING_ID` | Migrate into an existing Stoat server |
    | `--dry-run` | Run all phases without making any API calls |
    | `--max-channels N` | Channel limit (default `200`; raise for self-hosted) |
    | `--max-emoji N` | Emoji limit (default `100`; raise for self-hosted) |

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

## Step 4: Start the migration

=== "GUI (Windows / macOS)"

    1. Click **Start Migration**.
    2. The Progress screen appears and shows:
        - Phase indicator — 11 phases, each with a checkmark when complete
        - Progress bar during the message import phase
        - Running totals: messages sent, attachments uploaded, errors
        - Live log stream at the bottom
        - **Pause / Resume** button
        - **Cancel** button (saves progress so you can resume later)

    <!-- screenshot: ferry-migrate-progress -->

    !!! warning "Do not close Ferry during migration"
        Closing the app window or the terminal stops the migration. Use the **Pause** button if you need to stop temporarily. Ferry saves its progress so you can resume from where it left off.

=== "CLI (Linux / advanced)"

    The migration starts immediately after validation. You will see Rich-formatted progress output:

    - One progress bar per channel, updated in real time
    - Live stats: messages sent, attachments uploaded, errors
    - Any warnings or skipped items are printed as they occur

    To pause and resume later, press `Ctrl+C`. Run the same command again with `--resume` to continue from the last checkpoint.

---

## Step 5: Done

=== "GUI (Windows / macOS)"

    When all 11 phases complete, Ferry shows a summary card with:

    - Total channels created
    - Total messages migrated
    - Total attachments uploaded
    - Error count (click to view details)

    Click **Open Report** to view the full migration report in your browser.

    <!-- screenshot: ferry-complete-screen -->

=== "CLI (Linux / advanced)"

    Ferry prints a summary table when it finishes. The full report is saved to:

    ```
    ferry-output/report.json
    ```

---

## What your Stoat server will look like

After a successful migration:

- All channels and categories are created in the same structure as Discord
- Roles and their permissions are recreated
- Messages appear under the original author's name (using Stoat's masquerade feature)
- Original timestamps appear at the start of each message: `*[2024-01-15 14:30 UTC]*`
- Pinned messages are re-pinned in their channels
- Custom emoji are available in the server

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
    ferry migrate ./export/ --stoat-url ... --token ... --resume
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
