# Export Your Discord Server

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
- Server Manager or Administrator role on the Discord server you are migrating

---

## Step 1 — Download DiscordChatExporter

DiscordChatExporter (DCE) is a free, open-source tool that reads Discord's API and saves your
server content to JSON files. The current version is **2.46.1**.

1. Go to [https://github.com/Tyrrrz/DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter).
2. Click **Releases** in the right sidebar.
3. Download the version for your platform:

| Platform | File to download |
|----------|-----------------|
| Windows (recommended) | `DiscordChatExporter.zip` — includes a GUI (graphical) app |
| macOS / Linux | `DiscordChatExporter.Cli.zip` — command-line only |

4. Unzip the downloaded file to a folder you can easily find, such as `C:\Tools\DCE` on Windows
   or `~/Tools/DCE` on macOS/Linux.

<!-- screenshot: DiscordChatExporter GitHub releases page with the correct file highlighted -->

!!! info "GUI vs CLI"
    The Windows GUI app is easier to use for one-time exports, but Discord Ferry's instructions
    use the CLI (command-line interface) version because it gives you the exact flags needed.
    The CLI is available on all platforms. If you are on Windows and prefer a graphical app,
    ensure you still apply the settings described in Step 3.

---

## Step 2 — Get Your Discord User Token

Your Discord user token (a long string of letters, numbers, and symbols) acts like a temporary
password that lets DCE read your server on your behalf. This is different from a bot token.

!!! warning "Use a user token, NOT a bot token"
    Bot tokens cannot reliably export threads. Forum channels and thread history will be incomplete
    or missing entirely if you use a bot token. You must use your **user** token.

!!! warning "Never share your token with anyone"
    Your user token gives complete access to your Discord account — the same as your email and
    password combined. Do not paste it into chat, DMs, screenshots, or support tickets. Treat it
    like your bank password. If you accidentally share it, go to Discord Settings and change your
    password immediately; this invalidates the old token.

### How to find your user token

These steps use a web browser. Chrome, Firefox, and Edge all work.

1. Open **[https://discord.com/app](https://discord.com/app)** in your browser and log in if
   prompted.

2. Press **F12** (Windows/Linux) or **Option + Command + I** (macOS) to open the browser's
   developer tools panel (a panel with code and network information used by web developers).

3. Click the **Network** tab at the top of the developer tools panel.

<!-- screenshot: Browser developer tools open with Network tab selected -->

4. In the Network tab, look for a search or filter box and type `library` to filter requests.

5. In Discord's main window, click on any server or channel to trigger a network request.

6. A request named `library` (or similar) should appear in the list. Click it.

7. In the panel that opens on the right, scroll down to find the **Request Headers** section.

8. Find the line that starts with `authorization:`. The value after the colon is your token.
   It looks like a long string such as:
   ```
   MTExMjM0NTY3ODkw.GhIjKl.AbCdEfGhIjKlMnOpQrStUvWxYz012345
   ```

9. Right-click the value and choose **Copy value**, or click at the start of the value and
   drag to select it all, then copy.

<!-- screenshot: Network tab showing the authorization header value -->

!!! tip "Token not appearing?"
    Try clicking on a different channel or server in Discord to generate a new network request,
    then look for the `authorization` header again. Make sure you are on the **Network** tab,
    not the **Console** or **Elements** tab.

---

## Step 3 — Find Your Server ID

1. Open Discord (browser or desktop app).
2. Go to **User Settings** (gear icon near the bottom-left) → **Advanced**.
3. Turn on **Developer Mode**.
4. Close settings.
5. Right-click your server's icon in the left sidebar.
6. Choose **Copy Server ID**.

The server ID is a long number, such as `987654321012345678`.

<!-- screenshot: Right-click menu on a Discord server icon showing "Copy Server ID" -->

---

## Step 4 — Run the Export

!!! warning "You MUST include `--markdown false` and `--media` — read this before running anything"
    - **`--markdown false`**: Without this flag, DCE replaces raw mention data like `<@123456>`
      with plain text like `@Username`. Discord Ferry uses the raw IDs to remap mentions to Stoat
      users. If the IDs are gone, all mentions will appear as plain text in the migrated server.
    - **`--media`**: Without this flag, DCE saves only Discord CDN links instead of downloading
      the actual files. Discord CDN links expire within approximately 24 hours. Any attachment
      link that was not downloaded will be permanently broken in the migrated server.

Open a terminal (on Windows: Command Prompt or PowerShell; on macOS/Linux: Terminal). Navigate
to the folder where you unzipped DCE.

Run the following command, replacing the two placeholders:

```
DiscordChatExporter.Cli exportguild \
  --token YOUR_TOKEN_HERE \
  -g YOUR_SERVER_ID \
  --media \
  --reuse-media \
  --markdown false \
  --format Json \
  --include-threads All \
  --output ./export/
```

Replace:

| Placeholder | Replace with |
|-------------|-------------|
| `YOUR_TOKEN_HERE` | The token you copied in Step 2 |
| `YOUR_SERVER_ID` | The server ID you copied in Step 3 |

**On Windows**, replace the backslashes `\` at the end of each line with a caret `^`, or paste
the entire command on a single line:

```
DiscordChatExporter.Cli exportguild --token YOUR_TOKEN_HERE -g YOUR_SERVER_ID --media --reuse-media --markdown false --format Json --include-threads All --output ./export/
```

!!! info "What each flag does"
    - `exportguild` — exports every channel in the server
    - `--media` — downloads all attachments, images, and files locally
    - `--reuse-media` — skips re-downloading files already saved (useful if you resume a failed export)
    - `--markdown false` — preserves raw mention syntax needed for migration
    - `--format Json` — saves data as JSON, which Discord Ferry reads
    - `--include-threads All` — includes public and private thread history
    - `--output ./export/` — saves everything into a folder called `export` in the current directory

The export will print progress to the terminal. Let it run until it prints a completion message.

<!-- screenshot: Terminal showing DCE export progress output -->

---

## Step 5 — Verify the Export

When the export finishes, open the `export` folder. You should see:

1. **JSON files** for each channel, named like:
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
`--output` folder — the `--reuse-media` flag means already-downloaded files will not be
re-fetched.

### Can I export just some channels?

Yes. Use `exportchannel` instead of `exportguild` and provide specific channel IDs:

```
DiscordChatExporter.Cli exportchannel \
  --token YOUR_TOKEN_HERE \
  -c CHANNEL_ID_1 CHANNEL_ID_2 CHANNEL_ID_3 \
  --media \
  --reuse-media \
  --markdown false \
  --format Json \
  --output ./export/
```

To find a channel ID: enable Developer Mode in Discord settings (see Step 3), then right-click a
channel name and choose **Copy Channel ID**.

### What about direct messages (DMs)?

Discord Ferry migrates server content only — channels, messages, roles, and emoji. DMs are
account-specific and are not migrated. Stoat does not have an equivalent import feature for DMs.

### I get an error about rate limits. What do I do?

Discord limits how fast third-party tools can read its API. If you see an error mentioning
rate limits or HTTP 429:

1. Stop the export.
2. Wait 10–15 minutes.
3. Re-run the same command. The `--reuse-media` flag ensures already-downloaded content is not
   re-fetched.

For very large servers, consider exporting in smaller batches using `exportchannel` with a subset
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
