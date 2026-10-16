# Export Your Discord Server

!!! tip "Using 1-Click Migration?"
    If you're using Ferry's 1-Click Migration mode, you don't need to manually run DiscordChatExporter; Ferry handles it automatically. This guide is for **offline mode** only.

This page walks you through exporting your Discord server's content using DiscordChatExporter (DCE).
The export is the raw material that Discord Ferry uses to migrate your server. If the export is
missing data or uses the wrong settings, messages, mentions, and attachments will not migrate
correctly.

**Read every warning box before running any command.**

---

## What You Will Need

- A computer running Windows, macOS, or Linux
- Access to your Discord account in a web browser (not the desktop app)
- Enough free disk space for your server's media (allow 2x your server's approximate file size)
- A Discord account that can view the channels you want to export and read their message history

---

## Step 1: Download DiscordChatExporter

DiscordChatExporter (DCE) is a free, open-source tool that reads Discord's API and saves your
server content to data files (in JSON format, which Ferry can read). Check the [releases page](https://github.com/Tyrrrz/DiscordChatExporter/releases) for the latest version.

1. Go to [https://github.com/Tyrrrz/DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter).
2. Click **Releases** in the right sidebar.
3. Download the version for your platform:

| Platform | Command-line file to download |
|----------|--------------------------------|
| Windows x64 | `DiscordChatExporter.Cli.win-x64.zip` |
| macOS, Apple silicon | `DiscordChatExporter.Cli.osx-arm64.zip` |
| macOS, Intel | `DiscordChatExporter.Cli.osx-x64.zip` |
| Linux x64 | `DiscordChatExporter.Cli.linux-x64.zip` |

4. Unzip the downloaded file to a folder you can easily find, such as `C:\Tools\DCE` on Windows
   or `~/Tools/DCE` on macOS/Linux.

<!-- screenshot: DiscordChatExporter GitHub releases page with the correct file highlighted -->

!!! info "GUI vs CLI"
    The Windows GUI app is easier to use for one-time exports, but Discord Ferry's instructions
    use the CLI (command-line interface, where you type commands into a text window) version because it gives you the exact flags needed.
    The CLI is available on all platforms. If you are on Windows and prefer a graphical app,
    ensure you still apply the settings described in Step 3.

These file names are from the [DiscordChatExporter 2.48 release assets](https://github.com/Tyrrrz/DiscordChatExporter/releases/tag/2.48).

---

## Step 2: Get a Discord Token

DiscordChatExporter accepts either a user token or a bot token. Use a token for an account that
can view the channels you want to export and read their message history.

!!! warning "Never share your token with anyone"
    A token gives the bearer access as that account. Do not paste it into chat, DMs, screenshots,
    or support tickets. Treat it like a password.

For instructions on obtaining a token, use the
[DiscordChatExporter guide](https://github.com/Tyrrrz/DiscordChatExporter/wiki/Obtaining-Token).
The upstream guide can keep pace with Discord's web application.

---

## Step 3: Find Your Server ID

1. Open Discord (browser or desktop app).
2. Go to **User Settings** (gear icon near the bottom-left) → **Advanced**.
3. Turn on **Developer Mode**.
4. Close settings.
5. Right-click your server's icon in the left sidebar.
6. Choose **Copy Server ID**.

The server ID is a long number, such as `987654321012345678`.

<!-- screenshot: Right-click menu on a Discord server icon showing "Copy Server ID" -->

---

## Step 4: Run the Export

!!! warning "You MUST include `--markdown false` and `--media`; read this before running anything"
    - **`--markdown false`**: Without this flag, DCE replaces raw mention data like `<@123456>`
      with plain text like `@Username`. Discord Ferry uses the raw IDs to remap mentions to Stoat
      users. If the IDs are gone, all mentions will appear as plain text in the migrated server.
    - **`--media`**: Downloads assets referenced by the export, including attached files and
      images. Ferry needs those local files to migrate attachments.

Open a terminal, the text command window (on Windows: Command Prompt or PowerShell; on macOS/Linux: the Terminal app). Navigate to the folder where you unzipped DCE.

Run the following command, replacing the two placeholders:

```
DiscordChatExporter.Cli exportguild \
  --token YOUR_TOKEN_HERE \
  -g YOUR_SERVER_ID \
  --media \
  --markdown false \
  --format Json \
  --include-threads All \
  --output ./export/
```

Replace:

| Placeholder | Replace with |
|-------------|-------------|
| `YOUR_TOKEN_HERE` | The token you obtained in Step 2 |
| `YOUR_SERVER_ID` | The server ID you copied in Step 3 |

!!! tip "Keeping the token out of your shell history"
    A token typed into a command is saved in your shell history. DiscordChatExporter also reads
    the token from a `DISCORD_TOKEN` environment variable, so you can set that once and leave
    `--token YOUR_TOKEN_HERE` out of the command. On macOS and Linux run
    `export DISCORD_TOKEN=YOUR_TOKEN_HERE` first. In PowerShell run
    `$env:DISCORD_TOKEN="YOUR_TOKEN_HERE"`.

**On Windows**, replace the backslashes `\` at the end of each line with a caret `^`, or paste
the entire command on a single line:

```
DiscordChatExporter.Cli exportguild --token YOUR_TOKEN_HERE -g YOUR_SERVER_ID --media --markdown false --format Json --include-threads All --output ./export/
```

!!! info "What each flag does"
    - `exportguild`: exports every channel in the server
    - `--media`: downloads all attachments, images, and files locally
    - `--markdown false`: preserves raw mention syntax needed for migration
    - `--format Json`: saves data as JSON (a standard data format), which Discord Ferry reads
    - `--include-threads All`: includes public and private thread history
    - `--output ./export/`: saves everything into a folder called `export` in the current directory

The export will print progress to the terminal. Let it run until it prints a completion message,
then check that it did not report any failed channels or downloads.

!!! warning "Use `--reuse-media` only after a completed export"
    DCE trusts every existing media file when this flag is present, including a file left incomplete
    by an interrupted download. If the previous export stopped, failed, or reported partial
    channel failures, rerun without `--reuse-media` so DCE replaces those files. After an export
    finishes without failed channels or downloads, you can add `--reuse-media` to a later
    re-export into the same folder.

<!-- screenshot: Terminal showing DCE export progress output -->

---

## Step 5: Verify the Export

When the export finishes, open the `export` folder. You should see:

1. **Data files** (one per channel), named like:
   ```
   My Server - general [987654321098765432].json
   My Server - announcements [876543210987654321].json
   ```

2. **Thread files** with three dash-separated segments, named like:
   ```
   My Server - help - how-do-i-set-up-roles [765432109876543210].json
   ```

3. **A `media` folder** containing downloaded attachments, images, and files organised into
   subfolders.

!!! warning "If you see no media folder or it is empty"
    The `--media` flag was not applied correctly. Re-run the command with `--media` included.
    Attachments cannot be migrated without the downloaded files.

!!! warning "If filenames do not include a number in brackets"
    The export did not complete correctly. The channel ID in brackets is required for Discord Ferry
    to map channels. Re-run the export.

!!! tip "Checking file sizes"
    Compare the total size of the `export` folder against your rough estimate of your server's
    media. If the folder is suspiciously small (a few kilobytes for a large active server), the
    export may have failed silently. Check the terminal output for error messages.

---

## Frequently Asked Questions

### How long does the export take?

It depends on your server's size and activity level.

- Small servers (under 10,000 messages, minimal media): a few minutes
- Medium servers (tens of thousands of messages): 30 minutes to a few hours
- Large or media-heavy servers: potentially many hours

DCE exports one channel at a time. If you stop it partway through, run it again with the same
`--output` folder and omit `--reuse-media`. DCE will replace any media file that the interrupted
run may have left incomplete.

### Can I export just some channels?

Yes. Use `export` instead of `exportguild` and provide specific channel IDs. The DCE 2.48 source
declares this command as `[Command("export", Description = "Exports one or multiple channels.")]` in
[`ExportChannelsCommand`](https://github.com/Tyrrrz/DiscordChatExporter/blob/905489b01b5523719082275c34c232fc47f827c6/DiscordChatExporter.Cli/Commands/ExportChannelsCommand.cs#L12-L25).

```
DiscordChatExporter.Cli export \
  --token YOUR_TOKEN_HERE \
  -c CHANNEL_ID_1 CHANNEL_ID_2 CHANNEL_ID_3 \
  --media \
  --markdown false \
  --format Json \
  --output ./export/
```

To find a channel ID: enable Developer Mode in Discord settings (see Step 3), then right-click a
channel name and choose **Copy Channel ID**.

### What about direct messages (DMs)?

Discord Ferry migrates server content only: channels, messages, roles, and emoji. DMs are
account-specific and are not migrated. Stoat does not have an equivalent import feature for DMs.

### I get an error about rate limits. What do I do?

Discord limits how fast third-party tools can read its API. If you see an error mentioning
rate limits or HTTP 429:

1. Stop the export.
2. Wait 10–15 minutes.
3. Re-run the same command without `--reuse-media`, so a download interrupted by the rate limit
   is replaced rather than trusted as complete.

For very large servers, consider exporting in smaller batches using `export` with a subset
of channel IDs at a time.

### The export finished but some channels are missing.

Channels you do not have permission to view will not be exported, even if you are a server
administrator. Check that the Discord account whose token you used has permission to view all
channels. Voice channel message history may also be unavailable depending on your server's
configuration.

---

## Next Step

Once your `export` folder is ready and you have verified it contains JSON files and a media
folder, proceed to [**Setting Up Stoat**](setup-stoat.md) to get your API URL and token.
