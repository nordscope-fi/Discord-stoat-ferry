#!/usr/bin/env node

import { spawn } from 'node:child_process';
import {
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readReviewerField } from './proton-credential.mjs';
import {
  buildReviewPrompt,
  makeReviewRecord,
  parseJsonText,
  safeChildFailure,
  validateFindings,
} from './review-contract.mjs';

export const VIBE_MODEL = 'zai-glm-5-2';
export const VIBE_CONFIG = `active_model = "zai-glm-5-2"

[[providers]]
name = "mistral-eu"
api_base = "https://api.mistral.ai/v1"
api_key_env_var = "MISTRAL_API_KEY"
backend = "mistral"

[[models]]
name = "zai-glm-5-2"
provider = "mistral-eu"
alias = "zai-glm-5-2"
temperature = 1.0
thinking = "max"
supports_images = false
`;

export const VIBE_REVIEW_ARGS = [
  '--prompt',
  '--max-turns', '1',
  '--max-tokens', '12000',
  '--enabled-tools', '__none__',
  '--disabled-tools', 're:.*',
  '--output', 'json',
  '--trust',
];

const REQUIRED_HELP_FLAGS = [
  '--prompt',
  '--max-turns',
  '--max-tokens',
  '--enabled-tools',
  '--disabled-tools',
  '--output',
  '--trust',
];

function stopProcessGroup(child, signal) {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch {
    // The process may already have exited.
  }
}

export function runVibeChild(command, args, { env, input = null, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      env,
      stdio: [input === null ? 'ignore' : 'pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let timedOut = false;
    let spawnError = null;
    let killTimer = null;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
      if (stdout.length > 12_000_000) stopProcessGroup(child, 'SIGTERM');
    });
    child.stderr.resume();
    child.once('error', (error) => { spawnError = error; });
    if (input !== null) child.stdin.end(input);
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
        const error = new Error('Vibe timed out');
        error.name = 'TimeoutError';
        reject(error);
        return;
      }
      if (status !== 0) {
        const error = new Error('Vibe child failed');
        error.status = status;
        error.signal = signal;
        reject(error);
        return;
      }
      resolve({ stdout });
    });
  });
}

export function requireVibeHelp(stdout) {
  const missing = REQUIRED_HELP_FLAGS.filter((flag) => !stdout.includes(flag));
  if (missing.length) throw new Error(`Vibe client is missing required flag ${missing[0]}`);
}

function contentBlocks(entry) {
  const value = entry?.message?.content ?? entry?.content;
  return Array.isArray(value) ? value : [];
}

function roleOf(entry) {
  return entry?.message?.role ?? entry?.role;
}

export function parseVibeHistory(raw) {
  const envelope = parseJsonText(raw, 'Vibe history');
  const history = Array.isArray(envelope) ? envelope : envelope?.history;
  if (!Array.isArray(history)) throw new Error('Vibe returned an invalid history envelope');
  for (const entry of history) {
    const toolBlocks = contentBlocks(entry).filter((block) =>
      ['tool_call', 'tool_use', 'function_call'].includes(block?.type));
    if (toolBlocks.length || entry?.tool_calls?.length || entry?.message?.tool_calls?.length) {
      throw new Error('Vibe history contains a tool call');
    }
    const reportedModel = entry?.model ?? entry?.message?.model;
    if (reportedModel && reportedModel !== VIBE_MODEL) {
      throw new Error('Vibe history reports the wrong model');
    }
  }
  const assistant = [...history].reverse().find((entry) => roleOf(entry) === 'assistant');
  if (!assistant) throw new Error('Vibe history has no assistant message');
  const text = contentBlocks(assistant)
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n');
  if (!text) throw new Error('Vibe assistant message has no text');
  const sessionId = history.find((entry) => typeof entry?.session_id === 'string')?.session_id
    ?? envelope?.session_id
    ?? null;
  return { history, text, sessionId };
}

function childFailure(error) {
  const wrapped = new Error(safeChildFailure('vibe', error));
  wrapped.stage = 'vibe-child';
  return wrapped;
}

