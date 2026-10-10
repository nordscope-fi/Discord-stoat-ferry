// Discord Ferry — shared destructive git detection.
// Used by the Qwen guard and the Codex and Vibe adapters.
//
// The detector splits a shell line into segments, reads each segment as shell
// words (quotes and backslashes removed), locates each git invocation (through
// env assignments, shell keywords, common wrappers and `sh -c` strings), skips
// git global options, and then judges the subcommand and its flags. That
// survives whitespace, quoting, option order, short-option clustering (-df,
// -uf), long options and their unambiguous abbreviations (--forc), and global
// arguments like -C <path>. It is deliberately conservative: anything
// ambiguous in a destructive direction asks the user, so false positives are
// acceptable and false negatives are not.
//
// It does not expand variables, aliases, functions or command substitutions
// that build the git command at run time (for example git${IFS}reset), and it
// does not follow `find -exec`. Tests in tests/test_destructive_git.py pin
// what is covered.

const VALUE_GLOBAL_OPTIONS = new Set([
  '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix',
  '--config-env', '--attr-source',
]);

// Wrapper commands, each with the options that consume a following value.
// `timeout` also takes one positional (the duration) before the command.
const WRAPPERS = new Map([
  ['sudo', new Set(['-u', '-g', '-h', '-p', '-r', '-t', '-C', '-D', '-R', '-T', '-U'])],
  ['env', new Set(['-u', '-C', '-S'])],
  ['nice', new Set(['-n'])],
  ['xargs', new Set(['-n', '-I', '-P', '-L', '-s', '-d', '-E', '-a'])],
  ['timeout', new Set(['-s', '-k'])],
  ['nohup', new Set()],
  ['time', new Set()],
  ['command', new Set()],
  ['builtin', new Set()],
  ['exec', new Set(['-a'])],
]);

// Words that can start a command position without being the command.
const SHELL_KEYWORDS = new Set([
  '{', '}', '!', 'if', 'then', 'else', 'elif', 'do', 'while', 'until', 'coproc',
]);
const SHELLS = new Set(['sh', 'bash', 'zsh', 'dash', 'ksh', 'ash', 'fish']);
const MAX_DEPTH = 4;

// Index of the ) that closes a $( ... ) body starting at `start`. Brackets
// inside quotes do not count, and nested ones do. An unterminated body runs
// to the end of the line.
function substitutionEnd(cmd, start) {
  let depth = 1;
  let quote = null;
  for (let j = start; j < cmd.length; j += 1) {
    const c = cmd[j];
    if (quote === "'") {
      if (c === "'") quote = null;
    } else if (c === '\\') {
      j += 1;
    } else if (quote === '"') {
      if (c === '"') quote = null;
    } else if (c === "'" || c === '"') {
      quote = c;
    } else if (c === '(') {
      depth += 1;
    } else if (c === ')' && (depth -= 1) === 0) {
      return j;
    }
  }
  return cmd.length;
}

function backtickEnd(cmd, start) {
  for (let j = start; j < cmd.length; j += 1) {
    if (cmd[j] === '\\') j += 1;
    else if (cmd[j] === '`') return j;
  }
  return cmd.length;
}

// Splits a command line into simple commands the way the shell does. A
// separator or bracket only splits outside quotes and when not escaped, so
// "feat(x)" and 'a;b' stay one word. $(...) and backticks run even inside
// double quotes, so their bodies become segments of their own. A backslash
// before a newline continues the line, and a # that starts a word begins a
// comment that runs to the end of the line.
function segments(cmd) {
  const line = cmd.replace(/\\\r?\n/g, '');
  const out = [];
  let cur = '';
  let quote = null; // null, "'", '"' or "$'"
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote === "'") {
      cur += ch;
      if (ch === "'") quote = null;
    } else if (quote === "$'") {
      cur += ch;
      if (ch === '\\' && i + 1 < line.length) { i += 1; cur += line[i]; }
      else if (ch === "'") quote = null;
    } else if (ch === '\\' && i + 1 < line.length) {
      cur += ch + line[i + 1];
      i += 1;
    } else if ((ch === '$' && line[i + 1] === '(') || ch === '`') {
      const start = ch === '`' ? i + 1 : i + 2;
      const end = ch === '`' ? backtickEnd(line, start) : substitutionEnd(line, start);
      out.push(...segments(line.slice(start, end)));
      cur += 'X';
      i = end;
    } else if (quote === '"') {
      cur += ch;
      if (ch === '"') quote = null;
    } else if (ch === '$' && line[i + 1] === "'") {
      cur += "$'";
      i += 1;
      quote = "$'";
    } else if (ch === "'" || ch === '"') {
      cur += ch;
      quote = ch;
    } else if (ch === '#' && (cur === '' || /\s$/.test(cur))) {
      // A # that starts a word comments out the rest of the line.
      const nl = line.indexOf('\n', i);
      i = nl < 0 ? line.length : nl - 1;
    } else if (/[|;&\n()]/.test(ch)) {
      out.push(cur);
      cur = '';
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
}

