#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['critical', 'important', 'minor'] },
          category: {
            type: 'string',
            enum: ['security', 'correctness', 'performance', 'maintainability'],
          },
          file: { type: 'string' },
          line: { type: ['integer', 'null'] },
          description: { type: 'string' },
          suggestion: { type: 'string' },
          verification: {
            type: 'object',
            additionalProperties: false,
            properties: {
              command: { type: 'string' },
              confirms_if: { type: 'string' },
              refutes_if: { type: 'string' },
            },
            required: ['command', 'confirms_if', 'refutes_if'],
          },
        },
        required: [
          'severity',
          'category',
          'file',
          'line',
          'description',
          'suggestion',
          'verification',
        ],
      },
    },
    summary: { type: 'string' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['findings', 'summary', 'confidence'],
};

export const PROJECT_CONTEXT = `Discord Ferry migrates a Discord server export to Stoat (a Revolt fork). Python 3.11+, aiohttp,
Click, Rich, NiceGUI, pytest.

Conventions that matter for review:
- core/engine.py never imports gui.py or cli.py. Everything crossing that boundary is an event.
- Every public function is fully typed. mypy runs in strict mode.
- Tokens never reach logs, errors, state files or report.json. Redaction lives in the logging
  Formatter, not a Filter.
- Ferry is single-user and single-process. Findings that assume concurrent multi-user access
  are false positives here.
- Stoat rate limits: 5 requests per 10 seconds on /servers, 15 per 10 seconds per channel.
  X-RateLimit-Reset-After is milliseconds, unlike Discord's identically named header.
- Every finding must carry a verification command that would actually distinguish the finding
  being true from it being false. It will be run.`;

const REVIEW_DIRECTIVES = `Focus on the diff and use any full files provided for context. Report only what you are
confident about. Return an empty findings array if the diff is clean. Every finding must carry a
verification command that distinguishes the finding being true from being false; the command will
be run by the caller, so you never need to run it yourself.`;

const DESIGN_DIRECTIVES = `Review the design or plan document. Judge feasibility, fit with the project context above,
missing edge cases, and whether the tasks are atomic enough to plan and verify on their own.
Report only what you are confident about, and return an empty findings array when the design is
sound. Set file to the document path or the section name. Every finding must carry a verification
command that distinguishes the finding being true from being false; the command will be run by the
caller, so you never need to run it yourself.`;

const OUTPUT_DIRECTIVES = `Return only one JSON object with exactly these top-level keys: findings, summary, and
confidence. confidence is high, medium, or low. findings is an array. Every finding has severity,
category, file, line, description, suggestion, and verification. severity is critical, important,
or minor. category is security, correctness, performance, or maintainability. line is an integer or
null. verification has command, confirms_if, and refutes_if. Use an empty findings array for a clean
review, but always include summary and confidence.`;

const VALID_SEVERITIES = new Set(FINDINGS_SCHEMA.properties.findings.items.properties.severity.enum);
const VALID_CATEGORIES = new Set(FINDINGS_SCHEMA.properties.findings.items.properties.category.enum);
const VALID_CONFIDENCES = new Set(FINDINGS_SCHEMA.properties.confidence.enum);
const VALID_STATUSES = new Set(['valid', 'failed', 'timed_out']);
const SUBSTITUTION_REASONS = new Set([
  'credential',
  'timeout',
  'schema',
  'wrong-model',
  'child-exit',
  'unknown',
]);
const FAILURE_CLASSES = new Set([
  ...SUBSTITUTION_REASONS,
  'rate-limit',
  'provider',
  'request',
]);
const PLAN_MODELS = new Map([
  ['plan-qwen', 'qwen3.8-max'],
  ['plan-opus', 'claude-opus-5'],
]);
const PLAN_REQUESTED_MODELS = new Map([
  ['plan-qwen', 'qwen3.8-max'],
  ['plan-opus', 'opus'],
]);
const PLAN_SLOTS = new Map([
  ['qwen', 'plan-qwen'],
  ['opus', 'plan-opus'],
]);
const FINDING_KEYS = new Set([
  'severity',
  'category',
  'file',
  'line',
  'description',
  'suggestion',
  'verification',
]);
const VERIFICATION_KEYS = new Set(['command', 'confirms_if', 'refutes_if']);
const RESULT_KEYS = new Set(['findings', 'summary', 'confidence']);

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

