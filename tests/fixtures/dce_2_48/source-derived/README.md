# Rebuild the 2.48 source-derived export fixture

This page is for maintainers reviewing the 2.48 contract for the tool that writes Discord chats as
JSON, DiscordChatExporter (DCE). It rebuilds the maximal dummy export in scratch from generator
code reviewed against the pinned writer, then checks every generated JSON path against the
disposition ledger.

The result is a source-derived reconstruction. The generator does not compile or run DCE, contact
Discord, or turn the dummy values into evidence of a captured export.

## Acquire the exact writer before entering the offline environment

Fetch only the file named in `provenance.json`, at commit
`905489b01b5523719082275c34c232fc47f827c6`. Do this outside the regeneration environment because
that environment must have no network access.

```bash
mkdir -p /private/tmp/ferry-dce-2-48-source
curl -L \
  https://raw.githubusercontent.com/Tyrrrz/DiscordChatExporter/905489b01b5523719082275c34c232fc47f827c6/DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs \
  -o /private/tmp/ferry-dce-2-48-source/JsonMessageWriter.cs
shasum -a 256 /private/tmp/ferry-dce-2-48-source/JsonMessageWriter.cs
chmod 444 /private/tmp/ferry-dce-2-48-source/JsonMessageWriter.cs
```

The expected digest is:

```text
2b7d17c45f95c68437c598eaa2dec6b17317cc32dbc1416d4e02ab7c9a675127
```

The generator refuses any other bytes.

## Rebuild into an assigned scratch directory with no network

Enter an isolated environment with network access disabled. Mount the repository and pinned
writer read-only, allow writes only under the assigned scratch directory, and set a CPU and output
file limit before running the generator. Start the command with only the listed environment value.

```bash
mkdir -p /private/tmp/ferry-dce-2-48-output
(
  ulimit -t 10
  ulimit -f 2048
  env -i PATH=/usr/bin:/bin /usr/bin/python3 \
    scripts/regenerate_dce_2_48_source_fixture.py \
    --source /private/tmp/ferry-dce-2-48-source/JsonMessageWriter.cs \
    --output /private/tmp/ferry-dce-2-48-output/maximal-writer-shape.json
)
```

The generator contains only invented values. Its object builders follow the pinned writer's helper
methods, preamble, message body, and postamble. The writer ranges reviewed for this fixture are
lines 41 through 641 of `JsonMessageWriter.cs` at the pinned commit.

## Compare the rebuilt bytes and review the ledger

Run both checks inside the same offline environment:

```bash
cmp \
  /private/tmp/ferry-dce-2-48-output/maximal-writer-shape.json \
  tests/fixtures/dce_2_48/source-derived/maximal-writer-shape.json
env -i PATH=/usr/bin:/bin /usr/bin/python3 \
  scripts/regenerate_dce_2_48_source_fixture.py \
  --source /private/tmp/ferry-dce-2-48-source/JsonMessageWriter.cs \
  --check
```

`cmp` proves that regeneration is byte-for-byte repeatable. `--check` authenticates the source,
rebuilds the fixture in memory, compares it with the committed JSON, and requires its paths to
equal `field-dispositions.json`.

The source digest proves which writer was reviewed. It does not prove that a maintainer mapped a
new writer correctly. If the pinned writer changes, review its write calls, update the generator's
dummy builders, classify every added or removed path in the ledger, and preserve the
`source-derived` evidence class.