// Reads one segment as shell words. Quotes and backslash escapes are removed
// and an unterminated quote runs to the end, so 'a b' and a\ b are one word.
function shellWords(segment) {
  const words = [];
  let word = '';
  let inWord = false;
  let quote = null;
  for (let i = 0; i < segment.length; i += 1) {
    const ch = segment[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else word += ch;
    } else if (quote === "$'") {
      if (ch === "'") quote = null;
      else if (ch === '\\' && i + 1 < segment.length) { i += 1; word += segment[i]; }
      else word += ch;
    } else if (quote === '"') {
      if (ch === '"') quote = null;
      else if (ch === '\\' && i + 1 < segment.length) { i += 1; word += segment[i]; }
      else word += ch;
    } else if (ch === '$' && segment[i + 1] === "'") {
      // $'...' (ANSI-C) quoting, where a backslash escapes the next character.
      quote = "$'";
      i += 1;
      inWord = true;
    } else if (ch === '$' && segment[i + 1] === '"') {
      // $"..." (locale) quoting reads like a double-quoted word.
      inWord = true;
    } else if (ch === "'" || ch === '"') {
      quote = ch;
      inWord = true;
    } else if (ch === '\\' && i + 1 < segment.length) {
      i += 1;
      word += segment[i];
      inWord = true;
    } else if (/\s/.test(ch)) {
      if (inWord) words.push(word);
      word = '';
      inWord = false;
    } else {
      word += ch;
      inWord = true;
    }
  }
  if (inWord) words.push(word);
  return words;
}

function baseName(word) {
  return word.slice(word.lastIndexOf('/') + 1);
}

function isGitBinary(word) {
  return /^git(\.exe)?$/.test(baseName(word));
}

// Skips env assignments, shell keywords and wrappers (with their options) and
// returns the index of the command word.
function commandIndex(tokens) {
  let i = 0;
  while (i < tokens.length) {
    const t = tokens[i];
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t) || SHELL_KEYWORDS.has(t)) {
      i += 1;
    } else if (WRAPPERS.has(baseName(t))) {
      const valueOptions = WRAPPERS.get(baseName(t));
      i += 1;
      while (i < tokens.length && tokens[i].startsWith('-') && tokens[i] !== '--') {
        i += valueOptions.has(tokens[i]) ? 2 : 1;
      }
      if (i < tokens.length && tokens[i] === '--') i += 1;
      if (baseName(t) === 'timeout') i += 1;
    } else {
      break;
    }
  }
  return i;
}

// `env -S 'cmd args'` splits its value into a command line and runs it.
// Only an env in wrapper position, before the command word at commandAt, counts.
function envSplitString(tokens, commandAt) {
  const e = tokens.findIndex(t => baseName(t) === 'env');
  if (e < 0 || e >= commandAt) return null;
  for (let j = e + 1; j < tokens.length && tokens[j].startsWith('-'); j += 1) {
    const t = tokens[j];
    if (t === '-S' || t === '--split-string') return tokens.slice(j + 1).join(' ');
    if (t.startsWith('--split-string=')) {
      return [t.slice('--split-string='.length), ...tokens.slice(j + 1)].join(' ');
    }
    if (t.startsWith('-S')) return [t.slice(2), ...tokens.slice(j + 1)].join(' ');
  }
  return null;
}

