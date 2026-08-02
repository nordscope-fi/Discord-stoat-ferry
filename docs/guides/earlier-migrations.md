# Was my earlier migration affected?

Between v2.8.2 and v2.10.0, Ferry fixed five bugs that had one thing in common: **they failed
silently.** No error, no red text, nothing in the failed-message list. A migration could report
complete success and still be missing content.

If you migrated with an earlier version, this page tells you what to look for and what your options
are. It does not sugar-coat them: **for the content that was lost, there is currently no way to
repair a migrated server in place.** Permissions are the one thing you can fix where it stands.

!!! info "Nothing here requires a re-export"
    Everything below is a Ferry bug, not an export bug. Your existing
    DiscordChatExporter output is fine and still contains the missing data — with one exception,
    noted under [Forwarded messages](#1-forwarded-messages-were-discarded).

## Quick check

| If you migrated with | Then look for |
|----------------------|---------------|
| anything before **2.9.0** | Missing forwarded messages, and missing replies whose whole content was a sticker or an embed |
| anything before **2.8.5** | Missing attachments just over a round size — e.g. a 20.4 MB file |
| anything before **2.8.3** | Missing attachments, clustered in channels with lots of media |
| anything before **2.10.0** | Voice channels nobody can use, and roles missing permissions they had in Discord |
| anything before **2.8.2** | Nothing missing — migrations were just slower than they needed to be |

Your migration report is the fastest way to check the first three. It is written to your output
directory as `migration_report.json` (and `migration_report.md`).

!!! tip "No terminal? Open the Markdown report instead"
    Every search below has an equivalent in `migration_report.md`, which lists each warning on its
    own line with the type in square brackets:

    ```
    - [forwarded_message] Forwarded message 1506019526855360593 skipped (DCE limitation).
    - [attachment_upload_failed] Attachment 'clip.mp4' upload failed: File too large
    ```

    Open it in any text editor and search for the name in brackets.

---

## 1. Forwarded messages were discarded

**Fixed in v2.9.0.** Affects every earlier version.

Ferry skipped every forwarded message and recorded it as a DiscordChatExporter limitation. That was
true when the code was written and stopped being true in **February 2026**, when DCE 2.47 began
exporting the full forwarded payload. Ferry has pinned DCE 2.47.1 since **v2.0.2 (21 April 2026)**,
so if Ferry produced your export, the data was there and was thrown away.

**How to check.** Search your report for `forwarded_message`:

```bash
grep -c '"type": "forwarded_message"' migration_report.json
```

Every hit is one message that was dropped.

**The same check covers a second bug.** The detector keyed on *empty content*, which is not unique
to a forward — a reply whose entire payload was a sticker, or an embed, matched it exactly and was
discarded too. Those also appear under `forwarded_message`, so the count above includes them.

!!! warning "The one case that needs a re-export"
    If you exported with DiscordChatExporter **older than 2.47** — for example by pointing Ferry at
    an export you made yourself, some time ago — then the forwarded content was never written to
    disk. Re-exporting is the only way to recover it.

## 2. Some attachments were rejected by the server

**Fixed in v2.8.5.** Affects every earlier version.

Ferry checks a file's size before uploading it, to avoid wasting a round trip. Five of the six size
limits were written as **binary** megabytes (`20 × 1024 × 1024`) where Stoat's media server uses
**decimal** ones (`20,000,000`). Files landing in the gap passed Ferry's check, were uploaded, and
were then rejected by the server.

| Upload type | Ferry allowed | Server accepts | Files in this range were lost |
|---|---|---|---|
| Attachments | 20,971,520 | 20,000,000 | 20.00 – 20.97 MB |
| Avatars | 4,194,304 | 4,000,000 | 4.00 – 4.19 MB |
| Backgrounds | 6,291,456 | 6,000,000 | 6.00 – 6.29 MB |
| Banners | 6,291,456 | 6,000,000 | 6.00 – 6.29 MB |
| Emoji | 512,000 | 500,000 | 500 – 512 KB |
| Icons | 2,500,000 | 2,500,000 | — already correct |

**How to check.** Search your report for `attachment_upload_failed`:

```bash
grep '"type": "attachment_upload_failed"' migration_report.json
```

!!! danger "These leave no trace in the migrated message"
    When Ferry's own size check catches a file, it leaves a visible marker in the message, like
    `[File too large: clip.mp4 (24.1 MB, limit: 20.0 MB)]`. When the **server** rejects an upload,
    it does not — the message arrives with the file simply absent, and the only record is the
    warning in your report. So a visual skim of the migrated server will not find these.

## 3. Uploads could fail during rate limiting

**Fixed in v2.8.3.** Affects every earlier version.

Stoat tells a client how long to wait after a rate limit, in milliseconds. Ferry's media uploader
never read that header and fell back to waiting one second — roughly nine seconds too short. All
three retry attempts could therefore be spent inside a single closed window, and the upload would
give up.

This is most likely to have bitten large, media-heavy channels, which is exactly where it is hardest
to notice by eye.

**How to check.** The same search as above — a failed upload is recorded as
`attachment_upload_failed` whatever the cause. The message text will mention the status code.

## 4. Voice channels and role permissions

**Fixed in v2.10.0.** Affects every earlier version.

None of Discord's voice permissions had a Stoat equivalent recorded, so they all translated to
nothing. **Migrated voice channels granted nobody the right to connect, speak or hear.** Roles that
held ADMINISTRATOR in Discord were affected more broadly — they arrived without voice, mention,
timeout, audit-log or role-assignment rights.

**How to check.** There is no warning to search for; this one is silent by construction. Look at the
server instead:

- Ask a member who is not the server owner to join a migrated voice channel and speak.
- Compare a migrated moderator role's permissions against the same role in Discord.

The server owner will not reproduce it — Stoat grants owners everything regardless.

## 5. Migrations were slower than they needed to be

**Fixed in v2.8.2.** Affects every earlier version. **No content was lost.**

Ferry read Stoat's rate-limit header as seconds when it is milliseconds, so every rate-limit pause
waited the full one-minute cap instead of the ten seconds actually required. Listed here only so the
record is complete.

---

## What you can do

Be aware of what does **not** work, because both look like they should:

- **`--resume` will not backfill any of this.** It continues an interrupted run from where it
  stopped; it does not revisit messages it has already passed.
- **`--incremental` will not backfill it either.** It skips everything below the high-water mark it
  recorded for each channel, and all of the content above sits below that mark.
- **The retry machinery cannot find it either.** These were recorded as *warnings*, not as failed
  messages, so nothing ever entered the retry queue. (There is also no `ferry retry` command today —
  the retry code exists inside the engine but has no command-line surface yet. Exposing it is part of
  the same planned work as the repair tool.)

That leaves:

| What was affected | Your options today |
|---|---|
| Forwarded messages, sticker- or embed-only replies | Migrate the same export again with v2.9.0 or later, into a **new, empty Stoat server**. This produces a complete second copy — it does not merge into the existing one. |
| Missing attachments | The same. The message text is already in your server; only the file is missing, and there is no supported way to attach it after the fact. |
| Voice and role permissions | Fixable in place, by hand — no content is involved. Grant the permissions on the affected roles in Stoat, or re-run the ROLES and CHANNELS phases into a fresh server. |

## What we are building

A repair tool is planned. It will re-verify an already-migrated server against its original export
and fix what it safely can — including permissions and messages that were dropped rather than
failed. It is tracked as part of the ongoing upstream-drift work in
[issue #107](https://github.com/nordscope-fi/Discord-stoat-ferry/issues/107).

It will not be able to do everything. Ferry only keeps a record of what it sent for some
combinations of settings, and where that record is absent the tool will decline to touch messages
rather than risk duplicating a server's entire history. Permissions repair is the part that will
work everywhere.

If you think you were affected and the checks above do not settle it, please
[open an issue](https://github.com/nordscope-fi/Discord-stoat-ferry/issues/new) with your
`migration_report.json` attached — with any tokens removed.
