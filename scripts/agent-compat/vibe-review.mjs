#!/usr/bin/env node

import { readFileSync, realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { readReviewerField } from './proton-credential.mjs';
import {
  buildReviewPrompt,
  classifyReviewFailure,
  makeReviewRecord,
  parseJsonText,
  reviewReplyJson,
  validateFindings,
} from './review-contract.mjs';

export const VIBE_MODEL = 'zai-glm-5-2';
export const VIBE_URL = 'https://api.mistral.ai/v1/chat/completions';
export const VIBE_TEMPERATURE = 1.0;
export const VIBE_MAX_TOKENS = 12000;
// The mistral-vibe client turns its `thinking = "max"` model setting into
// reasoning_effort "high" (vibe/core/llm/backend/mistral.py, the
// _THINKING_TO_REASONING_EFFORT table), so the direct call sends the same value.
export const VIBE_REASONING_EFFORT = 'high';
export const VIBE_TIMEOUT_MS = 180000;

// Fixed reasons, so an error message never repeats text the provider sent.
const SCHEMA_FAILURE_REASONS = new Set([
  'response-body',
  'response-envelope',
  'response-tool-call',
  'response-finish-reason',
  'response-content',
  'response-json',
  'response-findings',
]);

export function vibeRequestBody(prompt) {
  return {
    model: VIBE_MODEL,
    messages: [
      { role: 'system', content: 'Return only the requested review JSON. Do not call tools.' },
      { role: 'user', content: prompt },
    ],
    temperature: VIBE_TEMPERATURE,
    max_tokens: VIBE_MAX_TOKENS,
    reasoning_effort: VIBE_REASONING_EFFORT,
  };
}

function reviewError(code, failureReason = null, message = 'Vibe response was invalid') {
  const error = new Error(message);
  error.code = code;
  error.failureReason = failureReason;
  return error;
}

function timeoutError() {
  const error = new Error('Vibe request timed out');
  error.code = 'ETIMEDOUT';
  return error;
}

export async function requestVibe({
  apiKey,
  prompt,
  timeoutMs = VIBE_TIMEOUT_MS,
  fetcher = fetch,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_resolve, reject) => {
    timer = schedule(() => {
      const error = timeoutError();
      controller.abort(error);
      reject(error);
    }, timeoutMs);
  });
  const exchange = (async () => {
    let response;
    try {
      response = await fetcher(VIBE_URL, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(vibeRequestBody(prompt)),
        signal: controller.signal,
      });
    } catch (error) {
      if (error?.code === 'ETIMEDOUT') throw error;
      const wrapped = new Error('Vibe request failed');
      wrapped.code = error?.name === 'TimeoutError' ? 'ETIMEDOUT' : 'REQUEST_FAILED';
      throw wrapped;
    }
    if (!response.ok) {
      const error = new Error(`Vibe request failed with HTTP ${response.status}`);
      error.httpStatus = response.status;
      throw error;
    }
    try {
      return await response.json();
    } catch (error) {
      if (error?.code === 'ETIMEDOUT') throw error;
      throw reviewError('INVALID_SCHEMA', 'response-body');
    }
  })();
  try {
    return await Promise.race([exchange, deadline]);
  } finally {
    cancel(timer);
  }
}

function replyText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return null;
  // A Mistral reasoning model may return its thinking as a chunk ahead of the text.
  if (content.some((chunk) => !['text', 'thinking'].includes(chunk?.type))) return null;
  return content
    .filter((chunk) => chunk.type === 'text' && typeof chunk.text === 'string')
    .map((chunk) => chunk.text)
    .join('');
}

