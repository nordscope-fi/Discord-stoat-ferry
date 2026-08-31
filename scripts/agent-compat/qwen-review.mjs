#!/usr/bin/env node

import { readFileSync, realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { readReviewerField } from './proton-credential.mjs';
import {
  buildReviewPrompt,
  classifyReviewFailure,
  FINDINGS_SCHEMA,
  isQwenSchemaFailureReason,
  makeReviewRecord,
  parseJsonText,
  QWEN_SCHEMA_FAILURE_REASONS,
  validateFindings,
} from './review-contract.mjs';

export { QWEN_SCHEMA_FAILURE_REASONS };

export const QWEN_MODEL = 'qwen3.8-max';
export const QWEN_URL =
  'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions';
export const QWEN_COMPLETION_TOKENS = 32768;
export const QWEN_DEADLINES = Object.freeze({
  connectionMs: 60000,
  idleMs: 30000,
  totalMs: 600000,
});
export function qwenRequestBody(prompt) {
  return {
    model: QWEN_MODEL,
    messages: [
      { role: 'system', content: 'Return only the requested review JSON. Do not call tools.' },
      { role: 'user', content: prompt },
    ],
    stream: true,
    enable_thinking: true,
    reasoning_effort: 'medium',
    max_completion_tokens: QWEN_COMPLETION_TOKENS,
    response_format: {
      type: 'json_schema',
      json_schema: {
        name: 'ferry_review',
        strict: true,
        schema: FINDINGS_SCHEMA,
      },
    },
  };
}

function reviewError(code, failureReason = null, message = 'Qwen response was invalid') {
  const error = new Error(message);
  error.code = code;
  error.failureReason = failureReason;
  return error;
}

function deadlineError(phase) {
  const error = new Error(`Qwen ${phase} timed out`);
  error.code = 'ETIMEDOUT';
  error.deadline = phase;
  return error;
}

function withDeadline(
  work,
  timeoutMs,
  phase,
  { schedule = setTimeout, cancel = clearTimeout, onTimeout = () => {} } = {},
) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = schedule(() => {
      if (settled) return;
      settled = true;
      const error = deadlineError(phase);
      onTimeout(error);
      reject(error);
    }, timeoutMs);
    Promise.resolve(work).then(
      (value) => {
        if (settled) return;
        settled = true;
        cancel(timer);
        resolve(value);
      },
      (error) => {
        if (settled) return;
        settled = true;
        cancel(timer);
        reject(error);
      },
    );
  });
}

function withAbort(work, signal, onAbort = () => {}) {
  if (!signal) return Promise.resolve(work);
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const aborted = () => {
      onAbort();
      reject(signal.reason);
    };
    signal.addEventListener('abort', aborted, { once: true });
    Promise.resolve(work).then(
      (value) => {
        signal.removeEventListener('abort', aborted);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener('abort', aborted);
        reject(error);
      },
    );
  });
}

function consumeQwenEvents(buffer) {
  const events = [];
  let remainder = buffer;
  for (;;) {
    const boundary = /\r?\n\r?\n/u.exec(remainder);
    if (!boundary) break;
    const block = remainder.slice(0, boundary.index);
    remainder = remainder.slice(boundary.index + boundary[0].length);
    const data = block
      .split(/\r?\n/u)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n');
    if (data) events.push(data);
  }
  return { events, remainder };
}

function parseQwenEvent(data) {
  try {
    const event = JSON.parse(data);
    if (!event || typeof event !== 'object' || Array.isArray(event)) {
      throw reviewError('INVALID_SCHEMA', 'stream-event');
    }
    return event;
  } catch (error) {
    if (error?.code === 'INVALID_SCHEMA') throw error;
    throw reviewError('INVALID_SCHEMA', 'stream-event');
  }
}

export async function readQwenStream(body, {
  signal = null,
  idleTimeoutMs = QWEN_DEADLINES.idleMs,
  schedule = setTimeout,
  cancel = clearTimeout,
} = {}) {
  if (!body || typeof body.getReader !== 'function') {
    throw reviewError('INVALID_SCHEMA', 'stream-body');
  }
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sessionId = null;
  let resolvedModel = null;
  let content = '';
  let finishReason = null;
  let completed = false;
  let idleTimer = null;
  let idleFailure = null;
  const refreshIdleDeadline = () => {
    if (idleTimer !== null) cancel(idleTimer);
    idleFailure = new Promise((resolve, reject) => {
      idleTimer = schedule(() => {
        const error = deadlineError('idle');
        reject(error);
        void reader.cancel();
      }, idleTimeoutMs);
    });
  };

  refreshIdleDeadline();
  try {
    while (!completed) {
      const read = Promise.race([reader.read(), idleFailure]);
      const chunk = await withAbort(read, signal, () => void reader.cancel());
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const parsed = consumeQwenEvents(buffer);
      buffer = parsed.remainder;
      if (parsed.events.length) refreshIdleDeadline();
      for (const data of parsed.events) {
        if (data === '[DONE]') {
          completed = true;
          break;
        }
        const event = parseQwenEvent(data);
        if (typeof event.model === 'string' && event.model !== QWEN_MODEL) {
          throw reviewError('WRONG_MODEL');
        }
        if (sessionId === null && typeof event.id === 'string') sessionId = event.id;
        if (resolvedModel === null && typeof event.model === 'string') {
          resolvedModel = event.model;
        }
        const choice = event.choices?.[0];
        if (typeof choice?.delta?.content === 'string') content += choice.delta.content;
        if (choice?.finish_reason !== null && choice?.finish_reason !== undefined) {
          finishReason = choice.finish_reason;
        }
      }
    }
  } finally {
    if (idleTimer !== null) cancel(idleTimer);
  }
  buffer += decoder.decode();
  if (!completed) throw reviewError('INVALID_SCHEMA', 'stream-completion');
  if (buffer.trim()) throw reviewError('INVALID_SCHEMA', 'stream-trailing-data');
  if (resolvedModel !== QWEN_MODEL) throw reviewError('INVALID_SCHEMA', 'stream-model');
  if (finishReason !== 'stop') {
    throw reviewError('INVALID_SCHEMA', 'stream-finish-reason');
  }
  if (!content) throw reviewError('INVALID_SCHEMA', 'stream-content');
  return {
    id: sessionId,
    model: resolvedModel,
    choices: [{ message: { role: 'assistant', content } }],
  };
}

