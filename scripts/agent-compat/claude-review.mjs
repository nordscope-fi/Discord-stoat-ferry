#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { accessSync, constants, readFileSync, realpathSync } from 'node:fs';
import { delimiter, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  buildReviewPrompt,
  makeReviewRecord,
  parseJsonText,
  validateFindings,
} from './review-contract.mjs';

export const CLAUDE_MODEL_ALIAS = 'sonnet';
export const CLAUDE_CANONICAL_MODEL = 'claude-sonnet-5';
export const CLAUDE_EFFORT = 'medium';
export const CLAUDE_REVIEW_ARGS = [
  '-p',
  '--model', CLAUDE_MODEL_ALIAS,
  '--effort', CLAUDE_EFFORT,
  '--output-format', 'json',
  '--safe-mode',
  '--tools', '',
  '--prompt-suggestions', 'false',
  '--system-prompt', 'Return only review JSON. Do not access files, tools, or external data.',
];

const REQUIRED_HELP_FLAGS = ['--safe-mode', '--tools', '--prompt-suggestions'];

export function resolveClaudeCommand(pathValue = process.env.PATH ?? '') {
  for (const directory of pathValue.split(delimiter)) {
    if (!directory) continue;
    const candidate = join(directory, 'claude');
    try {
      accessSync(candidate, constants.X_OK);
      return realpathSync(candidate);
    } catch {
      // Continue through the command lookup path.
    }
  }
  const error = new Error('Claude client prerequisite missing');
  error.code = 'ENOENT';
  throw error;
}

export function classifyClaudeStderr(stderr) {
  if (/sandbox|permission denied|operation not permitted|\bEACCES\b/iu.test(stderr)) {
    return 'sandbox-visibility';
  }
  if (/not logged in|authentication required|please log in|invalid api key/iu.test(stderr)) {
    return 'host-auth';
  }
  return 'child-exit';
}

function stopProcessGroup(child, signal) {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch {
    // The process may already have exited.
  }
}

export function runClaudeChild(command, args, { input = null, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      env: process.env,
      stdio: [input === null ? 'ignore' : 'pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let spawnError = null;
    let timedOut = false;
    let overflowed = false;
    let killTimer = null;
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (overflowed) return;
      stdout += chunk;
      if (stdout.length > 12_000_000) {
        overflowed = true;
        stdout = '';
        stopProcessGroup(child, 'SIGTERM');
      }
    });
    child.stderr.on('data', (chunk) => {
      if (stderr.length < 256_000) stderr += chunk;
    });
    child.once('error', (error) => { spawnError = error; });
    if (child.stdin) {
      child.stdin.on('error', () => {});
      child.stdin.end(input);
    }
    const timer = setTimeout(() => {
      timedOut = true;
      stopProcessGroup(child, 'SIGTERM');
      killTimer = setTimeout(() => stopProcessGroup(child, 'SIGKILL'), 2000);
      killTimer.unref();
    }, timeoutMs);
    child.once('close', (status, signal) => {
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      if (spawnError) {
        reject(spawnError);
        return;
      }
      if (timedOut) {
        const error = new Error('Claude child timed out');
        error.name = 'TimeoutError';
        error.claudeClass = 'timeout';
        reject(error);
        return;
      }
      if (overflowed) {
        const error = new Error('Claude child output exceeded limit');
        error.claudeClass = 'child-exit';
        reject(error);
        return;
      }
      if (status !== 0) {
        const error = new Error('Claude child failed');
        error.status = status;
        error.signal = signal;
        error.claudeClass = classifyClaudeStderr(stderr);
        reject(error);
        return;
      }
      resolve({ stdout });
    });
  });
}

export function requireClaudeHelp(stdout) {
  const missing = REQUIRED_HELP_FLAGS.filter((flag) => !stdout.includes(flag));
  if (missing.length) throw new Error(`Claude client is missing required flag ${missing[0]}`);
}

export function parseClaudeEnvelope(raw) {
  const envelope = parseJsonText(raw, 'Claude envelope');
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    throw new Error('Claude envelope mismatch');
  }
  const usage = envelope.modelUsage?.[CLAUDE_CANONICAL_MODEL];
  if (usage?.canonicalModel !== CLAUDE_CANONICAL_MODEL) {
    const error = new Error('Claude response model mismatch');
    error.code = 'WRONG_MODEL';
    throw error;
  }
  if (typeof envelope.result !== 'string') throw new Error('Claude envelope has no result text');
  const text = envelope.result.trim()
    .replace(/^```(?:json)?\s*/u, '')
    .replace(/\s*```$/u, '');
  const result = parseJsonText(text, 'Claude review');
  if (!validateFindings(result)) throw new Error('Claude findings schema mismatch');
  return {
    result,
    sessionId: typeof envelope.session_id === 'string' ? envelope.session_id : null,
  };
}

