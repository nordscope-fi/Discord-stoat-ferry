# Troubleshooting

This page covers the most common problems encountered during migration, their causes, and how to fix them.

---

## Authentication and Permission Errors

### 401 Unauthorized

| | |
|---|---|
| **Symptom** | Ferry stops immediately with `401 Unauthorized` |
| **Cause** | The token you provided is wrong, expired, or was copied incorrectly |
| **Solution** | Get a fresh token. Open Stoat in your browser, make sure you are logged in, then follow the [step-by-step token guide](../getting-started/setup-stoat.md#2-get-your-stoat-user-token) to copy a new one. Tokens expire when you log out or change your password, so you may need to do this again if it has been a while. |

### 403 Forbidden on server create

| | |
|---|---|
| **Symptom** | Ferry reports `403 Forbidden` when attempting to create the server |
| **Cause** | You may be using a bot token instead of a regular user token. Bot accounts cannot create servers on Stoat. |
| **Solution** | Make sure you are using the token from a **regular Stoat account** — the same account you log in to when you chat. Ferry does not use bots. See the [token guide](../getting-started/setup-stoat.md#2-get-your-stoat-user-token) for how to find the right token. |

---

## Rate Limit Errors

### 429 Too Many Requests (slow down)

| | |
|---|---|
| **Symptom** | Ferry slows significantly or logs frequent `429 Too Many Requests` errors (this code means "slow down") |
| **Cause** | Messages are being sent faster than the Stoat server allows |
| **Solution** | Increase the rate limit delay. In the GUI, go back to the Setup screen and drag the rate limit slider to 2.0 or 3.0 seconds. On the CLI, add `--rate-limit 2.0`. If you are on a self-hosted instance, you can also relax the server-side rate limit settings. |

---

## Export and File Problems

!!! warning "Content missing from a migration you already ran?"
    If the migration finished cleanly and content is missing anyway, the cause may be a Ferry bug
    rather than anything in this page — five silent ones were fixed between v2.8.2 and v2.10.0.
    See [Was my earlier migration affected?](earlier-migrations.md).

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
| **Solution** | No action needed. These are faithfully representing messages that had no text in Discord. Forwarded messages are no longer among them — their content is migrated and marked `[forwarded]`. If your export was made with DiscordChatExporter older than 2.47 the forwarded content is not in the export at all; those are skipped with a warning in the migration report, and re-exporting recovers them. |

### Messages show [continued 1/3] markers

| | |
|---|---|
| **Symptom** | Some messages in Stoat are split across multiple sequential messages with `[continued 1/3]`, `[continued 2/3]`, `[continued 3/3]` markers |
| **Cause** | The original Discord message exceeded Stoat's 2000-character message limit. Ferry automatically splits long messages into sequential parts. This is normal behavior — no content is lost. |
| **Solution** | No action needed. The full original message content is preserved across all parts. If this affects readability, self-hosted admins can raise the `message_length` limit in `Revolt.overrides.toml` — see [Self-Hosted Stoat Tips](self-hosted-tips.md). |

---

## Channel and Emoji Limits

### Channel limit exceeded

| | |
|---|---|
| **Symptom** | Ferry stops or warns that the server has reached its channel limit |
| **Cause** | The combined count of channels and flattened threads exceeds the Stoat server's per-server channel limit (200 by default) |
| **Solution** | Choose one of these options: (1) Use `--skip-threads` (CLI) or the **Skip threads** checkbox (GUI) to omit thread content; (2) If you run a self-hosted instance, raise the `server_channels` limit in `Revolt.overrides.toml` — see [Self-Hosted Stoat Tips](self-hosted-tips.md). |

---

## Application Won't Launch

### Ferry.exe blocked by antivirus (Windows)

| | |
|---|---|
| **Symptom** | Windows Defender or another antivirus quarantines or blocks `ferry.exe` |
| **Cause** | Ferry is packaged as a single-file app using PyInstaller (a Python packaging tool). These self-extracting apps are frequently flagged as false positives by antivirus software because the extraction technique resembles some malware behavior. |
| **Solution** | Add `ferry.exe` to your antivirus exclusion list. If your organization's policy prevents this, use the Python source distribution instead: clone the [GitHub repository](https://github.com/nordscope-fi/Discord-stoat-ferry) and install with `uv pip install .`, then run with `ferry` directly. The source distribution is not affected by this issue. |

### macOS "Apple could not verify Ferry is free of malware"

| | |
|---|---|
| **Symptom** | Double-clicking `Ferry.app` shows a dialog reading *"Apple could not verify 'Ferry' is free of malware that may harm your Mac or compromise your privacy."* The only buttons are **Done** and **Move to Bin**. |
| **Cause** | Ferry is not notarized through Apple's paid developer program, so Gatekeeper blocks the first launch. It is not a fault in the app. |
| **Solution** | Click **Done** — never **Move to Bin**, which deletes Ferry. Then open **System Settings → Privacy & Security**, scroll to the **Security** section, and click **Open Anyway** next to *"Ferry" was blocked to protect your Mac*. Confirm with **Open Anyway** again and enter your password. See [Installation](../getting-started/install.md) for the illustrated walkthrough. |

!!! warning "The Open Anyway button is time-limited"
    It appears only after macOS has blocked Ferry, and it disappears again after about an hour. If it is not there, double-click `Ferry.app` again and return to **Privacy & Security**.

!!! failure "Right-clicking no longer works"
    Older guides tell you to right-click (or Control-click) the app and choose **Open**. Apple removed that shortcut in **macOS 15 Sequoia**. On macOS 15 and later, the System Settings route above is the only way through the dialog.

### macOS "app is damaged and can't be opened"

| | |
|---|---|
| **Symptom** | macOS refuses to open `Ferry.app` with a message that the app is damaged or cannot be opened |
| **Cause** | macOS automatically marks files downloaded from the internet as untrusted. This is a built-in security check called Gatekeeper — it is not an actual problem with Ferry. |
| **Solution** | Run the following command in Terminal, then try opening Ferry again: |

```bash
xattr -dr com.apple.quarantine /Applications/Ferry.app
```

The `-r` flag matters. Without it the quarantine flag is cleared only from the bundle folder, while the executable inside it stays blocked and the app still refuses to open. If you kept Ferry somewhere other than `/Applications`, substitute that path.

### Ferry window is blank or says "Your browser does not support ES modules"

| | |
|---|---|
| **Symptom** | The Ferry window opens but stays empty, or shows the message *"Your browser does not support ES modules."* |
| **Cause** | Ferry draws its window using a renderer supplied by the operating system. On Windows that is the **Microsoft Edge WebView2 Runtime**, which is present on Windows 11 but not guaranteed on Windows 10. When it is missing or broken, the window falls back to a legacy engine that cannot run Ferry's interface. |
| **Solution** | Install or repair the [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/). If it is already installed, run the installer again; it repairs a broken install. |

!!! tip "Use `localhost:8765` meanwhile"
    Ferry serves its interface at `http://localhost:8765` whether or not the window draws. Open
    that address in any browser and carry on.

### Ferry.exe ignores `--help` and every other command

| | |
|---|---|
| **Symptom** | Running `Ferry-windows-x86_64.exe --help` from PowerShell prints nothing and opens the GUI. |
| **Cause** | Versions before **2.12.0** ignored command-line arguments entirely. |
| **Solution** | Upgrade to 2.12.0 or later from the [releases page](https://github.com/nordscope-fi/Discord-stoat-ferry/releases). |

!!! info "One case still opens the GUI by design"
    The app opens the GUI instead of running the command when there is nowhere to write output.
    That happens when it is launched from a Windows service, a scheduled task, or by dropping a
    file onto the icon. Run it from a terminal, or redirect its output to a file, and the
    command runs normally.

    Because the app is windowed, your shell prints its next prompt before Ferry's output
    arrives. The output prints underneath that new prompt.

---

## The Window Stops Responding

### "Connection lost. Trying to reconnect..."

|  |  |
|---|---|
| **Symptom** | A grey box appears over the Ferry window reading **"Connection lost. Trying to reconnect..."**, and it never goes away |
| **Cause** | Ferry's window is a viewer for a small server that runs inside the app. This message means that server stopped, so the window has nothing left to talk to and will keep retrying forever. On versions before 2.8.4 this happened after force-quitting Ferry: the window survived the quit. |
| **Solution** | Close the Ferry window, then reopen Ferry. |

If the window will not close, or Ferry does not start again, quit the leftovers by hand:

1. Open **Activity Monitor** (press ⌘ Space, type "Activity Monitor", press Return).
2. Type `Ferry` in the search box.
3. Select each result and click the **⊗** button in the toolbar, then **Force Quit**.
4. Open Ferry again.

!!! tip "You will not lose a migration in progress"
    Ferry saves a checkpoint as it works. Start it again and choose **Resume** — it
    picks up from the last completed step rather than starting over. Messages already
    copied are not copied twice.

---

## Migration Locks

### Another migration is in progress

| | |
|---|---|
| **Symptom** | Ferry reports "Another migration is in progress" or "Migration lock detected" and refuses to start |
| **Cause** | A prior Ferry run set an advisory lock marker in the Stoat server description. This prevents two Ferry instances from running against the same server simultaneously. The lock expires after 24 hours, but may persist if the prior migration crashed before it could clean up. |
| **Solution** | If the prior migration is genuinely still running, wait for it to finish. If it crashed, add `--force-unlock` to your command to clear the stale lock and proceed. |

---

## DCE Verification Errors

### DCE binary hash mismatch

| | |
|---|---|
| **Symptom** | Ferry reports "DCE binary hash mismatch" or "SHA-256 verification failed" |
| **Cause** | The downloaded DiscordChatExporter binary does not match the expected SHA-256 checksum. This can happen if the download was corrupted, if the cached binary is from a different version, or if you are using a self-built DCE binary. |
| **Solution** | Delete the cached DCE binary (found in the Ferry data directory) and re-run Ferry — it will re-download a fresh copy. If you are using a self-built or custom DCE binary, pass `--skip-dce-verify` to bypass the checksum check. |

### DCE export is N days old

| | |
|---|---|
| **Symptom** | Ferry warns or errors with "DCE export is N days old" and refuses to continue |
| **Cause** | Your DiscordChatExporter export is more than 30 days old. Ferry's freshness check flags old exports because Discord CDN attachment URLs expire, which means many attachments may no longer be downloadable. |
| **Solution** | Re-export from DiscordChatExporter to get a fresh export. If your export includes all media locally (exported with `--media`) and you want to proceed anyway, add `--force` to override the freshness check. |

---

## Flag Conflicts

### --resume and --incremental are mutually exclusive

| | |
|---|---|
| **Symptom** | Ferry reports `--resume and --incremental are mutually exclusive` and exits immediately |
| **Cause** | Both flags were passed on the same command. They serve different purposes and cannot be combined. |
| **Solution** | Use `--resume` to continue a migration that was interrupted mid-run (the state file was written but migration did not finish). Use `--incremental` when a prior migration completed successfully and you want to migrate only new messages that have arrived since. |

---

## Circuit Breaker Pausing

### Circuit breaker open

| | |
|---|---|
| **Symptom** | Logs show "Circuit breaker open" and migration pauses for 30 seconds |
| **Cause** | The Stoat API has failed 5 times in a row. Ferry's circuit breaker activates to avoid hammering a struggling server. |
| **Solution** | Ferry will automatically retry after 30 seconds with exponential backoff. If this keeps happening, check that your Stoat instance is running and reachable. On self-hosted instances, check the Stoat server logs for errors. |

---

## CDN and Attachment Issues

### Expired CDN URLs

| | |
|---|---|
| **Symptom** | Ferry warns "X attachment URLs have expired" during validation |
| **Cause** | Your DCE export is more than 24 hours old and was created without the `--media` flag. Discord CDN links expire, so the URLs in the export no longer work. |
| **Solution** | Re-export from DiscordChatExporter with the `--media` flag. This downloads all files locally so they do not depend on Discord's CDN. |

### Attachment overflow

| | |
|---|---|
| **Symptom** | Messages in Stoat show `[+N more attachments not migrated]` at the end |
| **Cause** | The original Discord message had more than 5 attachments. Stoat allows a maximum of 5 attachments per message — this is a platform limit, not a Ferry bug. |
| **Solution** | No action needed. The first 5 attachments are migrated. The overflow note tells you how many were left out. |

---

## Permission and Role Issues

### Per-member overrides skipped

| | |
|---|---|
| **Symptom** | Ferry warns "per-member overrides skipped" during structure creation |
| **Cause** | Discord allows channel-level permission overrides for individual users. Stoat only supports per-role overrides, so user-specific permissions cannot be migrated directly. |
| **Solution** | As a workaround, create single-user roles on your Stoat server for any users who need individual channel permissions, then assign those roles manually after migration. |

---

## Avatar Issues

### Avatar pre-flight shows 0 uploads

| | |
|---|---|
| **Symptom** | Avatar pre-flight reports "0 of N avatars uploaded" |
| **Cause** | Your DCE export does not include local avatar files. This happens when the export was created without the `--media` flag, so avatar URLs point to Discord's CDN instead of local files. |
| **Solution** | Re-export from DiscordChatExporter with the `--media` flag. Ferry will then upload the locally downloaded avatar files. |

---

## Post-Migration Validation

### Validation count mismatches

| | |
|---|---|
| **Symptom** | Post-migration validation warns about count differences between source and Stoat (e.g., "expected 25 channels, found 23") |
| **Cause** | Some channels or roles were not created during migration, likely due to errors during the structure creation phase. |
| **Solution** | Check the migration report (`migration_report.md` in your output directory) for specific errors. You can re-run the migration with `--resume` to retry failed items, or create the missing channels/roles manually on Stoat. |

---

## Getting More Help

If your issue is not listed here:

1. Run `ferry validate` on your export and check the warnings output — it often points directly to the problem.
2. Run the migration with `--verbose` (CLI) to get per-message detail in the log.
3. Run `ferry stats <output-dir>` after a failed or partial migration to see the aggregate picture: counts, fidelity score, and a truncated preview of the most recent error — often faster than scanning `state.json` by hand. See the [CLI reference](cli-reference.md#ferry-stats).
4. Check the migration report in `ferry-output/` for a full list of errors and warnings.
5. Open an issue on the Discord Ferry GitHub repository and include the relevant section of your log output.

!!! warning "Before sharing logs"
    Review your log output before sharing it publicly. Logs may contain channel names, user display names, or message content from your server. Redact any sensitive information before posting.