export async function requestQwen({
  apiKey,
  prompt,
  signal = null,
  connectionTimeoutMs = QWEN_DEADLINES.connectionMs,
  idleTimeoutMs = QWEN_DEADLINES.idleMs,
  fetcher = fetch,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  let response;
  const connection = new AbortController();
  const requestSignal = signal
    ? AbortSignal.any([signal, connection.signal])
    : connection.signal;
  try {
    const fetched = fetcher(QWEN_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(qwenRequestBody(prompt)),
      signal: requestSignal,
    });
    response = await withDeadline(withAbort(fetched, signal), connectionTimeoutMs, 'connection', {
      schedule,
      cancel,
      onTimeout: (error) => connection.abort(error),
    });
  } catch (error) {
    if (error?.code === 'ETIMEDOUT') throw error;
    const wrapped = new Error('Qwen request failed');
    wrapped.code = error?.name === 'TimeoutError' ? 'ETIMEDOUT' : 'REQUEST_FAILED';
    throw wrapped;
  }
  if (!response.ok) {
    const error = new Error(`Qwen request failed with HTTP ${response.status}`);
    error.httpStatus = response.status;
    throw error;
  }
  return readQwenStream(response.body, { signal, idleTimeoutMs, schedule, cancel });
}

export function parseQwenResponse(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw reviewError('INVALID_SCHEMA', 'response-envelope');
  }
  if (body.model !== QWEN_MODEL) throw reviewError('WRONG_MODEL');
  const text = body.choices?.[0]?.message?.content;
  if (typeof text !== 'string') {
    throw reviewError('INVALID_SCHEMA', 'response-envelope');
  }
  let result;
  try {
    result = parseJsonText(text, 'Qwen response');
  } catch {
    throw reviewError('INVALID_SCHEMA', 'response-json');
  }
  if (!validateFindings(result)) {
    throw reviewError('INVALID_SCHEMA', 'response-findings');
  }
  return { result, sessionId: typeof body.id === 'string' ? body.id : null };
}

function responseFailure(error, { durationMs = 0, stage = 'qwen-response' } = {}) {
  const classification = classifyReviewFailure(error);
  const safeMessage = typeof error?.httpStatus === 'number'
    ? `Qwen request failed with HTTP ${error.httpStatus}`
    : classification === 'timeout'
      ? 'Qwen request timed out'
      : stage === 'qwen-credential'
        ? 'Qwen credential retrieval failed'
        : 'Qwen response was invalid';
  const wrapped = new Error(safeMessage);
  wrapped.stage = stage;
  wrapped.durationMs = durationMs;
  wrapped.classification = classification;
  wrapped.httpStatus = Number.isInteger(error?.httpStatus) ? error.httpStatus : null;
  wrapped.failureReason = classification === 'schema'
    ? isQwenSchemaFailureReason(error?.failureReason)
      ? error.failureReason
      : 'response-unclassified'
    : null;
  if (classification === 'timeout') wrapped.code = 'ETIMEDOUT';
  else if (classification === 'schema') wrapped.code = 'INVALID_SCHEMA';
  else if (classification === 'wrong-model') wrapped.code = 'WRONG_MODEL';
  else if (classification === 'credential') wrapped.code = 'CREDENTIAL';
  if (classification === 'timeout' &&
      ['connection', 'idle', 'total'].includes(error?.deadline)) {
    wrapped.deadline = error.deadline;
  }
  return wrapped;
}

export async function runQwenReview({
  prompt,
  home,
  slot = 'qwen',
  credential = readReviewerField,
  request = requestQwen,
  deadlines = QWEN_DEADLINES,
  clock = Date,
  schedule = setTimeout,
  cancel = clearTimeout,
}) {
  const started = clock.now();
  const total = new AbortController();
  let stage = 'qwen-credential';
  try {
    const operation = (async () => {
      const apiKey = await credential({
        itemTitle: 'QwenCloud API Key',
        reason: 'Review Discord Ferry code with the fixed Qwen slot',
        home,
      });
      stage = 'qwen-response';
      const body = await request({
        apiKey,
        prompt,
        signal: total.signal,
        connectionTimeoutMs: deadlines.connectionMs,
        idleTimeoutMs: deadlines.idleMs,
        schedule,
        cancel,
      });
      const parsed = parseQwenResponse(body);
      return makeReviewRecord({
        adapter: 'qwen',
        slot,
        requestedModel: QWEN_MODEL,
        resolvedModel: QWEN_MODEL,
        sessionId: parsed.sessionId,
        durationMs: clock.now() - started,
        status: 'valid',
        result: parsed.result,
      });
    })();
    return await withDeadline(operation, deadlines.totalMs, 'total', {
      schedule,
      cancel,
      onTimeout: (error) => total.abort(error),
    });
  } catch (error) {
    throw responseFailure(error, { durationMs: clock.now() - started, stage });
  }
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
