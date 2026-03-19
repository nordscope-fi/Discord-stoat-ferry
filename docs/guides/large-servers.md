# Migrating Large Servers

This guide covers what to expect when migrating a server with hundreds of channels or 100,000+ messages, and how to keep things running smoothly.

---

## Time Estimates

Ferry sends one message per second by default (the 1.0s rate limit). v2.0.0 adds parallel channel processing, which significantly reduces wall-clock time for servers with many channels. Use these rough figures to plan:

| Message count | Sequential (v1) at 1.0s | Parallel (v2, default 3 channels) |
|---------------|-------------------------|-----------------------------------|
| 1,000 | ~17 min | ~6 min |
| 10,000 | ~3 hours | ~1 hour |
| 50,000 | ~14 hours | ~5 hours |
| 100,000 | ~28 hours | ~8–10 hours |
| 500,000 | ~6 days | ~2 days |

!!! note "Parallel estimates assume typical channel distribution"
    Actual speedup depends on how evenly messages are distributed across channels. Servers where one channel holds 90% of messages will see less benefit from parallelism.

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
    The state file is saved as `state.json` in your output directory (default: `./ferry-output/`). Do not delete it until you are satisfied the migration is complete.

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

## Parallel Channel Sends

v2.0.0 processes multiple channels concurrently, dramatically reducing migration time for large servers.

| Setting | Default | Description |
|---------|---------|-------------|
| `--max-concurrent-channels` | 3 | Channels processed simultaneously |
| `--max-concurrent-requests` | 5 | Total concurrent API calls across all workers |

These settings interact: with 3 channels and 5 API slots, each channel averages ~1.7 concurrent API calls. For self-hosted instances with generous rate limits, increase both.

!!! tip "Increasing concurrency on self-hosted instances"
    On a self-hosted instance with relaxed rate limits, try `--max-concurrent-channels 6 --max-concurrent-requests 12`. Monitor your Stoat server load and back off if you see errors.

---

## Incremental Migration

For active servers where new messages arrive between migration runs:

```bash
ferry migrate --incremental --stoat-url ... --token ...
```

Incremental mode:

- Loads the prior completed state (`state.json` + `message_map.json`) from the output directory
- Only migrates messages newer than the last completed run per channel
- New channels since the last run are fully migrated
- Cumulative stats shown in the report alongside delta stats

**Requirements:** A prior completed migration in the same output directory.

**Cannot be combined with `--resume`** — these are mutually exclusive. Use `--resume` for crashed migrations; use `--incremental` for delta updates of a successfully completed migration.

!!! info "message_map.json"
    As of v2.0.0, the message ID map is stored as a separate `message_map.json` file alongside `state.json`. Both files must be present in the output directory for `--incremental` or `--resume` to work correctly. Do not delete either file until you are satisfied the migration is complete.

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

- **Skip threads** — use `--skip-threads` (CLI) or the **Skip threads** checkbox (GUI) to omit all thread and forum content. This keeps you within the channel limit but loses threaded conversations.
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
    The CLI shows a live Rich dashboard with a phase progress bar, a per-channel message progress bar with ETA, and running stats (messages sent, errors, warnings, current channel). Add `--verbose` for a line per message — useful for debugging but very noisy on large servers. For truly unattended runs, redirect output to a log file:

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

---

## Thread Filtering

For servers with hundreds of threads, many may contain only a few messages. Use `min_thread_messages` (default 0) to exclude low-activity threads and stay within Stoat's 200-channel limit:

=== "GUI"
    On the Setup screen, expand **Advanced Options** and set the **Min thread messages** field. Threads with fewer messages than this threshold are skipped.

=== "CLI"
    ```bash
    ferry migrate ~/exports/my-discord-server/ \
      --stoat-url https://api.stoat.chat \
      --token your_token_here \
      --min-thread-messages 5
    ```

!!! tip "Find the right threshold"
    Run `ferry validate` first — the counts table shows thread message counts. Setting `min_thread_messages=5` is a good starting point, skipping threads that are essentially empty.

---

## Reaction Mode

By default, Ferry uses `reaction_mode='text'`, which appends reaction counts to the end of message content instead of making individual API calls per reaction. This is dramatically faster for large servers.

| Mode | Behavior | Speed |
|------|----------|-------|
| `text` (default) | Reactions shown as text at end of message (e.g., "Reactions: :thumbsup: 3, :heart: 1") | Fastest — no extra API calls |
| `native` | Reactions added via Stoat API (without per-user attribution) | Slow — one API call per unique reaction per message |
| `skip` | Reactions not migrated at all | Fastest — no processing |

For a 10,000-message server with 20,000 reactions, `text` mode saves roughly 5 hours compared to `native` mode.

=== "CLI"
    ```bash
    ferry migrate ~/exports/my-discord-server/ \
      --stoat-url https://api.stoat.chat \
      --token your_token_here \
      --reaction-mode text
    ```

---

## Checkpoint Tuning

Ferry saves migration state every 50 messages by default (`checkpoint_interval=50`). For very large servers (100,000+ messages), you can increase this to reduce disk I/O overhead:

=== "CLI"
    ```bash
    ferry migrate ~/exports/my-discord-server/ \
      --stoat-url https://api.stoat.chat \
      --token your_token_here \
      --checkpoint-interval 200
    ```

!!! info "Trade-off"
    A higher checkpoint interval means faster throughput but slightly more re-work if the migration is interrupted — Ferry will replay up to `checkpoint_interval` messages on resume. For most large migrations, 200 is a good balance.