export function validateFindings(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false;
  if (!hasExactKeys(result, RESULT_KEYS)) return false;
  if (typeof result.summary !== 'string' || !VALID_CONFIDENCES.has(result.confidence)) return false;
  if (!Array.isArray(result.findings)) return false;
  for (const finding of result.findings) {
    if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return false;
    if (!hasExactKeys(finding, FINDING_KEYS)) return false;
    if (!VALID_SEVERITIES.has(finding.severity)) return false;
    if (!VALID_CATEGORIES.has(finding.category)) return false;
    for (const key of ['file', 'description', 'suggestion']) {
      if (typeof finding[key] !== 'string') return false;
    }
    if (finding.line !== null && !Number.isInteger(finding.line)) return false;
    const verification = finding.verification;
    if (!verification || typeof verification !== 'object' || Array.isArray(verification)) {
      return false;
    }
    if (!hasExactKeys(verification, VERIFICATION_KEYS)) return false;
    for (const key of VERIFICATION_KEYS) {
      if (typeof verification[key] !== 'string' || !verification[key].trim()) return false;
    }
  }
  return true;
}

export function parseJsonText(raw, adapter = 'reviewer') {
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error(`${adapter} returned invalid JSON`);
  }
}

export function buildReviewPrompt({
  mode = 'whole-branch',
  title = '',
  focus = '',
  payloadLabel = null,
} = {}) {
  const labels = { chunk: 'chunk review', design: 'design review', plan: 'plan review' };
  const label = labels[mode] ?? 'whole-branch review';
  const titleLine = title ? `\nUnder review: ${title}` : '';
  const focusLine = focus ? `\nReview focus: ${focus}` : '';
  const documentReview = mode === 'design' || mode === 'plan';
  const directives = documentReview ? DESIGN_DIRECTIVES : REVIEW_DIRECTIVES;
  const payload = payloadLabel ?? (mode === 'design'
    ? 'design document'
    : mode === 'plan' ? 'implementation plan' : 'changed code');
  return [
    `You are a code reviewer performing a ${label} for the Discord Ferry project.`,
    '',
    PROJECT_CONTEXT,
    titleLine,
    focusLine,
    '',
    directives,
    OUTPUT_DIRECTIVES,
    '',
    `The ${payload} follows on stdin.`,
  ].join('\n');
}

export function classifyReviewFailure(error) {
  const declared = error?.reviewFailure ?? error?.classification;
  if (FAILURE_CLASSES.has(declared)) return declared;
  if (error?.name === 'TimeoutError' || error?.code === 'ETIMEDOUT') return 'timeout';
  if (error?.name === 'SyntaxError' || error?.code === 'INVALID_SCHEMA') return 'schema';
  if (error?.code === 'WRONG_MODEL') return 'wrong-model';
  if (error?.code === 'CREDENTIAL' || error?.httpStatus === 401 || error?.httpStatus === 403) {
    return 'credential';
  }
  if (error?.httpStatus === 429) return 'rate-limit';
  if (Number.isInteger(error?.httpStatus) && error.httpStatus >= 500 && error.httpStatus <= 599) {
    return 'provider';
  }
  if (Number.isInteger(error?.httpStatus)) return 'request';
  if (error?.code === 'ENOENT' || typeof error?.status === 'number' || error?.signal) {
    return 'child-exit';
  }
  return 'unknown';
}

export function safeChildFailure(adapter, error) {
  if (error?.code === 'ENOENT') return `${adapter} executable not found`;
  if (error?.name === 'TimeoutError' || error?.code === 'ETIMEDOUT') {
    return `${adapter} timed out`;
  }
  if (typeof error?.status === 'number' || error?.signal) return `${adapter} child failed`;
  return `${adapter} returned invalid output`;
}

