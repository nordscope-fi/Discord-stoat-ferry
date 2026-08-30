#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { verificationExitAllowed } from './review-verification.mjs';

export const VERIFICATION_OUTCOME_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    exit_code: { type: 'integer', enum: [0, 1] },
    stdout_contains: { type: ['string', 'null'] },
    stdout_excludes: { type: ['string', 'null'] },
  },
  required: ['exit_code', 'stdout_contains', 'stdout_excludes'],
};

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
              confirms_if: VERIFICATION_OUTCOME_SCHEMA,
              refutes_if: VERIFICATION_OUTCOME_SCHEMA,
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
null. verification has command, confirms_if, and refutes_if. confirms_if and refutes_if cannot both
match one result. Give them different exit_code values, or make one require the exact substring that
the other excludes. Each object has exactly exit_code, stdout_contains, and stdout_excludes.
exit_code is 0 or 1. Each stdout field is null or a non-empty exact substring. Use an empty findings
array for a clean review, but always include summary and confidence. Exit code 1 is valid only for
rg and git grep commands.`;

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
const VERIFICATION_OUTCOME_KEYS = new Set([
  'exit_code',
  'stdout_contains',
  'stdout_excludes',
]);
const RESULT_KEYS = new Set(['findings', 'summary', 'confidence']);

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

function validVerificationOutcome(outcome) {
  if (!outcome || typeof outcome !== 'object' || Array.isArray(outcome)) return false;
  if (!hasExactKeys(outcome, VERIFICATION_OUTCOME_KEYS)) return false;
  if (!Number.isInteger(outcome.exit_code) || ![0, 1].includes(outcome.exit_code)) return false;
  for (const key of ['stdout_contains', 'stdout_excludes']) {
    const value = outcome[key];
    if (value !== null && (typeof value !== 'string' || !value.trim())) return false;
  }
  return true;
}

function outcomesAreExclusive(first, second) {
  if (first.exit_code !== second.exit_code) return true;
  const firstConflicts = first.stdout_contains !== null
    && second.stdout_excludes !== null
    && first.stdout_contains.includes(second.stdout_excludes);
  const secondConflicts = second.stdout_contains !== null
    && first.stdout_excludes !== null
    && second.stdout_contains.includes(first.stdout_excludes);
  return firstConflicts || secondConflicts;
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
    if (typeof verification.command !== 'string' || !verification.command.trim()) return false;
    if (!validVerificationOutcome(verification.confirms_if)) return false;
    if (!validVerificationOutcome(verification.refutes_if)) return false;
    const verificationArgv = verification.command.trim().split(/\s+/u);
    if (!verificationExitAllowed(verificationArgv, verification.confirms_if.exit_code)) {
      return false;
    }
    if (!verificationExitAllowed(verificationArgv, verification.refutes_if.exit_code)) {
      return false;
    }
    if (!outcomesAreExclusive(verification.confirms_if, verification.refutes_if)) {
      return false;
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
    const error = new Error('valid review record requires schema-valid findings');
    error.code = 'INVALID_SCHEMA';
    throw error;
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

export function planFindingDigest(findings) {
  return createHash('sha256').update(JSON.stringify(findings)).digest('hex');
}

export function reviewRecordDigest(record) {
  return createHash('sha256').update(JSON.stringify(record)).digest('hex');
}

function validPlanLedger(route, ledger) {
  if (!ledger || route?.policy !== 'ferry-bounded-plan-v4') return false;
  const attempts = ledger.attempts;
  const round = route.review_round;
  if (ledger.policy !== 'ferry-plan-review-budget-v1'
      || ledger.plan_id !== route.plan_id
      || ledger.selected_provider !== route.selected_provider
      || !Array.isArray(attempts)
      || attempts.length !== round
      || ![1, 2].includes(round)
      || route.review_budget !== 2
      || route.budget_remaining !== 2 - round) {
    return false;
  }
  const record = route.attempts?.[0];
  const latest = attempts.at(-1);
  return latest?.round === round
    && latest.input_sha256 === route.input_sha256
    && latest.status === record?.status
    && latest.slot === record?.slot
    && latest.requested_model === record?.requested_model
    && latest.resolved_model === record?.resolved_model
    && latest.session_id === record?.session_id
    && latest.failure_class === (record?.failure_class ?? null)
    && latest.record_sha256 === reviewRecordDigest(record);
}

function validFailedPlanRecord(route, record) {
  const selectedSlot = PLAN_SLOTS.get(route?.selected_provider);
  const expectedRequestedModel = PLAN_REQUESTED_MODELS.get(selectedSlot);
  return route?.accepted === null
    && Array.isArray(route.attempts)
    && route.attempts.length === 1
    && route.attempts[0] === record
    && ['failed', 'timed_out'].includes(record?.status)
    && record.slot === selectedSlot
    && record.requested_model === expectedRequestedModel
    && record.resolved_model === null
    && record.session_id === null
    && record.substitution_for === null
    && record.substitution_reason === null
    && FAILURE_CLASSES.has(record.failure_class)
    && route.automatic_opus_calls === 0
    && route.owner_selected_opus_calls === (route.selected_provider === 'opus' ? 1 : 0);
}

function matchesOwnerDecision(ownerDecision, route, blocking) {
  return ownerDecision?.decision === 'accept_recorded_risk'
    && ownerDecision.plan_id === route.plan_id
    && ownerDecision.input_sha256 === route.input_sha256
    && ownerDecision.review_round === 2
    && ownerDecision.finding_sha256 === planFindingDigest(blocking);
}

export function evaluatePlanGate(
  route,
  verdicts,
  expectedInputSha256,
  { ledger = null, ownerDecision = null } = {},
) {
  if (!/^[a-f0-9]{64}$/u.test(expectedInputSha256 ?? '') ||
      route?.input_sha256 !== expectedInputSha256) {
    return { ready: false, reason: 'plan review input mismatch', minor_findings: [] };
  }
  const bounded = route?.policy === 'ferry-bounded-plan-v4';
  if (bounded && !validPlanLedger(route, ledger)) {
    return { ready: false, reason: 'invalid plan review ledger', minor_findings: [] };
  }
  const attempt = Array.isArray(route?.attempts) && route.attempts.length === 1
    ? route.attempts[0]
    : null;
  if (bounded && validFailedPlanRecord(route, attempt)) {
    return {
      ready: true,
      reason: null,
      minor_findings: [],
      decision_required: false,
      warning: {
        failure_class: attempt.failure_class,
        failure_stage: attempt.failure_stage ?? null,
        http_status: attempt.http_status ?? null,
        duration_ms: attempt.duration_ms,
      },
    };
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
    || verdicts.some((verdict) => !['CONFIRMED', 'REFUTED', 'INCONCLUSIVE'].includes(verdict))
  ) {
    return { ready: false, reason: 'unverified plan findings', minor_findings: [] };
  }
  const blocking = record.findings.filter((finding, index) =>
    ['critical', 'important'].includes(finding.severity) && verdicts[index] === 'CONFIRMED');
  const minor = record.findings.filter((finding, index) =>
    finding.severity === 'minor' && verdicts[index] === 'CONFIRMED');
  if (blocking.length && bounded && route.review_round === 2) {
    if (matchesOwnerDecision(ownerDecision, route, blocking)) {
      return {
        ready: true,
        reason: null,
        minor_findings: minor,
        accepted_slot: record.slot,
        accepted_model: record.resolved_model,
        decision_required: false,
        owner_decision: 'accept_recorded_risk',
      };
    }
    return {
      ready: false,
      reason: 'owner decision required after final plan review',
      minor_findings: minor,
      decision_required: true,
      decision_binding: {
        plan_id: route.plan_id,
        input_sha256: route.input_sha256,
        review_round: 2,
        finding_sha256: planFindingDigest(blocking),
      },
    };
  }
  return {
    ready: blocking.length === 0,
    reason: blocking.length ? 'confirmed blocking plan findings' : null,
    minor_findings: minor,
    accepted_slot: record.slot,
    accepted_model: record.resolved_model,
    ...(bounded ? { decision_required: false, warning: null } : {}),
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
      command: 'git status --short',
      confirms_if: {
        exit_code: 0,
        stdout_contains: 'the condition is present',
        stdout_excludes: null,
      },
      refutes_if: {
        exit_code: 0,
        stdout_contains: null,
        stdout_excludes: 'the condition is present',
      },
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
  if ([5, 6].includes(args.length) && args[0] === '--evaluate-plan-files') {
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
    let ledger;
    let ownerDecision = null;
    try {
      route = parseJsonText(readFileSync(paths[0], 'utf8'), 'plan route');
      verdicts = parseJsonText(readFileSync(paths[1], 'utf8'), 'plan verdicts');
      ledger = parseJsonText(readFileSync(paths[3], 'utf8'), 'plan ledger');
      if (paths[4]) {
        ownerDecision = parseJsonText(readFileSync(paths[4], 'utf8'), 'owner decision');
      }
    } catch (error) {
      process.stderr.write(`review-contract: ${safeChildFailure('plan gate', error)}\n`);
      process.exit(1);
      return;
    }
    const decision = evaluatePlanGate(
      route,
      verdicts,
      reviewInputDigest(readFileSync(paths[2])),
      { ledger, ownerDecision },
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
