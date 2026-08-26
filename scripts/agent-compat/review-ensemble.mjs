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

function validProviderRecord(record, provider) {
  return record?.status === 'valid'
    && record.slot === provider.slot
    && record.requested_model === provider.requestedModel
    && record.resolved_model === provider.requestedModel
    && record.substitution_for === null
    && validateFindings({
      findings: record.findings,
      summary: record.summary,
      confidence: record.confidence,
    });
}

function fulfilledFailure(record, provider) {
  if (record?.resolved_model && record.resolved_model !== provider.requestedModel) {
    return { code: 'WRONG_MODEL' };
  }
  if (record?.status === 'timed_out') return { code: 'ETIMEDOUT' };
  if (record?.failure_class) return { classification: record.failure_class };
  return { code: 'INVALID_SCHEMA' };
}

function failedProviderRecord(provider, error) {
  return {
    ...makeReviewRecord({
      adapter: provider.adapter,
      slot: provider.slot,
      requestedModel: provider.requestedModel,
      sessionId: null,
      durationMs: 0,
      status: error?.code === 'ETIMEDOUT' || error?.name === 'TimeoutError'
        ? 'timed_out'
        : 'failed',
    }),
    failure_class: classifyReviewFailure(error),
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

export async function runPlanReview({ request, adapters, inputSha256 }) {
  if (!/^[a-f0-9]{64}$/u.test(inputSha256 ?? '')) {
    throw new Error('plan review requires an input digest');
  }
  const provider = {
    slot: 'plan-qwen',
    adapter: 'qwen-api',
    requestedModel: 'qwen3.8-max',
    call: 'qwen',
  };
  let record;
  try {
    const result = await adapters.qwen({ ...request, slot: provider.slot });
    record = validProviderRecord(result, provider)
      ? result
      : failedProviderRecord(provider, fulfilledFailure(result, provider));
  } catch (error) {
    record = failedProviderRecord(provider, error);
  }
  return {
    policy: 'ferry-qwen-plan-v2',
    attempts: [record],
    accepted: record.status === 'valid' ? record : null,
    input_sha256: inputSha256,
    automatic_opus_calls: 0,
  };
}

function parseArgs(argv) {
  const args = { selfTest: false, plan: false, mode: 'chunk', title: '', focus: '' };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--self-test') args.selfTest = true;
    else if (argument === '--plan') args.plan = true;
    else if (argument === '--json') continue;
    else if (['--mode', '--title', '--focus'].includes(argument)) {
      args[argument.slice(2)] = argv[++index] ?? '';
    } else throw new Error(`unknown argument: ${argument}`);
  }
  if (!['chunk', 'whole-branch'].includes(args.mode)) {
    throw new Error('mode must be chunk or whole-branch');
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
    plan.accepted?.slot === 'plan-qwen' && plan.automatic_opus_calls === 0);

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
        adapters: { qwen: runQwenReview },
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