export function makeReviewRecord({
  adapter,
  slot,
  requestedModel,
  resolvedModel = null,
  substitutionFor = null,
  substitutionReason = null,
  sessionId,
  durationMs,
  status,
  result = null,
}) {
  if (!VALID_STATUSES.has(status)) throw new Error(`invalid review status: ${status}`);
  if (status === 'valid' && !validateFindings(result)) {
    throw new Error('valid review record requires schema-valid findings');
  }
  if (substitutionFor !== null && !SUBSTITUTION_REASONS.has(substitutionReason)) {
    throw new Error('substitution requires a structural reason');
  }
  if (substitutionFor === null && substitutionReason !== null) {
    throw new Error('substitution reason requires a substituted slot');
  }
  return {
    adapter,
    slot,
    requested_model: requestedModel,
    resolved_model: resolvedModel,
    substitution_for: substitutionFor,
    substitution_reason: substitutionReason,
    session_id: sessionId,
    duration_ms: durationMs,
    status,
    findings: status === 'valid' ? result.findings : [],
    summary: status === 'valid' ? result.summary : '',
    confidence: status === 'valid' ? result.confidence : null,
  };
}

export function reviewInputDigest(input) {
  return createHash('sha256').update(input).digest('hex');
}

export function evaluatePlanGate(route, verdicts, expectedInputSha256) {
  if (!/^[a-f0-9]{64}$/u.test(expectedInputSha256 ?? '') ||
      route?.input_sha256 !== expectedInputSha256) {
    return { ready: false, reason: 'plan review input mismatch', minor_findings: [] };
  }
  const record = route?.accepted;
  if (!validateFindings({
    findings: record?.findings,
    summary: record?.summary,
    confidence: record?.confidence,
  })) {
    return { ready: false, reason: 'invalid plan review record', minor_findings: [] };
  }
  const expectedModel = PLAN_MODELS.get(record?.slot);
  const expectedRequestedModel = PLAN_REQUESTED_MODELS.get(record?.slot);
  const attempts = Array.isArray(route?.attempts) ? route.attempts : [];
  const selectedSlot = PLAN_SLOTS.get(route?.selected_provider);
  const validPrimary = record?.slot === selectedSlot
    && attempts.length === 1
    && attempts[0]?.slot === record.slot
    && attempts[0]?.requested_model === record.requested_model
    && attempts[0]?.resolved_model === record.resolved_model
    && attempts[0]?.session_id === record.session_id
    && attempts[0]?.substitution_for === null
    && attempts[0]?.substitution_reason === null
    && record.substitution_for === null
    && record.substitution_reason === null
    && route.automatic_opus_calls === 0
    && route.owner_selected_opus_calls === (route.selected_provider === 'opus' ? 1 : 0);
  if (
    record?.status !== 'valid'
    || record.requested_model !== expectedRequestedModel
    || record.resolved_model !== expectedModel
    || !validPrimary
  ) {
    return { ready: false, reason: 'invalid plan review route', minor_findings: [] };
  }
  if (
    !Array.isArray(verdicts)
    || verdicts.length !== record.findings.length
    || verdicts.some((verdict) => !['CONFIRMED', 'REFUTED'].includes(verdict))
  ) {
    return { ready: false, reason: 'unverified plan findings', minor_findings: [] };
  }
  const blocking = record.findings.filter((finding, index) =>
    ['critical', 'important'].includes(finding.severity) && verdicts[index] === 'CONFIRMED');
  const minor = record.findings.filter((finding, index) =>
    finding.severity === 'minor' && verdicts[index] === 'CONFIRMED');
  return {
    ready: blocking.length === 0,
    reason: blocking.length ? 'confirmed blocking plan findings' : null,
    minor_findings: minor,
    accepted_slot: record.slot,
    accepted_model: record.resolved_model,
  };
}

function sampleFinding(severity = 'minor', category = 'correctness') {
  return {
    severity,
    category,
    file: 'docs/plan.md',
    line: null,
    description: 'description',
    suggestion: 'suggestion',
    verification: {
      command: 'true',
      confirms_if: 'the condition is present',
      refutes_if: 'the condition is absent',
    },
  };
}