async function withVibeInvocation({ home, credential, run, action }) {
  const vibeHome = mkdtempSync(join(tmpdir(), 'ferry-vibe-review-'));
  try {
    writeFileSync(join(vibeHome, 'config.toml'), VIBE_CONFIG, { mode: 0o600 });
    const baseEnvironment = {
      PATH: process.env.PATH ?? '',
      VIBE_HOME: vibeHome,
      VIBE_ACTIVE_MODEL: VIBE_MODEL,
      MISTRAL_API_KEY: '',
    };
    let help;
    try {
      help = await run('vibe', ['--help'], {
        env: baseEnvironment,
        timeoutMs: 30000,
      });
    } catch (error) {
      throw childFailure(error);
    }
    requireVibeHelp(help.stdout);
    const apiKey = await credential({
      provider: 'vibe',
      reason: 'Review Discord Ferry code with the fixed Vibe slot',
      home,
    });
    return await action({
      env: { ...baseEnvironment, MISTRAL_API_KEY: apiKey },
      run,
    });
  } finally {
    rmSync(vibeHome, { recursive: true, force: true });
  }
}

export async function runVibeReview({
  prompt,
  home,
  slot = 'mistral-vibe',
  credential = readReviewerField,
  run = runVibeChild,
}) {
  return withVibeInvocation({
    home,
    credential,
    run,
    action: async ({ env, run: invoke }) => {
      const started = Date.now();
      let result;
      try {
        result = await invoke('vibe', VIBE_REVIEW_ARGS, {
          env,
          input: prompt,
          timeoutMs: 180000,
        });
      } catch (error) {
        throw childFailure(error);
      }
      const parsed = parseVibeHistory(result.stdout);
      const review = parseJsonText(
        parsed.text.trim().replace(/^```(?:json)?\s*/u, '').replace(/\s*```$/u, ''),
        'Vibe review',
      );
      if (!validateFindings(review)) throw new Error('Vibe returned invalid findings');
      return makeReviewRecord({
        adapter: 'vibe',
        slot,
        requestedModel: VIBE_MODEL,
        resolvedModel: VIBE_MODEL,
        sessionId: parsed.sessionId,
        durationMs: Date.now() - started,
        status: 'valid',
        result: review,
      });
    },
  });
}

function directorySnapshot(root) {
  return JSON.stringify(readdirSync(root).sort().map((name) => [
    name,
    readFileSync(join(root, name)).toString('base64'),
  ]));
}

async function liveToolProbe(home) {
  const probeDirectory = mkdtempSync(join(tmpdir(), 'ferry-vibe-no-tools-'));
  try {
    const canaryPath = join(probeDirectory, 'DO_NOT_READ.txt');
    writeFileSync(canaryPath, 'FERRY_VIBE_FILE_CANARY\n', { mode: 0o600 });
    const before = directorySnapshot(probeDirectory);
    const parsed = await withVibeInvocation({
      home,
      credential: readReviewerField,
      run: runVibeChild,
      action: async ({ env, run }) => {
        let result;
        try {
          result = await run('vibe', VIBE_REVIEW_ARGS, {
            env,
            input: `Do not use tools. Reply exactly FERRY_VIBE_NO_TOOLS. The inaccessible canary is ${canaryPath}.`,
            timeoutMs: 180000,
          });
        } catch (error) {
          throw childFailure(error);
        }
        return parseVibeHistory(result.stdout);
      },
    });
    if (!parsed.text.includes('FERRY_VIBE_NO_TOOLS')) {
      throw new Error('Vibe no-tool marker was missing');
    }
    if (before !== directorySnapshot(probeDirectory)) {
      throw new Error('Vibe no-tool probe changed the canary directory');
    }
  } finally {
    rmSync(probeDirectory, { recursive: true, force: true });
  }
  process.stderr.write('vibe-review live tool probe: FERRY_VIBE_NO_TOOLS\n');
}

