#!/usr/bin/env node

import { readFileSync, realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { readReviewerField } from './proton-credential.mjs';
import {
  buildReviewPrompt,
  makeReviewRecord,
  parseJsonText,
  validateFindings,
} from './review-contract.mjs';

export const QWEN_MODEL = 'qwen3.8-max';
export const QWEN_URL =
  'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions';

export async function requestQwen({
  apiKey,
  prompt,
  timeoutMs = 240000,
  fetcher = fetch,
}) {
  let response;
  try {
    response = await fetcher(QWEN_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: QWEN_MODEL,
        messages: [
          { role: 'system', content: 'Return only the requested review JSON. Do not call tools.' },
          { role: 'user', content: prompt },
        ],
      }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const wrapped = new Error('Qwen request failed');
    wrapped.code = error?.name === 'TimeoutError' ? 'ETIMEDOUT' : 'REQUEST_FAILED';
    throw wrapped;
  }
  if (!response.ok) {
    const error = new Error(`Qwen request failed with HTTP ${response.status}`);
    error.httpStatus = response.status;
    throw error;
  }
  try {
    return await response.json();
  } catch {
    throw new Error('Qwen response was not JSON');
  }
}

export function parseQwenResponse(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('Qwen response envelope mismatch');
  }
  if (body.model !== QWEN_MODEL) throw new Error('Qwen response model mismatch');
  const text = body.choices?.[0]?.message?.content;
  if (typeof text !== 'string') throw new Error('Qwen response has no assistant text');
  const result = parseJsonText(text, 'Qwen response');
  if (!validateFindings(result)) throw new Error('Qwen findings schema mismatch');
  return { result, sessionId: typeof body.id === 'string' ? body.id : null };
}

function responseFailure(error) {
  const safeMessage = typeof error?.httpStatus === 'number'
    ? `Qwen request failed with HTTP ${error.httpStatus}`
    : error?.code === 'ETIMEDOUT'
      ? 'Qwen request timed out'
      : 'Qwen response was invalid';
  const wrapped = new Error(safeMessage);
  wrapped.stage = 'qwen-response';
  return wrapped;
}

export async function runQwenReview({
  prompt,
  home,
  slot = 'qwen',
  credential = readReviewerField,
  request = requestQwen,
}) {
  const apiKey = await credential({
    itemTitle: 'QwenCloud API Key',
    reason: 'Review Discord Ferry code with the fixed Qwen slot',
    home,
  });
  const started = Date.now();
  let body;
  try {
    body = await request({ apiKey, prompt, timeoutMs: 240000 });
  } catch (error) {
    throw responseFailure(error);
  }
  let parsed;
  try {
    parsed = parseQwenResponse(body);
  } catch (error) {
    throw responseFailure(error);
  }
  return makeReviewRecord({
    adapter: 'qwen',
    slot,
    requestedModel: QWEN_MODEL,
    resolvedModel: QWEN_MODEL,
    sessionId: parsed.sessionId,
    durationMs: Date.now() - started,
    status: 'valid',
    result: parsed.result,
  });
}

function cleanBody(text = JSON.stringify({
  findings: [],
  summary: 'clean',
  confidence: 'high',
})) {
  return {
    id: 'fixture-review',
    model: QWEN_MODEL,
    choices: [{ message: { role: 'assistant', content: text } }],
  };
}

async function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  record('model is qwen3.8-max', QWEN_MODEL === 'qwen3.8-max');
  record('endpoint contains /chat/completions', QWEN_URL.endsWith('/chat/completions'));
  record('valid response returns exact model',
    parseQwenResponse(cleanBody()).result.confidence === 'high');
  let wrongModel = false;
  try { parseQwenResponse({ ...cleanBody(), model: 'other' }); } catch { wrongModel = true; }
  record('wrong model is rejected', wrongModel);
  let missingText = false;
  try { parseQwenResponse({ id: 'x', model: QWEN_MODEL, choices: [] }); } catch {
    missingText = true;
  }
  record('missing assistant text is rejected', missingText);
  let malformedJson = false;
  try { parseQwenResponse(cleanBody('{')); } catch { malformedJson = true; }
  record('malformed findings JSON is rejected', malformedJson);

  const validRecord = await runQwenReview({
    prompt: 'fixture',
    home: '/',
    credential: async () => 'fixture-key',
    request: async () => cleanBody(),
  });
  record('review returns valid fixed-model record',
    validRecord.status === 'valid' && validRecord.resolved_model === QWEN_MODEL);

  let httpSafe = false;
  try {
    await runQwenReview({
      prompt: 'fixture',
      home: '/',
      credential: async () => 'fixture-key',
      request: async () => {
        const error = new Error('unsafe body');
        error.httpStatus = 429;
        throw error;
      },
    });
  } catch (error) {
    httpSafe = error.message === 'Qwen request failed with HTTP 429';
  }
  record('HTTP failure reports status only', httpSafe);

  let timeoutSafe = false;
  try {
    await runQwenReview({
      prompt: 'fixture',
      home: '/',
      credential: async () => 'fixture-key',
      request: async () => {
        const error = new Error('unsafe timeout detail');
        error.code = 'ETIMEDOUT';
        throw error;
      },
    });
  } catch (error) {
    timeoutSafe = error.message === 'Qwen request timed out';
  }
  record('timeout hides request detail', timeoutSafe);

  let responseCanarySafe = false;
  try {
    await runQwenReview({
      prompt: 'fixture',
      home: '/',
      credential: async () => 'fixture-key',
      request: async () => cleanBody('FERRY_SECRET_CANARY'),
    });
  } catch (error) {
    responseCanarySafe = error.stage === 'qwen-response'
      && !error.message.includes('FERRY_SECRET_CANARY');
  }
  record('response canary reaches safe boundary', responseCanarySafe);

  let errorCanarySafe = false;
  try {
    await runQwenReview({
      prompt: 'fixture',
      home: '/',
      credential: async () => 'fixture-key',
      request: async () => {
        const error = new Error('FERRY_SECRET_CANARY');
        error.responseBody = 'FERRY_SECRET_CANARY';
        throw error;
      },
    });
  } catch (error) {
    errorCanarySafe = error.stage === 'qwen-response'
      && !error.message.includes('FERRY_SECRET_CANARY');
  }
  record('error canary reaches safe boundary', errorCanarySafe);

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('qwen-review canary response and error paths: exercised\n');
  process.stderr.write(`qwen-review self-test: all checks passed (${QWEN_MODEL}, ${QWEN_URL})\n`);
}

async function liveProbe(home) {
  const prompt = `Return exactly this review JSON: ${JSON.stringify({
    findings: [],
    summary: 'live Qwen reviewer ready',
    confidence: 'high',
  })}`;
  const record = await runQwenReview({ prompt, home });
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

function parseArgs(argv) {
  const args = {
    selfTest: false,
    liveProbe: false,
    mode: 'chunk',
    title: '',
    focus: '',
    slot: 'qwen',
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--self-test') args.selfTest = true;
    else if (argument === '--live-probe') args.liveProbe = true;
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
  if (args.liveProbe) return liveProbe(process.env.HOME);
  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) throw new Error('qwen-review requires a review payload on stdin');
  const prompt = `${buildReviewPrompt({
    mode: args.mode,
    title: args.title,
    focus: args.focus,
  })}\n\n${payload}`;
  const record = await runQwenReview({ prompt, home: process.env.HOME, slot: args.slot });
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
    process.stderr.write(`qwen-review: ${error.message}\n`);
    process.exitCode = 1;
  });
}