function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  const clean = { findings: [], summary: 'clean', confidence: 'high' };

  for (const severity of VALID_SEVERITIES) {
    record(`schema severity ${severity}`, validateFindings({
      findings: [sampleFinding(severity)], summary: 'ok', confidence: 'medium',
    }));
  }
  for (const category of VALID_CATEGORIES) {
    record(`schema category ${category}`, validateFindings({
      findings: [sampleFinding('minor', category)], summary: 'ok', confidence: 'low',
    }));
  }
  for (const confidence of VALID_CONFIDENCES) {
    record(`schema confidence ${confidence}`, validateFindings({ ...clean, confidence }));
  }
  for (const key of VERIFICATION_KEYS) {
    const finding = sampleFinding();
    delete finding.verification[key];
    record(`schema rejects verification missing ${key}`, !validateFindings({
      findings: [finding], summary: 'bad', confidence: 'high',
    }));
  }
  record('schema rejects invalid enum', !validateFindings({
    findings: [sampleFinding('urgent')], summary: 'bad', confidence: 'high',
  }));
  record('schema rejects added field', !validateFindings({ ...clean, added: true }));
  record('JSON parser accepts valid text', parseJsonText('{"ok":true}').ok === true);
  const outputPrompt = buildReviewPrompt({ mode: 'chunk' });
  record('prompt requires findings summary and confidence',
    outputPrompt.includes('findings, summary, and')
      && outputPrompt.includes('always include summary and confidence'));
  let malformedSafe = false;
  try {
    parseJsonText('{"FERRY_SECRET_CANARY":', 'fixture');
  } catch (error) {
    malformedSafe = error.message === 'fixture returned invalid JSON';
  }
  record('JSON parser redacts malformed input', malformedSafe);

  const validOpus = makeReviewRecord({
    adapter: 'claude', slot: 'plan-opus', requestedModel: 'opus',
    resolvedModel: 'claude-opus-5', sessionId: 'opus-1', durationMs: 1,
    status: 'valid', result: clean,
  });
  const failedOpus = makeReviewRecord({
    adapter: 'claude', slot: 'plan-opus', requestedModel: 'opus',
    sessionId: 'opus-failed', durationMs: 1, status: 'failed',
  });
  const timedOutOpus = makeReviewRecord({
    adapter: 'claude', slot: 'plan-opus', requestedModel: 'opus',
    sessionId: 'opus-timeout', durationMs: 1, status: 'timed_out',
  });
  const validQwen = makeReviewRecord({
    adapter: 'qwen', slot: 'plan-qwen', requestedModel: 'qwen3.8-max',
    resolvedModel: 'qwen3.8-max', sessionId: 'qwen-1', durationMs: 1,
    status: 'valid', result: clean,
  });
  record('record accepts valid status', validOpus.status === 'valid');
  record('record accepts failed status', failedOpus.status === 'failed');
  record('record accepts timed out status', timedOutOpus.status === 'timed_out');
  let badStatus = false;
  try {
    makeReviewRecord({ status: 'maybe' });
  } catch {
    badStatus = true;
  }
  record('record rejects invalid status', badStatus);
  let missingReason = false;
  try {
    makeReviewRecord({
      adapter: 'qwen', slot: 'plan-qwen', requestedModel: 'qwen3.8-max',
      substitutionFor: 'plan-opus', sessionId: 'x', durationMs: 1,
      status: 'valid', result: clean,
    });
  } catch {
    missingReason = true;
  }
  record('record requires substitution reason', missingReason);

  const planDigest = reviewInputDigest('current plan');
  const routeFor = (selectedProvider, accepted, attempts = [accepted]) => ({
    selected_provider: selectedProvider,
    accepted,
    attempts,
    input_sha256: planDigest,
    automatic_opus_calls: 0,
    owner_selected_opus_calls: selectedProvider === 'opus' ? 1 : 0,
  });
  const primaryRoute = routeFor('qwen', validQwen);
  record('plan primary route passes clean result',
    evaluatePlanGate(primaryRoute, [], planDigest).ready);
  const blockingResult = {
    findings: [sampleFinding('important')], summary: 'blocking', confidence: 'high',
  };
  const blockingQwen = makeReviewRecord({
    adapter: 'qwen', slot: 'plan-qwen', requestedModel: 'qwen3.8-max',
    resolvedModel: 'qwen3.8-max', sessionId: 'qwen-2', durationMs: 1,
    status: 'valid', result: blockingResult,
  });
  record('plan primary route blocks confirmed finding', !evaluatePlanGate(
    routeFor('qwen', blockingQwen), ['CONFIRMED'], planDigest,
  ).ready);
  record('plan route blocks missing verdict', !evaluatePlanGate(
    routeFor('qwen', blockingQwen), [], planDigest,
  ).ready);
  record('plan accepts owner-selected Opus',
    evaluatePlanGate(routeFor('opus', validOpus), [], planDigest).ready);
  record('plan rejects Opus under Qwen selection', !evaluatePlanGate(
    routeFor('qwen', validOpus), [], planDigest,
  ).ready);
  record('plan rejects substituted Qwen', !evaluatePlanGate(
    routeFor('qwen', {
      ...validQwen,
      substitution_for: 'plan-opus',
      substitution_reason: 'timeout',
    }, [timedOutOpus, validQwen]), [], planDigest,
  ).ready);
  record('failure classifier identifies every structural class', [
    classifyReviewFailure({ code: 'CREDENTIAL' }),
    classifyReviewFailure({ name: 'TimeoutError' }),
    classifyReviewFailure({ code: 'INVALID_SCHEMA' }),
    classifyReviewFailure({ code: 'WRONG_MODEL' }),
    classifyReviewFailure({ status: 1 }),
    classifyReviewFailure({}),
  ].join(',') === 'credential,timeout,schema,wrong-model,child-exit,unknown');
  record('child failure hides child streams', !safeChildFailure('fixture', {
    status: 1,
    stdout: 'FERRY_SECRET_CANARY',
    stderr: 'FERRY_SECRET_CANARY',
    message: 'FERRY_SECRET_CANARY',
  }).includes('FERRY_SECRET_CANARY'));

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) {
    process.stderr.write(`review-contract self-test: ${failed.length} failure(s)\n`);
    process.exit(1);
  }
  process.stderr.write('review-contract plan-gate fixtures: exercised\n');
  process.stderr.write('review-contract self-test: all checks passed\n');
}