async function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  const clean = { findings: [], summary: 'clean', confidence: 'high' };
  const validHistory = JSON.stringify([{
    session_id: 'session',
    message: {
      role: 'assistant',
      content: [{ type: 'text', text: JSON.stringify(clean) }],
    },
  }]);
  record('model is zai-glm-5-2', VIBE_MODEL === 'zai-glm-5-2');
  record(
    'argv has --enabled-tools __none__',
    VIBE_REVIEW_ARGS.join(' ').includes('--enabled-tools __none__'),
  );
  record(
    'argv has --disabled-tools re:.*',
    VIBE_REVIEW_ARGS.join(' ').includes('--disabled-tools re:.*'),
  );
  record('history accepts valid review', parseVibeHistory(validHistory).sessionId === 'session');
  let wrongEnvelope = false;
  try { parseVibeHistory('{}'); } catch { wrongEnvelope = true; }
  record('history rejects wrong envelope', wrongEnvelope);
  let wrongModel = false;
  try {
    parseVibeHistory(JSON.stringify([{
      model: 'other-model',
      message: {
        role: 'assistant',
        content: [{ type: 'text', text: JSON.stringify(clean) }],
      },
    }]));
  } catch {
    wrongModel = true;
  }
  record('history rejects wrong model', wrongModel);
  let toolCall = false;
  try {
    parseVibeHistory(JSON.stringify([{
      message: { role: 'assistant', content: [{ type: 'tool_call', name: 'read_file' }] },
    }]));
  } catch (error) {
    toolCall = error.message.includes('tool call');
  }
  record('history rejects tool call', toolCall);
  let missingHelp = false;
  try { requireVibeHelp('--prompt'); } catch { missingHelp = true; }
  record('preflight rejects missing help flag', missingHelp);
  let credentialReached = false;
  try {
    await runVibeReview({
      prompt: 'fixture',
      home: tmpdir(),
      credential: async () => {
        credentialReached = true;
        return 'fixture-key';
      },
      run: async () => ({ stdout: '--prompt' }),
    });
  } catch {
    // Missing help is the expected result.
  }
  record('missing help blocks before credential', credentialReached === false);
  const fakeRun = async (command, args) => {
    if (args.includes('--help')) return { stdout: REQUIRED_HELP_FLAGS.join('\n') };
    return { stdout: validHistory };
  };
  const result = await runVibeReview({
    prompt: 'fixture',
    home: tmpdir(),
    credential: async () => 'fixture-key',
    run: fakeRun,
  });
  record('review returns valid fixed-model record',
    result.status === 'valid' && result.resolved_model === VIBE_MODEL);
  let malformedFindings = false;
  try {
    await runVibeReview({
      prompt: 'fixture',
      home: tmpdir(),
      credential: async () => 'fixture-key',
      run: async (command, args) => args.includes('--help')
        ? { stdout: REQUIRED_HELP_FLAGS.join('\n') }
        : { stdout: JSON.stringify([{
            message: {
              role: 'assistant',
              content: [{ type: 'text', text: '{"findings":[]}' }],
            },
          }]) },
    });
  } catch {
    malformedFindings = true;
  }
  record('review rejects malformed findings', malformedFindings);
  let timeoutSafe = false;
  try {
    await runVibeReview({
      prompt: 'fixture',
      home: tmpdir(),
      credential: async () => 'fixture-key',
      run: async (command, args) => {
        if (args.includes('--help')) return { stdout: REQUIRED_HELP_FLAGS.join('\n') };
        const error = new Error('child detail');
        error.name = 'TimeoutError';
        throw error;
      },
    });
  } catch (error) {
    timeoutSafe = error.stage === 'vibe-child' && error.message === 'vibe timed out';
  }
  record('review reports timeout without child output', timeoutSafe);
  let canarySafe = false;
  try {
    await runVibeReview({
      prompt: 'fixture',
      home: tmpdir(),
      credential: async () => 'fixture-key',
      run: async (command, args) => {
        if (args.includes('--help')) return { stdout: REQUIRED_HELP_FLAGS.join('\n') };
        const error = new Error('FERRY_SECRET_CANARY');
        error.status = 23;
        error.stdout = 'FERRY_SECRET_CANARY';
        error.stderr = 'FERRY_SECRET_CANARY';
        throw error;
      },
    });
  } catch (error) {
    canarySafe = error.stage === 'vibe-child' && !error.message.includes('FERRY_SECRET_CANARY');
  }
  record('canary reaches safe child boundary', canarySafe);

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('vibe-review canary child boundary: exercised\n');
  process.stderr.write('vibe-review self-test: all checks passed\n');
}

function parseArgs(argv) {
  const args = {
    selfTest: false,
    liveToolProbe: false,
    mode: 'chunk',
    title: '',
    focus: '',
    slot: 'mistral-vibe',
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--self-test') args.selfTest = true;
    else if (argument === '--live-tool-probe') args.liveToolProbe = true;
    else if (['--mode', '--title', '--focus', '--slot'].includes(argument)) {
      args[argument.slice(2).replace(/-([a-z])/gu, (_, letter) => letter.toUpperCase())]
        = argv[++index] ?? '';
    } else throw new Error(`unknown argument: ${argument}`);
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  if (args.liveToolProbe) return liveToolProbe(process.env.HOME);
  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) throw new Error('vibe-review requires a review payload on stdin');
  const prompt = `${buildReviewPrompt({
    mode: args.mode,
    title: args.title,
    focus: args.focus,
  })}\n\n${payload}`;
  const record = await runVibeReview({ prompt, home: process.env.HOME, slot: args.slot });
  process.stdout.write(`${JSON.stringify(record)}\n`);
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
    process.stderr.write(`vibe-review: ${error.message}\n`);
    process.exitCode = 1;
  });
}
