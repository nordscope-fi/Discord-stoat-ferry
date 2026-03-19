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
| **Solution** | Add `ferry.exe` to your antivirus exclusion list. If your organization's policy prevents this, use the Python source distribution instead: clone the [GitHub repository](https://github.com/psthubhorizon/Discord-stoat-ferry) and install with `uv pip install .`, then run with `ferry` directly. The source distribution is not affected by this issue. |

### macOS "app is damaged and can't be opened"

| | |
|---|---|
| **Symptom** | macOS refuses to open `ferry` with a message that the app is damaged or cannot be opened |
| **Cause** | macOS automatically marks files downloaded from the internet as untrusted. This is a built-in security check called Gatekeeper — it is not an actual problem with Ferry. |
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
3. Check the migration report in `ferry-output/` for a full list of errors and warnings.
4. Open an issue on the Discord Ferry GitHub repository and include the relevant section of your log output.

!!! warning "Before sharing logs"
    Review your log output before sharing it publicly. Logs may contain channel names, user display names, or message content from your server. Redact any sensitive information before posting.