export function parseVibeResponse(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw reviewError('INVALID_SCHEMA', 'response-envelope');
  }
  // The model the provider reports is the only proof of what answered, so a
  // missing or different name fails the review (#990).
  if (body.model !== VIBE_MODEL) throw reviewError('WRONG_MODEL');
  const choice = body.choices?.[0];
  const message = choice?.message;
  if (!message || typeof message !== 'object') {
    throw reviewError('INVALID_SCHEMA', 'response-envelope');
  }
  const calls = message.tool_calls;
  const hasToolCalls = calls !== undefined && calls !== null
    && !(Array.isArray(calls) && calls.length === 0);
  if (hasToolCalls || choice.finish_reason === 'tool_calls') {
    throw reviewError('INVALID_SCHEMA', 'response-tool-call');
  }
  if (choice.finish_reason !== 'stop') {
    throw reviewError('INVALID_SCHEMA', 'response-finish-reason');
  }
  const text = replyText(message.content);
  if (!text) throw reviewError('INVALID_SCHEMA', 'response-content');
  let result;
  try {
    result = parseJsonText(reviewReplyJson(text), 'Vibe review');
  } catch {
    throw reviewError('INVALID_SCHEMA', 'response-json');
  }
  if (!validateFindings(result)) throw reviewError('INVALID_SCHEMA', 'response-findings');
  return {
    result,
    resolvedModel: body.model,
    sessionId: typeof body.id === 'string' ? body.id : null,
  };
}

function responseFailure(error, { durationMs = 0, stage = 'vibe-response' } = {}) {
  const classification = classifyReviewFailure(error);
  const schemaReason = SCHEMA_FAILURE_REASONS.has(error?.failureReason)
    ? error.failureReason
    : 'response-unclassified';
  let safeMessage;
  if (typeof error?.httpStatus === 'number') {
    safeMessage = `Vibe request failed with HTTP ${error.httpStatus}`;
  } else if (classification === 'timeout') {
    safeMessage = 'Vibe request timed out';
  } else if (classification === 'wrong-model') {
    safeMessage = 'Vibe response did not name the expected model';
  } else if (stage === 'vibe-credential') {
    safeMessage = 'Vibe credential retrieval failed';
  } else if (classification === 'schema') {
    safeMessage = `Vibe response was invalid (${schemaReason})`;
  } else if (error?.code === 'REQUEST_FAILED') {
    safeMessage = 'Vibe request failed';
  } else {
    safeMessage = 'Vibe response was invalid';
  }
  const wrapped = new Error(safeMessage);
  wrapped.stage = stage;
  wrapped.durationMs = durationMs;
  wrapped.classification = classification;
  wrapped.httpStatus = Number.isInteger(error?.httpStatus) ? error.httpStatus : null;
  wrapped.failureReason = classification === 'schema' ? schemaReason : null;
  if (classification === 'timeout') wrapped.code = 'ETIMEDOUT';
  else if (classification === 'schema') wrapped.code = 'INVALID_SCHEMA';
  else if (classification === 'wrong-model') wrapped.code = 'WRONG_MODEL';
  else if (classification === 'credential') wrapped.code = 'CREDENTIAL';
  return wrapped;
}

export async function runVibeReview({
  prompt,
  home,
  slot = 'mistral-vibe',
  credential = readReviewerField,
  fetcher = fetch,
  timeoutMs = VIBE_TIMEOUT_MS,
  clock = Date,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  const started = clock.now();
  let stage = 'vibe-credential';
  try {
    const apiKey = await credential({
      provider: 'vibe',
      reason: 'Review Discord Ferry code with the fixed Vibe slot',
      home,
    });
    stage = 'vibe-response';
    const body = await requestVibe({ apiKey, prompt, timeoutMs, fetcher, schedule, cancel });
    const parsed = parseVibeResponse(body);
    return makeReviewRecord({
      adapter: 'vibe',
      slot,
      requestedModel: VIBE_MODEL,
      resolvedModel: parsed.resolvedModel,
      sessionId: parsed.sessionId,
      durationMs: clock.now() - started,
      status: 'valid',
      result: parsed.result,
    });
  } catch (error) {
    throw responseFailure(error, { durationMs: clock.now() - started, stage });
  }
}

function cleanBody(text = JSON.stringify({
  findings: [],
  summary: 'clean',
  confidence: 'high',
}), overrides = {}) {
  return {
    id: 'fixture-review',
    model: VIBE_MODEL,
    choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: text } }],
    ...overrides,
  };
}

function stubFetcher(body, init = {}) {
  return async () => new Response(JSON.stringify(body), { status: 200, ...init });
}

