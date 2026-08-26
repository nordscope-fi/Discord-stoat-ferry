#!/usr/bin/env node

import { readFileSync, realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  buildReviewPrompt,
  classifyReviewFailure,
  makeReviewRecord,
  reviewInputDigest,
  validateFindings,
} from './review-contract.mjs';
import { runClaudeReview } from './claude-review.mjs';
import { runQwenReview } from './qwen-review.mjs';
import { runVibeReview } from './vibe-review.mjs';

const PROVIDERS = [
  {
    slot: 'mistral-vibe',
    adapter: 'vibe',
    requestedModel: 'zai-glm-5-2',
    call: 'vibe',
  },
  {
    slot: 'qwen',
    adapter: 'qwen-api',
    requestedModel: 'qwen3.8-max',
    call: 'qwen',
  },
];

const PLAN_PROVIDERS = Object.freeze({
  qwen: {
    slot: 'plan-qwen',
    adapter: 'qwen-api',
    requestedModel: 'qwen3.8-max',
    call: 'qwen',
  },
  opus: {
    slot: 'plan-opus',
    adapter: 'claude',
    requestedModel: 'opus',
    resolvedModel: 'claude-opus-5',
    call: 'opus',
  },
});

function validProviderRecord(record, provider) {
  const resolvedModel = provider.resolvedModel ?? provider.requestedModel;
  return record?.status === 'valid'
    && record.slot === provider.slot
    && record.requested_model === provider.requestedModel
    && record.resolved_model === resolvedModel
    && record.substitution_for === null
    && validateFindings({
      findings: record.findings,
      summary: record.summary,
      confidence: record.confidence,
    });
}

async function attemptProvider(provider, request, adapters) {
  try {
    const result = await adapters[provider.call]({
      ...request,
      slot: provider.slot,
      ...(provider.call === 'opus' ? { record: true } : {}),
    });
    return validProviderRecord(result, provider)
      ? result
      : failedProviderRecord(provider, fulfilledFailure(result, provider));
  } catch (error) {
    return failedProviderRecord(provider, error);
  }
}

function fulfilledFailure(record, provider) {
  const resolvedModel = provider.resolvedModel ?? provider.requestedModel;
  if (record?.resolved_model && record.resolved_model !== resolvedModel) {
    return { code: 'WRONG_MODEL' };
  }
  if (record?.status === 'timed_out') return { code: 'ETIMEDOUT' };
  if (record?.failure_class) return { classification: record.failure_class };
  return { code: 'INVALID_SCHEMA' };
}

function failedProviderRecord(provider, error) {
  const failureClass = classifyReviewFailure(error);
  return {
    ...makeReviewRecord({
      adapter: provider.adapter,
      slot: provider.slot,
      requestedModel: provider.requestedModel,
      sessionId: null,
      durationMs: Number.isFinite(error?.durationMs) ? error.durationMs : 0,
      status: failureClass === 'timeout' ? 'timed_out' : 'failed',
    }),
    failure_class: failureClass,
    failure_stage: typeof error?.stage === 'string' ? error.stage : null,
    http_status: Number.isInteger(error?.httpStatus) ? error.httpStatus : null,
  };
}

export async function runEnsemble(request, adapters) {
  const outcomes = await Promise.allSettled(PROVIDERS.map((provider) =>
    adapters[provider.call]({ ...request, slot: provider.slot })));
  const slots = {};
  for (let index = 0; index < PROVIDERS.length; index += 1) {
    const provider = PROVIDERS[index];
    const outcome = outcomes[index];
    if (outcome.status === 'fulfilled' && validProviderRecord(outcome.value, provider)) {
      slots[provider.slot] = outcome.value;
      continue;
    }
    const error = outcome.status === 'rejected'
      ? outcome.reason
      : fulfilledFailure(outcome.value, provider);
    slots[provider.slot] = failedProviderRecord(provider, error);
  }
  const validSlots = PROVIDERS.filter((provider) =>
    slots[provider.slot].status === 'valid').length;
  return {
    policy: 'ferry-two-provider-advisory-v2',
    valid_slots: validSlots,
    automatic_opus_calls: 0,
    availability_blocks: false,
    slots,
  };
}

export async function runPlanReview({
  request,
  adapters,
  inputSha256,
  selectedProvider = 'qwen',
}) {
  if (!/^[a-f0-9]{64}$/u.test(inputSha256 ?? '')) {
    throw new Error('plan review requires an input digest');
  }
  const provider = PLAN_PROVIDERS[selectedProvider];
  if (!provider) throw new Error('plan provider must be qwen or opus');
  const record = await attemptProvider(provider, request, adapters);
  return {
    policy: 'ferry-selected-plan-v3',
    selected_provider: selectedProvider,
    attempts: [record],
    accepted: record.status === 'valid' ? record : null,
    input_sha256: inputSha256,
    automatic_opus_calls: 0,
    owner_selected_opus_calls: selectedProvider === 'opus' ? 1 : 0,
  };
}

export function parseArgs(argv) {
  const args = {
    selfTest: false,
    plan: false,
    planProvider: 'qwen',
    mode: 'chunk',
    title: '',
    focus: '',
  };
  let planProviderProvided = false;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--self-test') args.selfTest = true;
    else if (argument === '--plan') args.plan = true;
    else if (argument === '--json') continue;
    else if (argument === '--plan-provider') {
      planProviderProvided = true;
      args.planProvider = argv[++index] ?? '';
    }
    else if (['--mode', '--title', '--focus'].includes(argument)) {
      args[argument.slice(2)] = argv[++index] ?? '';
    } else throw new Error(`unknown argument: ${argument}`);
  }
  if (!['chunk', 'whole-branch'].includes(args.mode)) {
    throw new Error('mode must be chunk or whole-branch');
  }
  if (args.planProvider !== 'qwen' && args.planProvider !== 'opus') {
    throw new Error('plan provider must be qwen or opus');
  }
  if (!args.plan && planProviderProvided) {
    throw new Error('--plan-provider requires --plan');
  }
  return args;
}

