# Troubleshooting

This page covers the most common problems encountered during migration, their causes, and how to fix them.

---

## Authentication and Permission Errors

### 401 Unauthorized

| | |
|---|---|
| **Symptom** | Ferry stops immediately with `401 Unauthorized` |
| **Cause** | The token you provided is wrong, expired, or was copied incorrectly |
| **Solution** | Get a fresh token from your Stoat web client: open your browser developer tools (F12), go to **Application > Local Storage**, find your Stoat domain, and copy the `token` value. Tokens can expire if you log out or change your password. |

### 403 Forbidden on server create

| | |
|---|---|
| **Symptom** | Ferry reports `403 Forbidden` when attempting to create the server |
| **Cause** | You are using a bot token instead of a user token. Bot accounts cannot create servers. |
| **Solution** | Use your personal user token. See the [401 Unauthorized](#401-unauthorized) instructions above for where to find it. |

---

## Rate Limit Errors

### 429 Too Many Requests

| | |
|---|---|
| **Symptom** | Ferry slows significantly or logs frequent `429 Too Many Requests` errors |
| **Cause** | Messages are being sent faster than the Stoat server allows |
| **Solution** | Increase the rate limit delay. In the GUI, go back to the Setup screen and drag the rate limit slider to 2.0 or 3.0 seconds. On the CLI, add `--rate-limit 2.0`. If you are on a self-hosted instance, you can also relax the server-side rate limit settings. |

---

## Export and File Problems

### Attachment file missing

| | |
|---|---|
| **Symptom** | Ferry logs warnings about missing attachment files; some messages arrive without their attached images or files |
| **Cause** | DiscordChatExporter did not download the media files. This happens when the export was created without the `--media` flag, or when Discord CDN links had already expired before export. |
| **Solution** | Re-export from DiscordChatExporter with the `--media` flag. Discord CDN links expire within approximately 24 hours of the original export, so export and migrate promptly. |

### No valid DCE JSON files found

| | |
|---|---|
| **Symptom** | Ferry reports "No valid DCE JSON files found" and cannot start |
| **Cause** | The export folder path is wrong, or the files are in the wrong format |
| **Solution** | Confirm you are pointing Ferry at the folder that *contains* the `.json` files, not a parent folder. Also confirm you exported from DiscordChatExporter using `--format Json` (not HTML or CSV). |

### Rendered markdown detected

| | |
|---|---|
| **Symptom** | Ferry warns "Rendered markdown detected"; user mentions appear as `@Username` instead of raw mention IDs in Stoat messages |
| **Cause** | The export was created without `--markdown false`. DiscordChatExporter rendered `<@123456789>` into `@Username`, destroying the data needed to reconstruct mentions. |
| **Solution** | Re-export from DiscordChatExporter using the `--markdown false` flag. |

---

## Content Appearance

### Messages showing as [empty message]

| | |
|---|---|
| **Symptom** | Some messages in Stoat appear with the placeholder `[empty message]` |
| **Cause** | The original Discord message had no text content — for example, a message that was only a sticker, a forwarded message, or a system event with no body. This is normal behavior. |
| **Solution** | No action needed. These are faithfully representing messages that had no text in Discord. Forwarded messages are logged separately as "forwarded message skipped" in the migration report. |

---

## Channel and Emoji Limits

### Channel limit exceeded

| | |
|---|---|
| **Symptom** | Ferry stops or warns that the server has reached its channel limit |
| **Cause** | The combined count of channels and flattened threads exceeds the Stoat server's per-server channel limit (200 by default) |
| **Solution** | Choose one of these options: (1) Add `--skip-threads` (CLI) to omit thread content; (2) If you run a self-hosted instance, raise the `server_channels` limit in `Revolt.overrides.toml` — see [Self-Hosted Stoat Tips](self-hosted-tips.md). |

---

## Application Won't Launch

### Ferry.exe blocked by antivirus (Windows)

| | |
|---|---|
| **Symptom** | Windows Defender or another antivirus quarantines or blocks `ferry.exe` |
| **Cause** | Ferry is distributed as a PyInstaller single-file executable. PyInstaller bundles are frequently flagged as false positives by antivirus software because they self-extract to a temporary folder, which is a common malware technique. |
| **Solution** | Add `ferry.exe` to your antivirus exclusion list. If your organization's policy prevents this, use the Python source distribution instead: install via `pip install discord-ferry` and run with `ferry` directly. The source distribution is not affected by this issue. |

### macOS "app is damaged and can't be opened"

| | |
|---|---|
| **Symptom** | macOS refuses to open `ferry` with a message that the app is damaged or cannot be opened |
| **Cause** | macOS applies a quarantine attribute to files downloaded from the internet. This is a Gatekeeper security feature, not an actual problem with the binary. |
| **Solution** | Run the following command in Terminal, then try opening Ferry again: |

```bash
xattr -d com.apple.quarantine /path/to/ferry
```

Replace `/path/to/ferry` with the actual path to the downloaded binary. If you moved it to `/Applications`, the command would be:

```bash
xattr -d com.apple.quarantine /Applications/ferry
```

!!! info "Right-click workaround"
    Alternatively, right-click (or Control-click) the `ferry` file, choose **Open**, and click **Open** in the dialog that appears. macOS will remember this choice and not prompt again.

---

## Getting More Help

If your issue is not listed here:

1. Run `ferry validate` on your export and check the warnings output — it often points directly to the problem.
2. Run the migration with `--verbose` (CLI) to get per-message detail in the log.
3. Check the migration report in `ferry-output/` for a full list of errors and warnings.
4. Open an issue on the Discord Ferry GitHub repository and include the relevant section of your log output.

!!! warning "Before sharing logs"
    Review your log output before sharing it publicly. Logs may contain channel names, user display names, or message content from your server. Redact any sensitive information before posting.
