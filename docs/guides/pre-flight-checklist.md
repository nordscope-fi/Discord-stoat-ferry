# Pre-Flight Checklist

Run through this checklist before starting a migration. Each step prevents a specific class of problem that is difficult or impossible to fix after the migration has begun.

---

## 1. Put the Discord server in read-only mode

Set every channel's permissions so that regular members cannot send new messages. This freezes the server content at a known point in time.

**Why:** Messages posted after the export but before migration completes will not be included. If the server stays active during export, you will have an incomplete archive with no clean cutoff point.

---

## 2. Run DiscordChatExporter with the correct flags

Export using **both** of these flags:

```bash
DiscordChatExporter.Cli exportguild --token YOUR_DISCORD_TOKEN \
  --guild YOUR_SERVER_ID --format Json --markdown false --media
```

**Why:**

- `--markdown false` preserves raw mention syntax (`<@123456789>`) so Ferry can remap mentions to Stoat users. Without it, mentions become plain text like `@Username` and cannot be reconstructed.
- `--media` downloads all attachments locally. Discord CDN links expire within approximately 24 hours. Without this flag, images and files will be missing from the migration.

---

## 3. Verify Stoat file size limits

Check that your Stoat instance can accept the files in your export:

| File Type | Default Limit |
|-----------|--------------|
| Attachments | 20 MB |
| Avatars | 4 MB |
| Server icons | 2.5 MB |
| Banners | 6 MB |
| Emoji | 500 KB |

**Why:** Files exceeding these limits will fail to upload silently. Self-hosted admins can raise limits in `Revolt.overrides.toml` — see [Self-Hosted Tips](self-hosted-tips.md). On the official hosted service, these limits are fixed.

---

## 4. Grant the required permissions

The Stoat account running Ferry needs Masquerade and ManageRole permissions at minimum, plus several others for full functionality. The minimum permission value covering all required bits is **`1,022,361,624`**.

**Why:** Without Masquerade, messages cannot display the original Discord author's name and avatar. Without ManageRole, masquerade colours will not work. See the full permission table in [Self-Hosted Tips](self-hosted-tips.md#permissions).

!!! tip "Use the server owner's account"
    The simplest approach is to run Ferry with the Stoat server owner's token. The owner has all permissions automatically.

---

## 5. Estimate migration duration

Ferry sends approximately one message per second to stay within Stoat's rate limits. Use this to plan your time:

| Messages | Approximate Duration |
|----------|---------------------|
| 1,000 | ~17 minutes |
| 10,000 | ~3 hours |
| 50,000 | ~14 hours |
| 100,000 | ~28 hours |

**Why:** Large migrations take hours or days. Knowing the duration upfront lets you plan around maintenance windows and avoid interrupting a migration partway through. Ferry supports resume, but an uninterrupted run is always smoother.

---

## 6. Check the channel count

Stoat's default limit is **200 channels per server**. Every Discord thread and forum post becomes a separate Stoat channel after flattening.

Run `ferry validate` on your export to see the projected channel count before starting.

**Why:** If the total exceeds 200, the migration will fail partway through. Use `--min-thread-messages` to filter low-activity threads, or `--skip-threads` to omit threads entirely. Self-hosted admins can raise the limit — see [Self-Hosted Tips](self-hosted-tips.md#raising-limits-for-migration).

---

## 7. Identify private channels

If your Discord server has private channels (channels visible only to certain roles), you need to provide a Discord token so Ferry can fetch permission metadata.

```bash
ferry migrate --discord-token YOUR_DISCORD_TOKEN ...
```

**Why:** Without the Discord token, Ferry has no way to know which channels were private. All channels will be created as public on Stoat, potentially exposing content that was restricted on Discord.

---

## 8. Plan for per-member permission overrides

Discord allows setting permissions on individual members within a channel. Stoat only supports role-based overrides.

**Why:** If you have channels where specific users had unique permissions (e.g., a single user banned from a channel), those per-member overrides will be lost. The workaround is to create a single-user role for each affected member before migration and apply the override to that role instead.

---

## 9. Back up Stoat MongoDB (self-hosted)

Before starting the migration, create a database snapshot:

```bash
mongodump --uri="mongodb://localhost:27017" --db=revolt --out=/path/to/backup
```

**Why:** If the migration produces unexpected results, you can restore the database to its pre-migration state with `mongorestore`. Without a backup, the only option is to delete the server and start over.

!!! info "Official hosted service"
    This step only applies to self-hosted instances. On the official service, you cannot access the database directly. If something goes wrong, delete the Stoat server and re-run the migration.

---

## 10. Check Stoat account age (official hosted service)

Ensure the Stoat account you are using was created at least **72 hours ago**.

**Why:** Stoat may apply stricter rate limits to accounts less than 72 hours old. A new account could hit unexpected throttling during migration, slowing the process significantly or causing failures.

---

## 11. Review v2.0.0 flags before running

Ferry 2.0.0 adds several new flags. Decide which apply to your migration before you start:

| Flag | Purpose | When to use |
|------|---------|-------------|
| `--thread-strategy flatten\|merge\|archive` | Controls how Discord threads are represented in Stoat | Default is `flatten` (each thread becomes a channel). Use `merge` to append thread messages to the parent channel, or `archive` to create read-only channels |
| `--incremental` | Migrate only messages posted since the last run | Subsequent runs on a live-but-slowing server; requires a previous completed migration |
| `--verify-uploads` | After each file upload, fetch it back and compare size | Use when uploads may be silently corrupted (slow connections, large files) |
| `--cleanup-orphans` | Detect Autumn files that were uploaded but never referenced in a message | Run after migration to reclaim storage on self-hosted instances |
| `--force` | Override the DCE freshness check (exports older than 30 days) | Only use if re-running with a known-good old export |
| `--force-unlock` | Remove the advisory migration lock from the server description | Use if a previous migration crashed and left the lock in place |
| `--skip-dce-verify` | Skip SHA-256 verification of the DCE binary | Not recommended; use only in air-gapped environments where the hash is already verified |

**Why:** Choosing the wrong thread strategy or omitting `--incremental` on a second run will duplicate messages or create an unexpected channel structure. Decide before starting — these options cannot be changed mid-migration.

---

## Quick Reference

Copy this condensed checklist for quick use:

- [ ] Discord server set to read-only
- [ ] DCE export completed with `--markdown false --media`
- [ ] Stoat file size limits verified
- [ ] Ferry account has required permissions (`1,022,361,624`)
- [ ] Migration duration estimated and time blocked
- [ ] Channel count within Stoat limits (or limits raised)
- [ ] Private channels identified; Discord token ready if needed
- [ ] Per-member overrides planned (single-user roles created)
- [ ] MongoDB backed up (self-hosted only)
- [ ] Stoat account is older than 72 hours (official service only)
- [ ] v2.0.0 flags reviewed (`--thread-strategy`, `--incremental`, `--verify-uploads`, `--cleanup-orphans`, `--force`, `--force-unlock`, `--skip-dce-verify`)

---

## See Also

- [Exporting from Discord](../getting-started/export-discord.md) — detailed export instructions
- [Known Limitations](known-limitations.md) — what changes or gets lost during migration
- [Self-Hosted Tips](self-hosted-tips.md) — raising limits and configuring your Stoat instance
- [Troubleshooting](troubleshooting.md) — solutions for common migration errors