function fixtureRecord(provider) {
  return makeReviewRecord({
    adapter: provider.adapter,
    slot: provider.slot,
    requestedModel: provider.requestedModel,
    resolvedModel: provider.requestedModel,
    sessionId: `${provider.slot}-session`,
    durationMs: 1,
    status: 'valid',
    result: { findings: [], summary: 'clean', confidence: 'high' },
  });
}

async function selfTest() {
  const checks = [];
  const record = (name, ok) => checks.push({ name, ok });
  const adapters = {
    vibe: async () => fixtureRecord(PROVIDERS[0]),
    qwen: async () => fixtureRecord(PROVIDERS[1]),
  };
  const clean = await runEnsemble({ prompt: 'fixture' }, adapters);
  record('both fixed providers are valid', clean.valid_slots === 2);
  record('availability is advisory', clean.availability_blocks === false);
  record('automatic Opus calls are zero', clean.automatic_opus_calls === 0);

  const oneFailure = await runEnsemble({ prompt: 'fixture' }, {
    ...adapters,
    vibe: async () => {
      const error = new Error('unsafe credential detail');
      error.code = 'CREDENTIAL';
      throw error;
    },
  });
  record('credential failure is recorded',
    oneFailure.slots['mistral-vibe'].failure_class === 'credential');
  record('one provider failure does not block',
    oneFailure.valid_slots === 1 && oneFailure.availability_blocks === false);

  const wrongModel = await runEnsemble({ prompt: 'fixture' }, {
    ...adapters,
    qwen: async () => ({ ...fixtureRecord(PROVIDERS[1]), resolved_model: 'other' }),
  });
  record('wrong model is rejected', wrongModel.slots.qwen.failure_class === 'wrong-model');

  const malformed = await runEnsemble({ prompt: 'fixture' }, {
    ...adapters,
    qwen: async () => ({ ...fixtureRecord(PROVIDERS[1]), confidence: 'certain' }),
  });
  record('malformed result is rejected', malformed.slots.qwen.failure_class === 'schema');

  let opusCalls = 0;
  const bothFail = await runEnsemble({ prompt: 'fixture' }, {
    vibe: async () => { throw new Error('vibe failure'); },
    qwen: async () => { throw new Error('qwen failure'); },
    opus: async () => { opusCalls += 1; },
  });
  record('dual failure remains nonblocking',
    bothFail.valid_slots === 0 && bothFail.availability_blocks === false);
  record('optional Opus adapter is never called', opusCalls === 0);

  const plan = await runPlanReview({
    request: { prompt: 'fixture' },
    inputSha256: reviewInputDigest('fixture'),
    adapters: { qwen: async () => makeReviewRecord({
      adapter: 'qwen-api', slot: 'plan-qwen', requestedModel: 'qwen3.8-max',
      resolvedModel: 'qwen3.8-max', sessionId: 'plan-session', durationMs: 1,
      status: 'valid', result: { findings: [], summary: 'clean', confidence: 'high' },
    }) },
  });
  record('plan route uses Qwen only',
    plan.accepted?.slot === 'plan-qwen'
      && plan.automatic_opus_calls === 0
      && plan.owner_selected_opus_calls === 0);

  const opusPlan = await runPlanReview({
    request: { prompt: 'fixture' },
    inputSha256: reviewInputDigest('fixture'),
    selectedProvider: 'opus',
    adapters: { opus: async () => makeReviewRecord({
      adapter: 'claude', slot: 'plan-opus', requestedModel: 'opus',
      resolvedModel: 'claude-opus-5', sessionId: 'opus-plan-session', durationMs: 1,
      status: 'valid', result: { findings: [], summary: 'clean', confidence: 'high' },
    }) },
  });
  record('owner-selected plan route uses Opus only',
    opusPlan.accepted?.slot === 'plan-opus'
      && opusPlan.automatic_opus_calls === 0
      && opusPlan.owner_selected_opus_calls === 1);

  const failed = checks.filter((check) => !check.ok);
  for (const check of checks) {
    process.stderr.write(`  ${check.ok ? 'ok  ' : 'FAIL'} ${check.name}\n`);
  }
  if (failed.length) throw new Error(`${failed.length} self-test failure(s)`);
  process.stderr.write('review-ensemble self-test: all checks passed\n');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  const payload = readFileSync(0, 'utf8');
  if (!payload.trim()) throw new Error('review-ensemble requires a review payload on stdin');
  const prompt = `${buildReviewPrompt({
    mode: args.plan ? 'plan' : args.mode,
    title: args.title,
    focus: args.focus,
  })}\n\n${payload}`;
  const report = args.plan
    ? await runPlanReview({
        request: { prompt, home: process.env.HOME },
        inputSha256: reviewInputDigest(payload),
        selectedProvider: args.planProvider,
        adapters: { qwen: runQwenReview, opus: runClaudeReview },
      })
    : await runEnsemble(
        { prompt, home: process.env.HOME },
        { vibe: runVibeReview, qwen: runQwenReview },
      );
  process.stdout.write(`${JSON.stringify(report)}\n`);
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
    process.stderr.write(`review-ensemble: ${error.message}\n`);
    process.exitCode = 1;
  });
}
