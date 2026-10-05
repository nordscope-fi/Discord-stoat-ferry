# Security probes for the export path containment program

Bounded runtime validation for audit issues #955 and #956, kept in the repo so any host can
re-run the evidence. Each runner takes one argument: a scratch directory it may write to.

```bash
bash tests/security_probes/run-955.sh /path/to/scratch
bash tests/security_probes/run-956.sh /path/to/scratch
```

Exit code 0 means zero escapes reached the mocked upload boundary and every in-folder control
behaved; exit code 1 means regression. Result JSONs land in the scratch directory
(`result-955.json`, `v956/result-956.json`).

Controls: macOS `sandbox-exec` profile with deny-default and deny-network, writes limited to
the scratch subpath, empty allowlisted environment (`env -i`), no bytecode writes, CPU, process
and file-size rlimits. The scratch argument is canonicalized with `pwd -P` because
`sandbox-exec` matches canonical paths; a symlinked or `/tmp`-style argument would otherwise
render a profile that denies the probe's own writes. Process execution inside the sandbox is
unrestricted by design: the probes need the interpreter and coreutils, writes are scratch-only,
and the network is denied, so an executed binary cannot exfiltrate or persist anything.

The probes are not pytest tests: their names match neither `test_*.py` nor `*_test.py`, so the
suite never collects them. They import private migrator symbols on purpose; a rename there
breaks the harness and should update it in the same commit.
