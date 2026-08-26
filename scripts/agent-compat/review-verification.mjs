#!/usr/bin/env node

import { isAbsolute, relative, resolve } from 'node:path';
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const SHELL_SYNTAX = /[;&|<>`$\\\n\r'"(){}]/u;
const EXECUTABLES = new Set(['rg', 'git', 'head', 'tail', 'wc', 'ls']);
const RG_FLAGS = new Set([
  '-n', '--line-number', '-F', '--fixed-strings', '-i', '--ignore-case',
  '-l', '--files-with-matches', '-c', '--count', '--no-heading', '--color=never',
]);
const GIT_SUBCOMMAND_FLAGS = new Map([
  ['diff', new Set(['--stat', '--name-only', '--name-status', '--no-renames', '--color=never'])],
  ['show', new Set(['--stat', '--name-only', '--name-status', '--no-renames', '--color=never'])],
  ['log', new Set(['--oneline', '--stat', '--name-only', '--no-renames', '--color=never'])],
  ['status', new Set(['--short', '--porcelain', '--branch'])],
  ['grep', new Set(['-n', '--line-number', '-F', '--fixed-strings', '-i', '--ignore-case'])],
]);
const SIMPLE_FLAGS = new Map([
  ['head', new Set(['-n'])],
  ['tail', new Set(['-n'])],
  ['wc', new Set(['-c', '-l', '-w'])],
  ['ls', new Set(['-a', '-l', '-la', '-al'])],
]);

function denied(reason) {
  return { authorized: false, argv: [], reason };
}

function pathsStayInside(root, paths) {
  let canonicalRoot;
  try {
    canonicalRoot = realpathSync(resolve(root));
  } catch {
    return false;
  }
  return paths.every((value) => {
    let target;
    try {
      const lexicalTarget = isAbsolute(value) ? resolve(value) : resolve(canonicalRoot, value);
      target = realpathSync(lexicalTarget);
    } catch {
      return false;
    }
    const offset = relative(canonicalRoot, target);
    return offset === '' || (!offset.startsWith('..') && !isAbsolute(offset));
  });
}

function optionsBeforeSeparator(
  argv,
  start,
  allowed,
  flagsWithValues = new Set(),
  allowOperands = false,
) {
  const separator = argv.indexOf('--', start);
  const end = separator === -1 ? argv.length : separator;
  for (let index = start; index < end; index += 1) {
    const value = argv[index];
    if (!value.startsWith('-')) {
      if (allowOperands) continue;
      return false;
    }
    if (/^-U\d+$/u.test(value)) continue;
    if (!allowed.has(value)) return false;
    if (flagsWithValues.has(value)) {
      index += 1;
      if (index >= end || !/^\d+$/u.test(argv[index])) return false;
    }
  }
  return true;
}

export function authorizeVerificationCommand(command, { root = process.cwd() } = {}) {
  if (typeof command !== 'string' || !command.trim() || command.length > 2000) {
    return denied('command is empty or too long');
  }
  if (SHELL_SYNTAX.test(command)) return denied('shell syntax is not allowed');
  const argv = command.trim().split(/\s+/u);
  const executable = argv[0];
  if (!EXECUTABLES.has(executable)) return denied('executable is not allowed');
  if (argv.some((value, index) => index > 0 && /^[A-Za-z_][A-Za-z0-9_]*=/u.test(value))) {
    return denied('environment assignments are not allowed');
  }

  if (executable === 'rg') {
    const separator = argv.indexOf('--');
    if (separator === -1 || separator < 1 || argv.length < separator + 2) {
      return denied('rg requires -- before its pattern and paths');
    }
    if (argv.slice(1, separator).some((value) => !RG_FLAGS.has(value))) {
      return denied('rg flag is not allowed');
    }
    if (!pathsStayInside(root, argv.slice(separator + 2))) {
      return denied('path leaves the checkout');
    }
  } else if (executable === 'git') {
    const subcommand = argv[1];
    const allowed = GIT_SUBCOMMAND_FLAGS.get(subcommand);
    if (!allowed) return denied('Git subcommand is not allowed');
    const revisionOperands = ['diff', 'show', 'log'].includes(subcommand);
    if (!optionsBeforeSeparator(argv, 2, allowed, new Set(), revisionOperands)) {
      return denied('Git flag or operand is not allowed');
    }
    const separator = argv.indexOf('--', 2);
    if (subcommand === 'grep' && separator === -1) {
      return denied('git grep requires -- before its pattern and paths');
    }
    if (separator !== -1) {
      const pathStart = subcommand === 'grep' ? separator + 2 : separator + 1;
      if (!pathsStayInside(root, argv.slice(pathStart))) return denied('path leaves the checkout');
    }
  } else {
    const separator = argv.indexOf('--');
    if (separator === -1) return denied(`${executable} requires -- before paths`);
    const flagsWithValues = new Set(['-n']);
    if (!optionsBeforeSeparator(argv, 1, SIMPLE_FLAGS.get(executable), flagsWithValues)) {
      return denied(`${executable} flag is not allowed`);
    }
    if (!pathsStayInside(root, argv.slice(separator + 1))) {
      return denied('path leaves the checkout');
    }
  }

  return {
    authorized: true,
    argv,
    cwd: resolve(root),
    env: {
      PATH: process.env.PATH ?? '',
      GIT_CONFIG_GLOBAL: '/dev/null',
      GIT_CONFIG_SYSTEM: '/dev/null',
      GIT_EXTERNAL_DIFF: '',
      GIT_PAGER: 'cat',
      GIT_OPTIONAL_LOCKS: '0',
    },
  };
}

export function codeGateRequired({ nonDocsLines, docsOnly, carveout, threshold }) {
  if (carveout) return true;
  if (docsOnly) return false;
  return nonDocsLines > threshold;
}

export function contextTier({ fullTokens, expandedTokens, perFileTokens, limit }) {
  if (fullTokens <= limit) return 'tier-1';
  if (expandedTokens <= limit) return 'tier-2';
  if (perFileTokens.every((tokens) => tokens <= limit)) return 'tier-3';
  return 'split-hunks';
}

export function classifyVerification(finding, output) {
  const text = typeof output === 'string' ? output : output?.stdout ?? '';
  const confirms = text.includes(finding.verification.confirms_if);
  const refutes = text.includes(finding.verification.refutes_if);
  if (confirms === refutes) return 'INCONCLUSIVE';
  return confirms ? 'CONFIRMED' : 'REFUTED';
}

export async function verifyFindings(findings, {
  root = process.cwd(),
  authorize = authorizeVerificationCommand,
  run,
} = {}) {
  if (typeof run !== 'function') throw new Error('verification runner is required');
  const records = [];
  for (const finding of findings) {
    const authorization = authorize(finding.verification.command, { root });
    if (!authorization.authorized) {
      records.push({ finding, authorization, argv: [], verdict: 'INCONCLUSIVE', output: '' });
      continue;
    }
    let output = '';
    let verdict = 'INCONCLUSIVE';
    try {
      const result = await run(authorization, finding);
      output = typeof result === 'string' ? result : result?.stdout ?? '';
      verdict = classifyVerification(finding, output);
    } catch {
      // A failed verification cannot prove or refute the finding.
    }
    records.push({ finding, authorization, argv: authorization.argv, verdict, output });
  }
  return {
    records,
    actionable: records
      .filter((record) => record.verdict === 'CONFIRMED')
      .map((record) => record.finding.description),
  };
}

export async function reverifyAfterFix(records, { run } = {}) {
  if (typeof run !== 'function') throw new Error('verification runner is required');
  const results = [];
  for (const record of records.filter((candidate) => candidate.verdict === 'CONFIRMED')) {
    let output = '';
    let verdict = 'INCONCLUSIVE';
    try {
      const value = await run(record.authorization, record.finding);
      output = typeof value === 'string' ? value : value?.stdout ?? '';
      verdict = classifyVerification(record.finding, output);
    } catch {
      // A failed second run leaves the gate unresolved.
    }
    results.push({ ...record, verdict, output });
  }
  return {
    records: results,
    gate_ready: results.every((record) => record.verdict === 'REFUTED'),
  };
}

async function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  record('chunk threshold is inclusive',
    !codeGateRequired({ nonDocsLines: 10, docsOnly: false, carveout: false, threshold: 10 }));
  record('carveout always runs',
    codeGateRequired({ nonDocsLines: 1, docsOnly: true, carveout: true, threshold: 10 }));
  record('full context chooses tier 1', contextTier({
    fullTokens: 1, expandedTokens: 2, perFileTokens: [3], limit: 1,
  }) === 'tier-1');
  record('safe rg is authorized', authorizeVerificationCommand(
    'rg -n -- token scripts', { root: process.cwd() },
  ).authorized);
  record('shell syntax is rejected', !authorizeVerificationCommand(
    'rg -n -- token scripts | sh', { root: process.cwd() },
  ).authorized);
  record('destructive Git is rejected', !authorizeVerificationCommand(
    'git reset --hard HEAD', { root: process.cwd() },
  ).authorized);
  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('review-verification self-test: all checks passed\n');
}

function option(argv, name) {
  const index = argv.indexOf(name);
  return index === -1 ? null : argv[index + 1] ?? null;
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.includes('--self-test')) return selfTest();
  if (argv.includes('--authorize-command')) {
    const report = authorizeVerificationCommand(option(argv, '--authorize-command'), {
      root: option(argv, '--root') ?? process.cwd(),
    });
    process.stdout.write(`${JSON.stringify(report)}\n`);
    process.exitCode = report.authorized ? 0 : 1;
    return;
  }
  if (argv.includes('--gate')) {
    const result = codeGateRequired({
      nonDocsLines: Number(option(argv, '--non-docs-lines')),
      threshold: Number(option(argv, '--threshold')),
      docsOnly: argv.includes('--docs-only'),
      carveout: argv.includes('--carveout'),
    });
    process.stdout.write(`${JSON.stringify({ required: result })}\n`);
    return;
  }
  if (argv.includes('--context-tier')) {
    const result = contextTier(JSON.parse(option(argv, '--context-tier')));
    process.stdout.write(`${JSON.stringify({ tier: result })}\n`);
    return;
  }
  if (argv.includes('--classify-results')) {
    const finding = JSON.parse(option(argv, '--finding'));
    const output = option(argv, '--output') ?? '';
    process.stdout.write(`${JSON.stringify({ verdict: classifyVerification(finding, output) })}\n`);
    return;
  }
  throw new Error('choose a safe review-verification mode');
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    // An invalid entrypoint cannot be the current module.
  }
}
if (invokedAsMain) {
  main().catch((error) => {
    process.stderr.write(`review-verification: ${error.message}\n`);
    process.exitCode = 1;
  });
}