function childFailure(error) {
  let message = 'Claude child failed';
  if (error?.code === 'ENOENT') message = 'Claude client prerequisite missing';
  else if (error?.name === 'TimeoutError' || error?.claudeClass === 'timeout') {
    message = 'Claude child timed out';
  } else if (error?.claudeClass === 'sandbox-visibility') {
    message = 'Claude sandbox visibility failed';
  } else if (error?.claudeClass === 'host-auth') {
    message = 'Claude host authentication failed';
  }
  const wrapped = new Error(message);
  wrapped.stage = error?.code === 'ENOENT' ? 'claude-client' : 'claude-child';
  wrapped.classification = error?.claudeClass ?? 'child-exit';
  return wrapped;
}

export async function runClaudeReview({
  prompt,
  slot = 'plan-sonnet',
  record = false,
  substitutionFor = null,
  substitutionReason = null,
  resolve = resolveClaudeCommand,
  run = runClaudeChild,
}) {
  let command;
  try {
    command = resolve();
  } catch (error) {
    throw childFailure(error);
  }
  let help;
  try {
    help = await run(command, ['--help'], { timeoutMs: 30000 });
  } catch (error) {
    throw childFailure(error);
  }
  requireClaudeHelp(help.stdout);
  const started = Date.now();
  let response;
  try {
    response = await run(command, CLAUDE_REVIEW_ARGS, {
      input: prompt,
      timeoutMs: 600000,
    });
  } catch (error) {
    throw childFailure(error);
  }
  let parsed;
  try {
    parsed = parseClaudeEnvelope(response.stdout);
  } catch {
    const error = new Error('Claude response was invalid');
    error.stage = 'claude-response';
    throw error;
  }
  if (!record) return parsed.result;
  return makeReviewRecord({
    adapter: 'claude',
    slot,
    requestedModel: CLAUDE_MODEL_ALIAS,
    resolvedModel: CLAUDE_CANONICAL_MODEL,
    substitutionFor,
    substitutionReason: substitutionFor ? substitutionReason ?? 'unknown' : null,
    sessionId: parsed.sessionId,
    durationMs: Date.now() - started,
    status: 'valid',
    result: parsed.result,
  });
}

function cleanEnvelope(result = {
  findings: [],
  summary: 'clean',
  confidence: 'high',
}) {
  return JSON.stringify({
    session_id: 'fixture-session',
    result: JSON.stringify(result),
    modelUsage: {
      [CLAUDE_CANONICAL_MODEL]: { canonicalModel: CLAUDE_CANONICAL_MODEL },
      'claude-haiku-4-5': { canonicalModel: 'claude-haiku-4-5' },
    },
  });
}

