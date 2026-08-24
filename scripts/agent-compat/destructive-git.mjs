// Discord Ferry — shared destructive git detection.
// Used by the Qwen guard and the Codex and Vibe adapters. Substrings match
// regardless of git's global options (-C <path>, -c key=value) between the
// binary and the subcommand. The clean regex catches short-option clusters
// (-df, -fd, -ffx) and --force, which plain "-f" prefixes miss.

const DESTRUCTIVE_SUBSTRINGS = [
  'reset --hard',
  'push --force',
  'push -f',
  'branch -D',
  'checkout -- .',
  'restore .',
];

const CLEAN_FORCE = /\bclean\b[^|;&()]*-[a-zA-Z]*f/;

export function isDestructiveGitCommand(cmd) {
  if (typeof cmd !== 'string' || cmd === '') return false;
  for (const sub of DESTRUCTIVE_SUBSTRINGS) {
    if (cmd.includes(sub)) return true;
  }
  return CLEAN_FORCE.test(cmd);
}
