#!/usr/bin/env node

import { isAbsolute, relative, resolve } from 'node:path';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
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
const VERIFICATION_FAILURE_CLASSES = new Set([
  'approval-denied',
  'child-exit',
  'signal',
  'timeout',
  'unknown',
]);
const VERIFICATION_ARTIFACT_DIRECTORY = ['docs', 'plans', '.review', 'verification'];
const MAX_VERIFICATION_ARTIFACT_BYTES = 2_097_152;

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

function pathStaysInside(root, target) {
  const offset = relative(root, target);
  return offset === '' || (!offset.startsWith('..') && !isAbsolute(offset));
}

export function readVerificationArtifact(file, { root = process.cwd() } = {}) {
  if (typeof file !== 'string' || !file.trim()) throw new Error('artifact path is required');
  const canonicalRoot = realpathSync(resolve(root));
  const artifactDirectory = resolve(canonicalRoot, ...VERIFICATION_ARTIFACT_DIRECTORY);
  const canonicalArtifactDirectory = realpathSync(artifactDirectory);
  if (!pathStaysInside(canonicalRoot, canonicalArtifactDirectory)) {
    throw new Error('artifact directory leaves the checkout');
  }
  const lexicalTarget = isAbsolute(file) ? resolve(file) : resolve(canonicalRoot, file);
  if (!pathStaysInside(artifactDirectory, lexicalTarget)) {
    throw new Error('artifact path leaves its dedicated directory');
  }
  const metadata = lstatSync(lexicalTarget);
  if (metadata.isSymbolicLink() || !metadata.isFile()) {
    throw new Error('artifact must be a regular non-linked file');
  }
  if (metadata.size > MAX_VERIFICATION_ARTIFACT_BYTES) {
    throw new Error('artifact exceeds 2097152 bytes');
  }
  const canonicalTarget = realpathSync(lexicalTarget);
  if (!pathStaysInside(canonicalArtifactDirectory, canonicalTarget)) {
    throw new Error('artifact path leaves its dedicated directory');
  }
  try {
    return JSON.parse(readFileSync(canonicalTarget, 'utf8'));
  } catch {
    throw new Error('artifact contains invalid JSON');
  }
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

export function parseVerificationCommand(command) {
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
    if (separator === -1 || separator < 1 || argv.length < separator + 3) {
      return denied('rg requires -- before its pattern and paths');
    }
    if (argv.slice(1, separator).some((value) => !RG_FLAGS.has(value))) {
      return denied('rg flag is not allowed');
    }
    return { authorized: true, argv, paths: argv.slice(separator + 2) };
  } else if (executable === 'git') {
    const subcommand = argv[1];
    const allowed = GIT_SUBCOMMAND_FLAGS.get(subcommand);
    if (!allowed) return denied('Git subcommand is not allowed');
    const revisionOperands = ['diff', 'show', 'log'].includes(subcommand);
    if (!optionsBeforeSeparator(argv, 2, allowed, new Set(), revisionOperands)) {
      return denied('Git flag or operand is not allowed');
    }
    const separator = argv.indexOf('--', 2);
    if (subcommand === 'grep' && (separator === -1 || argv.length < separator + 3)) {
      return denied('git grep requires -- before its pattern and paths');
    }
    const safeArgv = ['diff', 'show'].includes(subcommand)
      ? [argv[0], subcommand, '--no-ext-diff', '--no-textconv', ...argv.slice(2)]
      : argv;
    if (separator !== -1) {
      const pathStart = subcommand === 'grep' ? separator + 2 : separator + 1;
      if (argv.length === pathStart) return denied('Git path is missing');
      return { authorized: true, argv: safeArgv, paths: argv.slice(pathStart) };
    }
    return { authorized: true, argv: safeArgv, paths: [] };
  } else {
    const separator = argv.indexOf('--');
    if (separator === -1 || argv.length === separator + 1) {
      return denied(`${executable} requires -- before paths`);
    }
    const flagsWithValues = new Set(['-n']);
    if (!optionsBeforeSeparator(argv, 1, SIMPLE_FLAGS.get(executable), flagsWithValues)) {
      return denied(`${executable} flag is not allowed`);
    }
    return { authorized: true, argv, paths: argv.slice(separator + 1) };
  }
}