function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && args[0] === '--self-test') return selfTest();
  if (args.length === 4 && args[0] === '--evaluate-plan-record') {
    let route;
    let verdicts;
    try {
      route = parseJsonText(args[1], 'plan route');
      verdicts = parseJsonText(args[2], 'plan verdicts');
    } catch (error) {
      process.stderr.write(`review-contract: ${safeChildFailure('plan gate', error)}\n`);
      process.exit(1);
    }
    const decision = evaluatePlanGate(route, verdicts, args[3]);
    process.stdout.write(`${JSON.stringify(decision)}\n`);
    if (!decision.ready) process.exitCode = 1;
    return;
  }
  if (args.length === 4 && args[0] === '--evaluate-plan-files') {
    const root = realpathSync(process.cwd());
    const paths = args.slice(1).map((path) => resolve(root, path));
    if (paths.some((path) => {
      let target;
      try {
        target = realpathSync(path);
      } catch {
        return true;
      }
      const offset = relative(root, target);
      return offset.startsWith('..') || isAbsolute(offset);
    })) {
      process.stderr.write('review-contract: plan gate file leaves the checkout\n');
      process.exit(1);
      return;
    }
    let route;
    let verdicts;
    try {
      route = parseJsonText(readFileSync(paths[0], 'utf8'), 'plan route');
      verdicts = parseJsonText(readFileSync(paths[1], 'utf8'), 'plan verdicts');
    } catch (error) {
      process.stderr.write(`review-contract: ${safeChildFailure('plan gate', error)}\n`);
      process.exit(1);
      return;
    }
    const decision = evaluatePlanGate(
      route,
      verdicts,
      reviewInputDigest(readFileSync(paths[2])),
    );
    process.stdout.write(`${JSON.stringify(decision)}\n`);
    if (!decision.ready) process.exitCode = 1;
    return;
  }
  process.stderr.write(
    'review-contract: expected --self-test, --evaluate-plan-record, or --evaluate-plan-files\n',
  );
  process.exit(2);
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    // An invalid entrypoint cannot be the current module.
  }
}
if (invokedAsMain) main();