async function rejection(work) {
  try {
    await work();
  } catch (error) {
    return error;
  }
  return null;
}

async function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  const review = (fetcher, extra = {}) => runVibeReview({
    prompt: 'fixture',
    home: '/',
    credential: async () => 'fixture-key',
    fetcher,
    ...extra,
  });
  record('model is zai-glm-5-2', VIBE_MODEL === 'zai-glm-5-2');
  record('endpoint is the chat completions route', VIBE_URL.endsWith('/v1/chat/completions'));
  record('request sends the model, no tools and the effort the client sends',
    vibeRequestBody('x').model === VIBE_MODEL
      && !('tools' in vibeRequestBody('x'))
      && vibeRequestBody('x').reasoning_effort === 'high');
  record('valid response returns exact model',
    parseVibeResponse(cleanBody()).resolvedModel === VIBE_MODEL);

  const wrongModel = await rejection(async () => parseVibeResponse(cleanBody(undefined, { model: 'other' })));
  record('wrong model is rejected', wrongModel?.code === 'WRONG_MODEL');
  const noModel = await rejection(async () => parseVibeResponse(cleanBody(undefined, { model: undefined })));
  record('missing model is rejected', noModel?.code === 'WRONG_MODEL');
  const toolCall = await rejection(async () => parseVibeResponse(cleanBody(undefined, {
    choices: [{
      finish_reason: 'stop',
      message: { role: 'assistant', content: '{}', tool_calls: [{ id: 'call-1' }] },
    }],
  })));
  record('tool call is rejected', toolCall?.failureReason === 'response-tool-call');
  const malformed = await rejection(async () => parseVibeResponse(cleanBody('{')));
  record('malformed findings JSON is rejected', malformed?.failureReason === 'response-json');
  const missingText = await rejection(async () => parseVibeResponse({ id: 'x', model: VIBE_MODEL, choices: [] }));
  record('missing assistant text is rejected', missingText?.failureReason === 'response-envelope');

  const sent = [];
  const validRecord = await review(async (url, options) => {
    sent.push({ url, options });
    return stubFetcher(cleanBody())();
  });
  record('review returns the reported model and completion id',
    validRecord.status === 'valid'
      && validRecord.resolved_model === VIBE_MODEL
      && validRecord.session_id === 'fixture-review');
  record('key travels only in the Authorization header',
    sent[0].options.headers.Authorization === 'Bearer fixture-key'
      && !sent[0].options.body.includes('fixture-key'));

  const httpError = await rejection(() => review(async () => new Response('FERRY_SECRET_CANARY', { status: 429 })));
  record('HTTP failure reports status only',
    httpError?.message === 'Vibe request failed with HTTP 429');

  const timeout = await rejection(() => review(
    (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(options.signal.reason));
    }),
    { timeoutMs: 10 },
  ));
  record('timeout hides request detail', timeout?.message === 'Vibe request timed out');

  const canaryResponse = await rejection(() => review(stubFetcher(cleanBody('FERRY_SECRET_CANARY'))));
  record('response canary reaches safe boundary',
    canaryResponse?.stage === 'vibe-response' && !canaryResponse.message.includes('FERRY_SECRET_CANARY'));

  const canaryError = await rejection(() => review(async () => {
    throw new Error('FERRY_SECRET_CANARY');
  }));
  record('error canary reaches safe boundary',
    canaryError?.stage === 'vibe-response' && !canaryError.message.includes('FERRY_SECRET_CANARY'));

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('vibe-review canary response and error paths: exercised\n');
  process.stderr.write(`vibe-review self-test: all checks passed (${VIBE_MODEL}, ${VIBE_URL})\n`);
}

async function liveProbe(home) {
  const prompt = `Return exactly this review JSON: ${JSON.stringify({
    findings: [],
    summary: 'live Vibe reviewer ready',
    confidence: 'high',
  })}`;
  const record = await runVibeReview({ prompt, home });
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

function parseArgs(argv) {
  const args = {
    selfTest: false,
    liveProbe: false,
    mode: 'chunk',
    title: '',
    focus: '',
    slot: 'mistral-vibe',
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
