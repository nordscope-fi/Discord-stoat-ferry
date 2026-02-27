# Migrating Large Servers

This guide covers what to expect when migrating a server with hundreds of channels or 100,000+ messages, and how to keep things running smoothly.

---

## Time Estimates

Ferry sends one message per second by default (the 1.0s rate limit). Use these rough figures to plan:

| Message count | Estimated time at 1.0s | Estimated time at 0.5s |
|---------------|------------------------|------------------------|
| 10,000 | ~3 hours | ~1.5 hours |
| 50,000 | ~14 hours | ~7 hours |
| 100,000 | ~28 hours | ~14 hours |
| 500,000 | ~6 days | ~3 days |

!!! warning "Run overnight or over a weekend"
    Large migrations are not something you watch in real time. Start the migration before you go to sleep or before the weekend. Use the CLI for unattended runs — it keeps running even if you close your terminal (use `nohup` or `screen`/`tmux`).

---

## Resume Support

Ferry saves its progress after finishing each channel. If the migration is interrupted — by a crash, a network error, or a deliberate Ctrl+C — you can pick up where it left off.

=== "GUI"
    On the Setup screen, expand **Advanced Options** and enter the same export folder path. Ferry will detect the existing state file and offer to resume.

=== "CLI"
    ```bash
    ferry migrate ~/exports/my-discord-server/ \
      --stoat-url https://api.stoat.chat \
      --token your_token_here \
      --resume
    ```

!!! info "State file location"
    The state file is saved as `ferry-state.json` in your output directory (default: `./ferry-output/`). Do not delete it until you are satisfied the migration is complete.

---

## Rate Limit Tuning

The default 1.0s inter-message delay is conservative and suitable for the official hosted Stoat service. You can adjust it:

| Delay | Effect |
|-------|--------|
| 1.0s (default) | Safe for official hosted service |
| 0.5s | Twice as fast; acceptable on self-hosted instances with relaxed limits |
| 2.0–3.0s | Use if you are seeing frequent 429 errors |

!!! warning "Do not go below 0.5s on the official service"
    The official Stoat service enforces 10 messages per 10 seconds. Going below 0.5s per message will reliably trigger rate limit errors and slow your migration down overall due to backoff delays.

---

## Self-Hosted Advantage

If you are migrating to a self-hosted Stoat instance, you can raise the server-side limits to remove artificial bottlenecks. See [Self-Hosted Stoat Tips](self-hosted-tips.md) for the full configuration table.

---

## Disk Space

DCE exports with media can be very large. Before migrating, confirm you have enough free space:

- A server with 100k messages and active image sharing can produce 10–50 GB of media files.
- Ferry does not delete the export after migration. You can remove it once you are satisfied everything transferred correctly.
- The `ferry-output/` folder with reports and state files is small (a few MB at most).

---

## Channel Limit

Stoat allows a maximum of 200 channels per server by default. Discord servers with many threads and forum posts can easily exceed this when threads are flattened into text channels.

**Options:**

- **Skip threads** — use `--skip-threads` (CLI) to omit all thread and forum content. This keeps you within the channel limit but loses threaded conversations.
- **Raise the limit** — on a self-hosted instance, increase `server_channels` in your configuration. See [Self-Hosted Stoat Tips](self-hosted-tips.md). If your self-hosted instance has a raised limit, pass `--max-channels N` to Ferry so it respects the higher ceiling.

!!! tip "Check before you start"
    Run `ferry validate` on your export first. The counts table will show the total channel and thread count so you can decide before migration begins.

---

## Emoji Limit

Stoat allows a maximum of 100 custom emoji per server by default. Ferry migrates the first 100 and logs a warning for any beyond that.

If emoji fidelity matters, raise the `server_emoji` limit on a self-hosted instance. On the official hosted service, the first 100 emoji will be migrated and the rest skipped. If your self-hosted instance has a raised limit, pass `--max-emoji N` to Ferry so it respects the higher ceiling.

---

## Monitoring Progress

=== "GUI"
    The Migrate screen shows a live phase indicator, per-channel progress bar, running totals, and a scrolling log. Leave the browser tab open and check back periodically.

=== "CLI"
    The default CLI output shows one line per channel. Add `--verbose` for a line per message — useful for debugging but very noisy on large servers. For truly unattended runs, redirect output to a log file:

    ```bash
    ferry migrate ~/exports/my-discord-server/ \
      --stoat-url https://api.stoat.chat \
      --token your_token_here \
      > ferry.log 2>&1 &
    ```

    Then tail the log to check in:

    ```bash
    tail -f ferry.log
    ```