// Returns the git arguments for a segment, or null when it is not git. A shell
// run with -c, and eval, carry a script that is judged as a command line of its own.
function gitArgTokens(segment, depth) {
  const tokens = shellWords(segment);
  const i = commandIndex(tokens);
  if (depth < MAX_DEPTH) {
    const split = envSplitString(tokens, i);
    if (split !== null) return { script: split };
  }
  const binary = tokens[i];
  if (binary === undefined) return null;
  if (isGitBinary(binary)) return { args: tokens.slice(i + 1) };
  if (depth < MAX_DEPTH) {
    if (binary === 'eval') return { script: tokens.slice(i + 1).join(' ') };
    if (SHELLS.has(baseName(binary))) {
      const c = tokens.findIndex((t, j) => j > i && /^-[a-zA-Z]*c[a-zA-Z]*$/.test(t));
      const s = c >= 0 && tokens[c + 1] === '--' ? c + 2 : c + 1;
      if (c >= 0 && s < tokens.length) return { script: tokens[s] };
    }
  }
  return null;
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

// git accepts any unambiguous prefix of a long option and a trailing =value.
// An ambiguous prefix makes git refuse, so matching it only adds a harmless
// false positive. minPrefix keeps a bare --f from matching.
function hasLongOption(flags, full, minPrefix = 1) {
  return flags.some(f => {
    if (!f.startsWith('--')) return false;
    const name = f.slice(2).split('=')[0];
    return name.length >= minPrefix && full.slice(2).startsWith(name);
  });
}

// The flags of `git restore` with the --source value removed. -s takes a
// value, glued (-sSTABLE) or as the next word, so a capital S inside that
// value must not read as --staged.
function restoreFlags(args) {
  const flags = [];
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    if (a === '--') break;
    if (a.startsWith('--')) {
      flags.push(a);
      if (!a.includes('=') && hasLongOption([a], '--source', 3) && args[i + 1] !== '--') i += 1;
    } else if (/^-[a-zA-Z]+$/.test(a)) {
      const s = a.indexOf('s', 1);
      flags.push(s < 0 ? a : a.slice(0, s));
      // Whether git takes a following -- as the value of -s is not worth
      // betting on, so a -- there still ends the options.
      if (s === a.length - 1 && args[i + 1] !== '--') i += 1;
    }
  }
  return flags.filter(f => f !== '-');
}

function isDestructiveGitSegment(segment, depth) {
  const found = gitArgTokens(segment, depth);
  if (!found) return false;
  if (found.script !== undefined) return isDestructive(found.script, depth + 1);
  const parsed = subcommandAndFlags(found.args);
  if (!parsed) return false;
  const { subcommand, rest } = parsed;

  switch (subcommand) {
    case 'reset':
      return hasLongOption(rest, '--hard');
    case 'push':
      // --force-with-lease included: it is still a force push, and asking is cheap.
      // A leading + on a refspec forces that ref without any flag.
      return hasLongOption(rest, '--force', 3) || hasLongOption(rest, '--force-with-lease', 3)
        || hasLongOption(rest, '--force-if-includes', 3) || hasShortFlag(rest, 'f')
        || rest.some(a => a.startsWith('+') && a.length > 1);
    case 'clean':
      return hasLongOption(rest, '--force') || hasShortFlag(rest, 'f');
    case 'branch': {
      const deletes = hasShortFlag(rest, 'd') || hasLongOption(rest, '--delete', 2);
      const forces = hasShortFlag(rest, 'f') || hasLongOption(rest, '--force', 3);
      return hasShortFlag(rest, 'D') || (deletes && forces);
    }
    case 'checkout':
      // git checkout . and git checkout -- . discard the same changes.
      return rest.some(a => a === '.' || a === './');
    case 'restore': {
      if (!rest.some(a => a === '.' || a === './')) return false;
      // --staged alone restores the index, not the working tree.
      const flags = restoreFlags(rest);
      const staged = hasShortFlag(flags, 'S') || hasLongOption(flags, '--staged', 3);
      const worktree = hasShortFlag(flags, 'W') || hasLongOption(flags, '--worktree', 3);
      return !staged || worktree;
    }
    default:
      return false;
  }
}

function isDestructive(cmd, depth) {
  return segments(cmd).some(segment => isDestructiveGitSegment(segment, depth));
}

export function isDestructiveGitCommand(cmd) {
  if (typeof cmd !== 'string' || cmd === '') return false;
  return isDestructive(cmd, 0);
}
