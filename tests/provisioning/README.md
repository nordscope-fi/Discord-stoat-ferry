# Discord Test Server Provisioning

This operator-only command prepares and checks the dedicated Discord guild used for reviewed
DiscordChatExporter (DCE) fixtures. Its network commands never run in continuous integration.

## Prerequisites

1. Use a Discord bot installed in the fixture guild with these permissions:
   `MANAGE_CHANNELS`, `SEND_MESSAGES`, `MANAGE_THREADS`, `EMBED_LINKS`, and
   `READ_MESSAGE_HISTORY`.
2. Store the bot token in Proton Pass. Put only its `pass://` reference in an operator-owned dotenv
   file:

   ```dotenv
   DISCORD_TEST_BOT_TOKEN=pass://<share-id>/<item-id>/<field>
   ```

3. Set the reference-file and guild variables. Do not export the token itself:

   ```bash
   export FERRY_CAPTURE_ENV=/path/to/discord-fixture-references.env
   export FERRY_FIXTURE_GUILD_ID=1505988963628879902
   ```

4. Install the locked development dependencies with `uv sync --locked --extra dev --extra native`.

The bot application must expose Discord's limited message-content intent during DCE capture. For a
bot in fewer than 100 guilds, this is application flag `524288`. Record the original flags and
restore them after capture. The reviewed 2.48 capture recorded `0`, enabled `524288`, then restored
`0`.

## Manage the fixture guild

Plan a restoration before making changes:

```bash
pass-cli run --env-file "$FERRY_CAPTURE_ENV" -- \
  uv run python -m tests.provisioning.provision_test_server provision \
  --guild-id "$FERRY_FIXTURE_GUILD_ID" --dry-run
```

Remove `--dry-run` to create only the missing manifest entities. The operation is idempotent and
does not edit foreign channels.

Verify the live state before every capture:

```bash
pass-cli run --env-file "$FERRY_CAPTURE_ENV" -- \
  uv run python -m tests.provisioning.provision_test_server verify \
  --guild-id "$FERRY_FIXTURE_GUILD_ID"
```

Exit `0` means the manifest matches. Exit `1` means drift. Exit `2` means the command could not
decide because authentication, network access, or the manifest failed. Do not capture after either
nonzero result.

To remove only marked fixture channels, run:

```bash
pass-cli run --env-file "$FERRY_CAPTURE_ENV" -- \
  uv run python -m tests.provisioning.provision_test_server teardown \
  --guild-id "$FERRY_FIXTURE_GUILD_ID"
```

## Capture with DCE 2.48

Resolve `DCE_BIN` through Ferry's managed downloader. It selects the current platform, verifies the
published archive digest, and extracts the self-contained command-line package. Create a new
temporary directory for each capture:

```bash
export DCE_BIN="$HOME/.discord-ferry/bin/dce/2.48/DiscordChatExporter.Cli"
export CAPTURE_DIR="$(mktemp -d /tmp/ferry-dce-2.48-capture.XXXXXX)"
```

Run the same argument shape as Ferry's exporter. The shell expands the injected token only inside
the child process. Neither the command text nor this guide contains its value:

```bash
pass-cli run --env-file "$FERRY_CAPTURE_ENV" -- sh -c '
  exec "$1" exportguild \
    --token "$DISCORD_TEST_BOT_TOKEN" \
    -g "$2" \
    --media \
    --reuse-media \
    --markdown false \
    --format Json \
    --include-threads All \
    --output "$3"
' sh "$DCE_BIN" "$FERRY_FIXTURE_GUILD_ID" "$CAPTURE_DIR"
```

Restore the original application flags even when DCE fails. A successful DCE exit does not replace
the review below.

## Review before import

Stop and discard the temporary capture when any check finds:

- a failed manifest verification or DCE process;
- an unexpected guild, channel, author, or fixture marker;
- a credential or authorization header;
- private or unrelated message content;
- an absolute filesystem path;
- a local media path that leaves the capture directory;
- an application flag that was not restored.

Inspect identities, channels, marker content, and local paths without printing credential values:

```bash
jq -c '{guild,channel,messageCount,exportedAt}' "$CAPTURE_DIR"/*.json
rg -n '\[ferry:|\[ferry-fixture\]' "$CAPTURE_DIR" -g '*.json'
rg -n '(mfa\.|Authorization|Bearer |/Users/|/home/|[A-Za-z]:\\)' \
  "$CAPTURE_DIR" -g '*.json'
```

Compare every `[ferry:<manifest-id>]` marker with `fixture-spec.json`. Copy only reviewed fixture
channels and their referenced media under `tests/fixtures/dce_2_48/captured/`. Record DCE version,
release target and digest, capture date, verification and DCE exit codes, included and excluded
channel IDs, application flags, and the redaction result in `provenance.json`.

Run the local replay and privacy gate:

```bash
uv run pytest tests/test_dce_2_48_evidence.py tests/provisioning -v
```

## Marker and CI rules

Fixture channel topics start with `[ferry-fixture]`. Fixture messages end with
`[ferry:<manifest-id>]`. Do not edit these markers in Discord because the provisioner uses them for
idempotency and drift detection.

The provisioning command and live DCE capture are operator-only. Continuous integration runs only
the mocked provisioning tests and reviewed local evidence. It must not hold a Discord credential or
call the live fixture guild.