async function selfTest() {
  const checks = [];
  const check = (name, ok) => checks.push({ name, ok });
  const argv = CLAUDE_REVIEW_ARGS.join(' ');
  check('argv has --model sonnet', argv.includes('--model sonnet'));
  check('canonical model is claude-sonnet-5',
    CLAUDE_CANONICAL_MODEL === 'claude-sonnet-5');
  check('argv has --effort medium', argv.includes('--effort medium'));
  check('argv has --safe-mode --tools', argv.includes('--safe-mode --tools'));
  check('valid envelope accepts ancillary model metadata',
    parseClaudeEnvelope(cleanEnvelope()).result.confidence === 'high');
  let wrongModel = false;
  try {
    parseClaudeEnvelope(JSON.stringify({
      result: JSON.stringify({ findings: [], summary: 'clean', confidence: 'high' }),
      modelUsage: {
        'claude-opus-5': { canonicalModel: 'claude-opus-5' },
      },
    }));
  } catch {
    wrongModel = true;
  }
  check('wrong canonical model is rejected', wrongModel);
  let malformed = false;
  try { parseClaudeEnvelope(cleanEnvelope({ findings: [] })); } catch { malformed = true; }
  check('malformed result is rejected', malformed);
  check('sandbox visibility has its own class',
    classifyClaudeStderr('Operation not permitted in sandbox') === 'sandbox-visibility');
  check('host authentication remains distinct',
    classifyClaudeStderr('Please log in') === 'host-auth');
  const help = REQUIRED_HELP_FLAGS.join('\n');
  const valid = await runClaudeReview({
    prompt: 'fixture',
    resolve: () => '/fixture/claude',
    run: async (command, args) => args.includes('--help')
      ? { stdout: help }
      : { stdout: cleanEnvelope() },
  });
  check('bare review compatibility is preserved', validateFindings(valid));
  const recorded = await runClaudeReview({
    prompt: 'fixture',
    record: true,
    substitutionFor: 'mistral-vibe',
    resolve: () => '/fixture/claude',
    run: async (command, args) => args.includes('--help')
      ? { stdout: help }
      : { stdout: cleanEnvelope() },
  });
  check('record proves Sonnet substitution and medium effort command',
    recorded.resolved_model === CLAUDE_CANONICAL_MODEL
      && recorded.substitution_for === 'mistral-vibe');
  let sandboxSafe = false;
  try {
    await runClaudeReview({
      prompt: 'fixture',
      resolve: () => '/fixture/claude',
      run: async (command, args) => {
        if (args.includes('--help')) return { stdout: help };
        const error = new Error('unsafe detail');
        error.status = 1;
        error.claudeClass = 'sandbox-visibility';
        throw error;
      },
    });
  } catch (error) {
    sandboxSafe = error.message === 'Claude sandbox visibility failed';
  }
  check('sandbox visibility is not mislabeled as host auth', sandboxSafe);
  let timeoutSafe = false;
  try {
    await runClaudeReview({
      prompt: 'fixture',
      resolve: () => '/fixture/claude',
      run: async (command, args) => {
        if (args.includes('--help')) return { stdout: help };
        const error = new Error('unsafe timeout');
        error.name = 'TimeoutError';
        throw error;
      },
    });
  } catch (error) {
    timeoutSafe = error.message === 'Claude child timed out';
  }
  check('timeout hides child output', timeoutSafe);
  let canarySafe = false;
  try {
    await runClaudeReview({
      prompt: 'fixture',
      resolve: () => '/fixture/claude',
      run: async (command, args) => {
        if (args.includes('--help')) return { stdout: help };
        const error = new Error('FERRY_SECRET_CANARY');
        error.status = 23;
        error.stdout = 'FERRY_SECRET_CANARY';
        error.stderr = 'FERRY_SECRET_CANARY';
        throw error;
      },
    });
  } catch (error) {
    canarySafe = error.stage === 'claude-child'
      && !error.message.includes('FERRY_SECRET_CANARY');
  }
  check('canary reaches safe child boundary', canarySafe);

  const failed = checks.filter((item) => !item.ok);
  for (const item of checks) {
    process.stderr.write(`  ${item.ok ? 'ok  ' : 'FAIL'} ${item.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('claude-review canary and sandbox visibility paths: exercised\n');
  process.stderr.write('claude-review self-test: all checks passed\n');
}

function parseArgs(argv) {
  const args = {
    selfTest: false,
    record: false,
    mode: 'whole-branch',
    title: '',
    focus: '',
    slot: 'plan-sonnet',
    substitutionFor: null,
    substitutionReason: null,
  };
  const values = new Map([
    ['--mode', 'mode'],
    ['--title', 'title'],
    ['--focus', 'focus'],
    ['--slot', 'slot'],
    ['--substitution-for', 'substitutionFor'],
    ['--substitution-reason', 'substitutionReason'],
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--self-test') args.selfTest = true;
    else if (argument === '--record') args.record = true;
    else if (values.has(argument)) args[values.get(argument)] = argv[++index] ?? '';
    else throw new Error(`unknown argument: ${argument}`);
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) {
    process.stdout.write(JSON.stringify({ findings: [], summary: 'empty diff', confidence: 'high' }));
    return;
  }
  const prompt = `${buildReviewPrompt({
    mode: args.mode,
    title: args.title,
    focus: args.focus,
  })}\n\n${payload}`;
  const result = await runClaudeReview({
    prompt,
    record: args.record,
    slot: args.slot,
    substitutionFor: args.substitutionFor,
    substitutionReason: args.substitutionReason,
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
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
    process.stderr.write(`claude-review: ${error.message}\n`);
    process.exitCode = 1;
  });
}
