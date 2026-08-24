// Discord Ferry — shared destructive git detection.
// Used by the Qwen guard and the Codex and Vibe adapters.
//
// The detector splits a shell line into segments, locates each git invocation
// (through env assignments and common wrappers), skips git global options,
// and then judges the subcommand and its flags. That survives whitespace,
// option order, short-option clustering (-df, -uf), long options, and global
// arguments like -C <path>. It is deliberately conservative: anything
// ambiguous in a destructive direction asks the user, so false positives are
// acceptable and false negatives are not.

const VALUE_GLOBAL_OPTIONS = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix']);
const WRAPPERS = new Set(['sudo', 'nohup', 'time', 'nice', 'env']);

function segments(cmd) {
  // Shell separators. Separators inside quotes also split, which can only
  // add false positives, never false negatives.
  return cmd.split(/[|;&\n`]|\$\(/);
}

function gitArgTokens(segment) {
  const tokens = segment.trim().split(/\s+/).filter(Boolean);
  let i = 0;
  while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) i += 1;
  while (i < tokens.length && WRAPPERS.has(tokens[i])) i += 1;
  const binary = tokens[i];
  if (binary !== 'git' && !(binary ?? '').endsWith('/git')) return null;
  return tokens.slice(i + 1);
}

function subcommandAndFlags(args) {
  let i = 0;
  while (i < args.length) {
    const a = args[i];
    if (!a.startsWith('-')) return { subcommand: a, rest: args.slice(i + 1) };
    // Global options that consume a following value.
    if (VALUE_GLOBAL_OPTIONS.has(a)) i += 2;
    else i += 1;
  }
  return null;
}

function hasFlag(flags, ...names) {
  return flags.some(f => names.includes(f));
}

function hasShortFlag(flags, letter) {
  // Matches -f on its own or inside a cluster like -df, -uf, -ffx.
  return flags.some(f => /^-[a-zA-Z]+$/.test(f) && f.slice(1).includes(letter));
}

function isDestructiveGitSegment(segment) {
  const args = gitArgTokens(segment);
  if (!args) return false;
  const parsed = subcommandAndFlags(args);
  if (!parsed) return false;
  const { subcommand, rest } = parsed;

  switch (subcommand) {
    case 'reset':
      return hasFlag(rest, '--hard');
    case 'push':
      // --force-with-lease included: it is still a force push, and asking is cheap.
      return hasFlag(rest, '--force', '--force-with-lease') || hasShortFlag(rest, 'f');
    case 'clean':
      return hasFlag(rest, '--force') || hasShortFlag(rest, 'f');
    case 'branch':
      return hasFlag(rest, '-D') || (hasFlag(rest, '--delete') && hasFlag(rest, '--force'));
    case 'checkout': {
      const dashdash = rest.indexOf('--');
      return dashdash >= 0 && rest.slice(dashdash + 1).includes('.');
    }
    case 'restore': {
      const dots = rest.filter(f => f === '.' || f === './');
      if (dots.length === 0) return false;
      // --staged alone restores the index, not the working tree.
      return !hasFlag(rest, '--staged') || hasFlag(rest, '--worktree');
    }
    default:
      return false;
  }
}

export function isDestructiveGitCommand(cmd) {
  if (typeof cmd !== 'string' || cmd === '') return false;
  return segments(cmd).some(isDestructiveGitSegment);
}