export function authorizeVerificationCommand(command, { root = process.cwd() } = {}) {
  const parsed = parseVerificationCommand(command);
  if (!parsed.authorized) return parsed;
  if (!pathsStayInside(root, parsed.paths)) return denied('path leaves the checkout');
  return {
    authorized: true,
    argv: parsed.argv,
    cwd: resolve(root),
    env: {
      PATH: process.env.PATH ?? '',
      GIT_CONFIG_GLOBAL: '/dev/null',
      GIT_CONFIG_SYSTEM: '/dev/null',
      GIT_EXTERNAL_DIFF: '',
      GIT_PAGER: 'cat',
      GIT_OPTIONAL_LOCKS: '0',
      GIT_CONFIG_COUNT: '1',
      GIT_CONFIG_KEY_0: 'core.fsmonitor',
      GIT_CONFIG_VALUE_0: 'false',
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

function exactKeys(value, keys) {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function validVerificationResult(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false;
  if (result.status === 'completed') {
    return exactKeys(result, ['status', 'exit_code', 'stdout', 'stderr'])
      && Number.isInteger(result.exit_code)
      && typeof result.stdout === 'string'
      && typeof result.stderr === 'string';
  }
  return result.status === 'failed'
    && exactKeys(result, ['status', 'failure_class'])
    && VERIFICATION_FAILURE_CLASSES.has(result.failure_class);
}

function failedVerification(error) {
  if (error?.name === 'TimeoutError' || error?.code === 'ETIMEDOUT') {
    return { status: 'failed', failure_class: 'timeout' };
  }
  if (error?.signal) return { status: 'failed', failure_class: 'signal' };
  return { status: 'failed', failure_class: 'child-exit' };
}

function outcomeMatches(outcome, result) {
  return result.exit_code === outcome.exit_code
    && (outcome.stdout_contains === null || result.stdout.includes(outcome.stdout_contains))
    && (outcome.stdout_excludes === null || !result.stdout.includes(outcome.stdout_excludes));
}

export function verificationExitAllowed(argv, exitCode) {
  const search = argv[0] === 'rg' || (argv[0] === 'git' && argv[1] === 'grep');
  return exitCode === 0 || (search && exitCode === 1);
}

export function classifyVerification(finding, result, authorization) {
  if (!validVerificationResult(result) || result.status !== 'completed') return 'INCONCLUSIVE';
  if (!verificationExitAllowed(authorization.argv, result.exit_code)) return 'INCONCLUSIVE';
  const confirms = outcomeMatches(finding.verification.confirms_if, result);
  const refutes = outcomeMatches(finding.verification.refutes_if, result);
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
      records.push({
        finding,
        authorization,
        argv: [],
        verdict: 'INCONCLUSIVE',
        result: { status: 'failed', failure_class: 'approval-denied' },
      });
      continue;
    }
    let result = { status: 'failed', failure_class: 'unknown' };
    let verdict = 'INCONCLUSIVE';
    try {
      const value = await run(authorization, finding);
      result = validVerificationResult(value)
        ? value
        : { status: 'failed', failure_class: 'unknown' };
      verdict = classifyVerification(finding, result, authorization);
    } catch (error) {
      result = failedVerification(error);
    }
    records.push({ finding, authorization, argv: authorization.argv, verdict, result });
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
    let result = { status: 'failed', failure_class: 'unknown' };
    let verdict = 'INCONCLUSIVE';
    try {
      const value = await run(record.authorization, record.finding);
      result = validVerificationResult(value)
        ? value
        : { status: 'failed', failure_class: 'unknown' };
      verdict = classifyVerification(record.finding, result, record.authorization);
    } catch (error) {
      result = failedVerification(error);
    }
    results.push({ ...record, verdict, result });
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
  if (argv.includes('--authorize-finding')) {
    const root = option(argv, '--root') ?? process.cwd();
    const finding = readVerificationArtifact(option(argv, '--authorize-finding'), { root });
    const report = authorizeVerificationCommand(finding?.verification?.command, { root });
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
  if (argv.includes('--classify-files')) {
    const root = option(argv, '--root') ?? process.cwd();
    const finding = readVerificationArtifact(option(argv, '--finding-file'), { root });
    const result = readVerificationArtifact(option(argv, '--result-file'), { root });
    const authorization = authorizeVerificationCommand(finding?.verification?.command, { root });
    const verdict = authorization.authorized
      ? classifyVerification(finding, result, authorization)
      : 'INCONCLUSIVE';
    process.stdout.write(`${JSON.stringify({ verdict })}\n`);
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
