#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync,
  cpSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  readlinkSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { join, relative } from 'node:path';
import { performance } from 'node:perf_hooks';
import { dispatchCodexPreTool } from '../../scripts/agent-compat/codex-hook-adapter.mjs';
import {
  installReviewerRuntime,
  provisionReviewerAgent,
  runBootstrap,
  verifyReviewerRuntime,
} from '../../scripts/agent-compat/codex-bootstrap.mjs';
import {
  runLiveReadiness,
  runReviewerReadiness,
  runStaticReadiness,
  runWorktreeReadiness,
} from '../../scripts/agent-compat/codex-readiness.mjs';
import {
  assertTwoChatWrappers,
  canonicalJson,
  generatedHostSecretViolations,
} from '../../scripts/agent-compat/check.mjs';
import { routesFor } from '../../scripts/agent-compat/hook-parity.mjs';
import {
  canonicalCheckoutRoot,
  DIRECT_CHAT_COMMAND,
  removeUnusedIssueChannel,
  transformCodexChatHooks,
} from '../../scripts/agent-compat/plain-english-chat-hook.mjs';
import {
  evaluatePlanGate,
  makeReviewRecord,
  reviewInputDigest,
  safeChildFailure,
} from '../../scripts/agent-compat/review-contract.mjs';
import {
  runEnsemble,
  runPlanReview,
} from '../../scripts/agent-compat/review-ensemble.mjs';
import {
  authorizeVerificationCommand,
  codeGateRequired,
  contextTier,
  reverifyAfterFix,
  verifyFindings,
} from '../../scripts/agent-compat/review-verification.mjs';
import { readReviewerField } from '../../scripts/agent-compat/proton-credential.mjs';
import { runVibeReview } from '../../scripts/agent-compat/vibe-review.mjs';
import { runQwenReview } from '../../scripts/agent-compat/qwen-review.mjs';
import { runClaudeReview } from '../../scripts/agent-compat/claude-review.mjs';
import {
  advertisesSelfTest,
  runVerificationLayers,
} from '../../scripts/agent-compat/verify-all.mjs';

function uniqueRoutes(groups) {
  return [...new Set(groups.flat())];
}

function writeJson(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function hookRoutes() {
  writeJson({
    codex_write: uniqueRoutes([
      routesFor('PreToolUse', 'apply_patch', 'codex'),
      routesFor('PreToolUse', 'Edit', 'codex'),
      routesFor('PreToolUse', 'Write', 'codex'),
    ]),
    vibe_write: uniqueRoutes([
      routesFor('PreToolUse', 'Write', 'vibe'),
      routesFor('PreToolUse', 'Edit', 'vibe'),
    ]),
    qwen_write: routesFor('PreToolUse', 'Write', 'qwen'),
    codex_bash: routesFor('PreToolUse', 'Bash', 'codex'),
    vibe_bash: routesFor('PreToolUse', 'Bash', 'vibe'),
    qwen_bash: routesFor('PreToolUse', 'Bash', 'qwen'),
  });
}

function preToolTiming(policy) {
  if (policy !== 'patch') throw new Error(`unknown timing policy: ${policy}`);
  const routes = [];
  const started = performance.now();
  dispatchCodexPreTool({
    tool_name: 'apply_patch',
    tool_input: {
      patch: '*** Begin Patch\n*** Update File: docs/example.md\n@@\n-old\n+new\n*** End Patch',
    },
  }, route => routes.push(route));
  const durationMs = performance.now() - started;
  const plainEnglish = new Set(['docs', 'github-docs']);
  writeJson({
    plain_english_children: routes.filter(route => plainEnglish.has(route)).length,
    security_children: routes.filter(route => !plainEnglish.has(route)),
    duration_ms: durationMs,
  });
}

function directChatDocument(command) {
  const group = () => ({
    matcher: '*',
    hooks: [{ type: 'command', command, timeout: 10 }],
  });
  return {
    hooks: {
      Issue: [{ matcher: 'mcp__linear__', hooks: [] }],
      Stop: [group()],
      SubagentStop: [group()],
    },
  };
}

function hookRegeneration(shape) {
  const command = shape === 'current'
    ? DIRECT_CHAT_COMMAND
    : `${DIRECT_CHAT_COMMAND} --changed`;
  const runsIndex = process.argv.indexOf('--runs');
  const runs = runsIndex === -1 ? 1 : Number(process.argv[runsIndex + 1]);
  let previous = null;
  let document;
  for (let index = 0; index < runs; index += 1) {
    document = directChatDocument(command);
    removeUnusedIssueChannel(document);
    transformCodexChatHooks(document, '/canonical/ferry');
    assertTwoChatWrappers(document);
    const current = canonicalJson(document);
    if (previous !== null && current !== previous) throw new Error('hook regeneration drifted');
    previous = current;
  }
  const wrappers = ['Stop', 'SubagentStop'].flatMap((event) =>
    document.hooks[event].flatMap((group) => group.hooks));
  writeJson({
    wrapper_count: wrappers.length,
    timeouts: wrappers.map((hook) => hook.timeout),
  });
}

function directorySnapshot(root) {
  const records = {};
  function walk(path) {
    for (const name of readdirSync(path)) {
      const full = join(path, name);
      const key = relative(root, full);
      const stat = lstatSync(full);
      if (stat.isSymbolicLink()) records[key] = `link:${readlinkSync(full)}`;
      else if (stat.isDirectory()) walk(full);
      else records[key] = `file:${readFileSync(full).toString('base64')}`;
    }
  }
  walk(root);
  return JSON.stringify(records);
}

function installLinkedHosts() {
  const baseIndex = process.argv.indexOf('--base');
  const base = baseIndex === -1 ? null : process.argv[baseIndex + 1];
  if (!base) throw new Error('--base is required');
  const repo = process.cwd();
  const primary = join(base, 'primary');
  const checkout = join(base, 'checkout');
  const home = join(base, 'home');
  mkdirSync(primary, { recursive: true });
  mkdirSync(checkout, { recursive: true });
  mkdirSync(home, { recursive: true });
  const init = spawnSync('git', ['init', '-q', checkout], { encoding: 'utf8' });
  if (init.status !== 0) throw new Error(init.stderr);
  cpSync(join(repo, 'config'), join(checkout, 'config'), { recursive: true });
  cpSync(join(repo, '.claude', 'skills'), join(checkout, '.claude', 'skills'), {
    recursive: true,
    verbatimSymlinks: true,
  });
  cpSync(join(repo, 'AGENTS.md'), join(checkout, 'AGENTS.md'));
  const hosts = ['.agents', '.codex', '.vibe', '.qwen'];
  const before = {};
  for (const host of hosts) {
    const owner = join(primary, host);
    if (host === '.agents') {
      cpSync(join(repo, host), owner, { recursive: true, dereference: true });
    } else {
      mkdirSync(owner, { recursive: true });
    }
    writeFileSync(join(owner, 'sentinel.txt'), `${host}:owner\n`);
    before[host] = directorySnapshot(owner);
    symlinkSync(owner, join(checkout, host), 'dir');
  }
  const installed = spawnSync(
    process.execPath,
    [join(repo, 'scripts', 'agent-compat', 'install-local.mjs')],
    {
      cwd: checkout,
      env: { ...process.env, HOME: home },
      encoding: 'utf8',
    },
  );
  const unchanged = Object.fromEntries(hosts.map((host) => [
    host,
    before[host] === directorySnapshot(join(primary, host)),
  ]));
  writeJson({
    ran_real_installer: installed.status === 0,
    primary_bytes_unchanged: unchanged,
  });
  if (installed.status !== 0) {
    process.stderr.write(installed.stderr);
    process.exitCode = installed.status ?? 1;
  }
}

function setupRepeatReal() {
  const baseIndex = process.argv.indexOf('--base');
  const base = baseIndex === -1 ? null : process.argv[baseIndex + 1];
  if (!base) throw new Error('--base is required');
  const source = process.cwd();
  const root = join(base, 'repo');
  const home = join(base, 'home');
  const fakeBin = join(base, 'bin');
  mkdirSync(root, { recursive: true });
  mkdirSync(home, { recursive: true });
  mkdirSync(fakeBin, { recursive: true });
  const initialized = spawnSync('git', ['init', '-q', root], { encoding: 'utf8' });
  if (initialized.status !== 0) throw new Error(initialized.stderr);
  cpSync(join(source, 'config'), join(root, 'config'), { recursive: true });
  cpSync(join(source, 'scripts'), join(root, 'scripts'), { recursive: true });
  cpSync(join(source, 'AGENTS.md'), join(root, 'AGENTS.md'));
  cpSync(join(source, '.claude', 'skills'), join(root, '.claude', 'skills'), {
    recursive: true,
    dereference: true,
  });
  mkdirSync(join(root, '.claude', 'scripts'), { recursive: true });
  cpSync(
    join(source, '.claude', 'scripts', 'new-worktree.sh'),
    join(root, '.claude', 'scripts', 'new-worktree.sh'),
  );
  cpSync(
    join(canonicalCheckoutRoot(source), '.worktreeinclude'),
    join(root, '.worktreeinclude'),
  );

  const passCli = join(home, '.local', 'bin', 'pass-cli');
  mkdirSync(join(home, '.local', 'bin'), { recursive: true });
  writeFileSync(passCli, `#!/bin/sh
state="$HOME/.config/fake-pass-agent"
if [ "$1 $2 $3" = "agent create --help" ]; then
  printf 'NAME --expiration 3m --vault\\n'
elif [ "$1 $2" = "vault list" ]; then
  printf '[{"name":"PortalPilot"}]\\n'
elif [ "$1 $2" = "agent list" ]; then
  if [ -f "$state" ]; then printf '[{"name":"discord-ferry-reviewers"}]\\n'; else printf '[]\\n'; fi
elif [ "$1 $2" = "agent create" ]; then
  mkdir -p "$HOME/.config"
  : > "$state"
  printf '{"token":"PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_12345678901234567890123456789012"}\\n'
else
  exit 1
fi
`);
  chmodSync(passCli, 0o755);
  const realNode = process.execPath;
  const fakeNode = join(fakeBin, 'node');
  writeFileSync(fakeNode, `#!/bin/sh
case "$1" in
  */codex-readiness.mjs) exit 0 ;;
  *) exec "${realNode}" "$@" ;;
esac
`);
  chmodSync(fakeNode, 0o755);
  const rules = join(home, '.codex', 'rules');
  mkdirSync(rules, { recursive: true });
  writeFileSync(
    join(rules, 'ferry-reviewers.rules'),
    'prefix_rule(pattern=["node", "/fixture/reviewer-runtime/current/review-ensemble.mjs"], decision="allow")\n',
  );
  const environment = {
    ...process.env,
    HOME: home,
    CODEX_HOME: join(home, '.codex'),
    PATH: `${fakeBin}:${process.env.PATH}`,
  };
  const run = () => spawnSync('bash', [join(root, 'scripts', 'codex-setup.sh')], {
    cwd: root,
    env: environment,
    encoding: 'utf8',
  });
  const digest = () => createHash('sha256').update(directorySnapshot(base)).digest('hex');
  const first = run();
  if (first.status !== 0) throw new Error(first.stderr || first.stdout);
  const firstHashes = digest();
  const second = run();
  if (second.status !== 0) throw new Error(second.stderr || second.stdout);
  const secondHashes = digest();
  const config = readFileSync(join(home, '.codex', 'config.toml'), 'utf8');
  const ruleSource = readFileSync(join(rules, 'ferry-reviewers.rules'), 'utf8');
  const leftovers = [];
  const scan = (path) => {
    for (const name of readdirSync(path)) {
      const full = join(path, name);
      const stat = lstatSync(full);
      if (/\.(?:tmp|bak)$/u.test(name)) leftovers.push(relative(base, full));
      if (stat.isDirectory()) scan(full);
    }
  };
  scan(base);
  writeJson({
    ran_real_setup: true,
    first_hashes: firstHashes,
    second_hashes: secondHashes,
    trust_entries: config.split(/\r?\n/u).filter((line) =>
      line.trim() === `[projects.${JSON.stringify(root)}]`).length,
    rule_entries: ruleSource.split(/\r?\n/u).filter((line) =>
      line.startsWith('prefix_rule(')).length,
    leftover_temp_or_backup_files: leftovers,
  });
}

const [mode, argument] = process.argv.slice(2);

switch (mode) {
  case 'hook-routes':
    hookRoutes();
    break;
  case 'pre-tool-timing':
    preToolTiming(argument);
    break;
  case 'canonical-checkout-root': {
    const rootIndex = process.argv.indexOf('--root');
    if (rootIndex < 0 || !process.argv[rootIndex + 1]) {
      throw new Error('--root is required');
    }
    writeJson({ root: canonicalCheckoutRoot(process.argv[rootIndex + 1]) });
    break;
  }
  case 'hook-regeneration':
    hookRegeneration(argument);
    break;
  case 'install-linked-hosts':
    installLinkedHosts();
    break;
  case 'setup-repeat-real':
    setupRepeatReal();
    break;
  case 'review-contract': {
    if (argument !== 'canary-child-error') throw new Error('unknown review contract fixture');
    const error = {
      status: 23,
      stdout: 'FERRY_SECRET_CANARY',
      stderr: 'FERRY_SECRET_CANARY',
      message: 'FERRY_SECRET_CANARY',
    };
    writeJson({ stage: 'child-exit', error: safeChildFailure('fixture', error) });
    process.exitCode = 1;
    break;
  }
  case 'proton-field': {
    const homeIndex = process.argv.indexOf('--home');
    const home = homeIndex === -1 ? null : process.argv[homeIndex + 1];
    if (argument !== 'canary-child-error' || !home) {
      throw new Error('invalid proton field fixture');
    }
    const tokenDirectory = join(home, '.config', 'discord-ferry');
    mkdirSync(tokenDirectory, { recursive: true });
    const tokenPath = join(tokenDirectory, 'reviewer-agent.pat');
    writeFileSync(tokenPath, `pst_${'x'.repeat(40)}`, { mode: 0o600 });
    try {
      await readReviewerField({
        itemTitle: 'Mistral Vibe API Key',
        reason: 'fixture',
        home,
        run: async () => {
          const error = new Error('FERRY_SECRET_CANARY');
          error.status = 23;
          error.stdout = 'FERRY_SECRET_CANARY';
          error.stderr = 'FERRY_SECRET_CANARY';
          throw error;
        },
      });
    } catch (error) {
      writeJson({ stage: error.stage ?? 'login', error: error.message });
      process.exitCode = 1;
    }
    break;
  }
  case 'vibe-review': {
    const help = [
      '--prompt', '--max-turns', '--max-tokens', '--enabled-tools',
      '--disabled-tools', '--output', '--trust',
    ].join('\n');
    const clean = {
      findings: [],
      summary: 'clean',
      confidence: 'high',
    };
    const history = [{
      session_id: 'fixture-session',
      message: {
        role: 'assistant',
        content: [{ type: 'text', text: JSON.stringify(clean) }],
      },
    }];
    if (argument === 'tool-call') {
      history.unshift({
        message: { role: 'assistant', content: [{ type: 'tool_call', name: 'read_file' }] },
      });
    }
    let argvContainsCanary = false;
    let stdinReceivedCanary = false;
    const run = async (command, args, options) => {
      if (args.includes('--help')) return { stdout: help };
      argvContainsCanary = args.some((value) => value.includes('FERRY_SECRET_CANARY'));
      stdinReceivedCanary = options.input?.includes('FERRY_SECRET_CANARY') ?? false;
      if (argument === 'canary-child-error') {
        const error = new Error('FERRY_SECRET_CANARY');
        error.status = 23;
        error.stdout = 'FERRY_SECRET_CANARY';
        error.stderr = 'FERRY_SECRET_CANARY';
        throw error;
      }
      return { stdout: JSON.stringify(history) };
    };
    try {
      await runVibeReview({
        prompt: argument === 'stdin-prompt' ? 'FERRY_SECRET_CANARY' : 'fixture prompt',
        home: process.cwd(),
        credential: async () => 'fixture-api-key',
        run,
      });
      if (argument === 'stdin-prompt') {
        writeJson({
          argv_contains_canary: argvContainsCanary,
          stdin_received_canary: stdinReceivedCanary,
        });
      }
    } catch (error) {
      if (argument === 'canary-child-error') {
        writeJson({ stage: error.stage ?? 'vibe-child', error: error.message });
      } else {
        process.stderr.write(`${error.message}\n`);
      }
      process.exitCode = 1;
    }
    break;
  }
  case 'qwen-review': {
    if (argument !== 'canary-response-error') throw new Error('invalid Qwen fixture');
    try {
      await runQwenReview({
        prompt: 'fixture prompt',
        home: process.cwd(),
        credential: async () => 'fixture-api-key',
        request: async () => {
          const error = new Error('FERRY_SECRET_CANARY');
          error.responseBody = 'FERRY_SECRET_CANARY';
          error.stdout = 'FERRY_SECRET_CANARY';
          error.stderr = 'FERRY_SECRET_CANARY';
          throw error;
        },
      });
    } catch (error) {
      writeJson({ stage: error.stage ?? 'qwen-response', error: error.message });
      process.exitCode = 1;
    }
    break;
  }
  case 'claude-review': {
    if (argument !== 'canary-child-error') throw new Error('invalid Claude fixture');
    try {
      await runClaudeReview({
        prompt: 'fixture prompt',
        resolve: () => '/fixture/claude',
        run: async (command, args) => {
          if (args.includes('--help')) {
            return { stdout: '--safe-mode\n--tools\n--prompt-suggestions\n' };
          }
          const error = new Error('FERRY_SECRET_CANARY');
          error.status = 23;
          error.stdout = 'FERRY_SECRET_CANARY';
          error.stderr = 'FERRY_SECRET_CANARY';
          throw error;
        },
      });
    } catch (error) {
      writeJson({ stage: error.stage ?? 'claude-child', error: error.message });
      process.exitCode = 1;
    }
    break;
  }
  case 'ensemble':
  case 'ensemble-no-opus':
  case 'ensemble-boundary': {
    const providerRecord = (slot) => {
      const qwen = slot === 'qwen';
      const model = qwen ? 'qwen3.8-max' : 'zai-glm-5-2';
      return makeReviewRecord({
        adapter: qwen ? 'qwen-api' : 'vibe',
        slot,
        requestedModel: model,
        resolvedModel: model,
        sessionId: `${slot}-fixture-session`,
        durationMs: 1,
        status: 'valid',
        result: { findings: [], summary: 'clean', confidence: 'high' },
      });
    };
    let opusCalls = 0;
    const vibeFails = argument === 'vibe-fails'
      || argument === 'both-fail'
      || argument === 'vibe-credential-fails';
    const qwenFails = argument === 'both-fail';
    const adapters = {
      vibe: async () => {
        if (!vibeFails) return providerRecord('mistral-vibe');
        const error = new Error('FERRY_SECRET_CANARY');
        if (argument === 'vibe-credential-fails') error.code = 'CREDENTIAL';
        throw error;
      },
      qwen: async () => {
        if (qwenFails) throw new Error('FERRY_SECRET_CANARY');
        return providerRecord('qwen');
      },
      opus: async () => { opusCalls += 1; },
    };
    const report = await runEnsemble({ prompt: 'fixture' }, adapters);
    writeJson(mode === 'ensemble-no-opus' ? { ...report, opus_calls: opusCalls } : report);
    break;
  }
  case 'review-verification': {
    if (argument !== 'three') throw new Error('invalid verification fixture');
    const commandsRun = [];
    const findings = ['a', 'b', 'c'].map((name) => ({
      description: `finding-${name}`,
      verification: {
        command: `verify-${name}`,
        confirms_if: `CONFIRM-${name}`,
        refutes_if: `REFUTE-${name}`,
      },
    }));
    const outputs = new Map([
      ['verify-a', 'CONFIRM-a'],
      ['verify-b', 'REFUTE-b'],
      ['verify-c', 'neither'],
    ]);
    const report = await verifyFindings(findings, {
      authorize: (command) => ({ authorized: true, argv: [command] }),
      run: async ({ argv }) => {
        commandsRun.push(argv[0]);
        return outputs.get(argv[0]);
      },
    });
    writeJson({ ...report, commands_run: commandsRun });
    break;
  }
  case 'review-authorize': {
    const commandIndex = process.argv.indexOf('--command');
    const command = commandIndex === -1 ? '' : process.argv[commandIndex + 1];
    const rootIndex = process.argv.indexOf('--root');
    const root = rootIndex === -1 ? process.cwd() : process.argv[rootIndex + 1];
    const report = authorizeVerificationCommand(command, { root });
    writeJson({ ...report, executed: false });
    if (!report.authorized) process.exitCode = 1;
    break;
  }
  case 'review-policy': {
    writeJson({
      chunk_10: codeGateRequired({
        nonDocsLines: 10, docsOnly: false, carveout: false, threshold: 10,
      }),
      chunk_11: codeGateRequired({
        nonDocsLines: 11, docsOnly: false, carveout: false, threshold: 10,
      }),
      ship_20: codeGateRequired({
        nonDocsLines: 20, docsOnly: false, carveout: false, threshold: 20,
      }),
      ship_21: codeGateRequired({
        nonDocsLines: 21, docsOnly: false, carveout: false, threshold: 20,
      }),
      docs_only: codeGateRequired({
        nonDocsLines: 100, docsOnly: true, carveout: false, threshold: 10,
      }),
      carveout: codeGateRequired({
        nonDocsLines: 1, docsOnly: true, carveout: true, threshold: 10,
      }),
      tier_1: contextTier({
        fullTokens: 10, expandedTokens: 20, perFileTokens: [30], limit: 10,
      }),
      tier_2: contextTier({
        fullTokens: 11, expandedTokens: 10, perFileTokens: [30], limit: 10,
      }),
      tier_3: contextTier({
        fullTokens: 11, expandedTokens: 11, perFileTokens: [9, 10], limit: 10,
      }),
      split: contextTier({
        fullTokens: 11, expandedTokens: 11, perFileTokens: [9, 11], limit: 10,
      }),
    });
    break;
  }
  case 'ensemble-verification': {
    if (argument !== 'dual-primary') throw new Error('invalid ensemble verification fixture');
    const commandsRun = [];
    const slots = {};
    for (const [slot, command] of [
      ['mistral-vibe', 'verify-vibe-slot'],
      ['qwen', 'verify-qwen-slot'],
    ]) {
      const report = await verifyFindings([{
        description: slot,
        verification: {
          command,
          confirms_if: 'CONFIRMED',
          refutes_if: 'REFUTED',
        },
      }], {
        authorize: (value) => ({ authorized: true, argv: [value] }),
        run: async ({ argv }) => {
          commandsRun.push(argv[0]);
          return 'REFUTED';
        },
      });
      slots[slot] = report;
    }
    writeJson({
      slots,
      commands_run: commandsRun,
      verified_findings: Object.keys(slots),
    });
    break;
  }
  case 'post-fix-reverification': {
    const commandsRun = [];
    const finding = {
      description: 'fixed finding',
      verification: {
        command: 'verify-fixed-finding',
        confirms_if: 'CONFIRMED',
        refutes_if: 'REFUTED',
      },
    };
    const authorize = (command) => ({ authorized: true, argv: [command] });
    const first = await verifyFindings([finding], {
      authorize,
      run: async ({ argv }) => {
        commandsRun.push(argv[0]);
        return 'CONFIRMED';
      },
    });
    const second = await reverifyAfterFix(first.records, {
      run: async ({ argv }) => {
        commandsRun.push(argv[0]);
        return 'REFUTED';
      },
    });
    writeJson({
      fix_applied: true,
      commands_run: commandsRun,
      before: first.records[0].verdict,
      after: second.records[0].verdict,
      gate_ready: second.gate_ready,
    });
    break;
  }
  case 'plan-route': {
    let qwenCalls = 0;
    let opusCalls = 0;
    const route = await runPlanReview({
      request: { prompt: 'fixture' },
      inputSha256: reviewInputDigest('fixture'),
      adapters: {
        qwen: async () => {
          qwenCalls += 1;
          if (argument === 'qwen-fails') {
            const error = new Error('FERRY_SECRET_CANARY');
            error.code = 'CREDENTIAL';
            throw error;
          }
          return makeReviewRecord({
            adapter: 'qwen-api',
            slot: 'plan-qwen',
            requestedModel: 'qwen3.8-max',
            resolvedModel: 'qwen3.8-max',
            sessionId: 'plan-qwen-session',
            durationMs: 1,
            status: 'valid',
            result: { findings: [], summary: 'clean', confidence: 'high' },
          });
        },
        opus: async () => { opusCalls += 1; },
      },
    });
    const decision = evaluatePlanGate(route, [], reviewInputDigest('fixture'));
    writeJson({
      attempt_slots: route.attempts.map((record) => record.slot),
      qwen_calls: qwenCalls,
      opus_calls: opusCalls,
      accepted_model: route.accepted?.resolved_model ?? null,
      ready: decision.ready,
    });
    if (!decision.ready) process.exitCode = 1;
    break;
  }
  case 'plan-stale-route': {
    const inputSha256 = 'a'.repeat(64);
    const record = makeReviewRecord({
      adapter: 'qwen-api',
      slot: 'plan-qwen',
      requestedModel: 'qwen3.8-max',
      resolvedModel: 'qwen3.8-max',
      sessionId: 'old-plan-session',
      durationMs: 1,
      status: 'valid',
      result: { findings: [], summary: 'clean', confidence: 'high' },
    });
    const decision = evaluatePlanGate({
      accepted: record,
      attempts: [record],
      input_sha256: inputSha256,
    }, [], 'b'.repeat(64));
    writeJson(decision);
    if (!decision.ready) process.exitCode = 1;
    break;
  }
  case 'plan-revision-loop': {
    let qwenCalls = 0;
    let opusCalls = 0;
    const sessionIds = [];
    const attemptSlots = [];
    const withheldVersions = [];
    let approvalVersion = null;
    for (const version of [1, 2]) {
      const route = await runPlanReview({
        request: { prompt: `version ${version}` },
        inputSha256: reviewInputDigest(`version ${version}`),
        adapters: {
          qwen: async () => {
            qwenCalls += 1;
            const finding = {
              severity: 'important',
              category: 'correctness',
              file: 'docs/plan.md',
              line: null,
              description: 'finding',
              suggestion: 'revise',
              verification: {
                command: 'rg -n -- finding docs/plan.md',
                confirms_if: 'present',
                refutes_if: 'absent',
              },
            };
            return makeReviewRecord({
              adapter: 'qwen-api',
              slot: 'plan-qwen',
              requestedModel: 'qwen3.8-max',
              resolvedModel: 'qwen3.8-max',
              sessionId: `plan-qwen-${version}`,
              durationMs: 1,
              status: 'valid',
              result: {
                findings: version === 1 ? [finding] : [],
                summary: version === 1 ? 'revise' : 'clean',
                confidence: 'high',
              },
            });
          },
          opus: async () => { opusCalls += 1; },
        },
      });
      attemptSlots.push(route.attempts.map((record) => record.slot));
      sessionIds.push(...route.attempts.map((record) => record.session_id));
      const decision = evaluatePlanGate(
        route,
        version === 1 ? ['CONFIRMED'] : [],
        reviewInputDigest(`version ${version}`),
      );
      if (decision.ready) approvalVersion = version;
      else withheldVersions.push(version);
    }
    writeJson({
      accepted_reviews: qwenCalls,
      attempt_slots: attemptSlots,
      session_ids: sessionIds,
      withheld_versions: withheldVersions,
      approval_version: approvalVersion,
      opus_calls: opusCalls,
    });
    break;
  }
  case 'bootstrap': {
    const fixtureArgs = process.argv.slice(3);
    const has = (name) => fixtureArgs.includes(name);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const root = option('--root');
    await runBootstrap({
      home: option('--home'),
      root,
      dryRun: has('--dry-run'),
      canonicalRoot: option('--canonical-root') ?? root,
      proton: async () => {},
      beforeRename: has('--fail-before-rename')
        ? () => { throw new Error('injected stop'); }
        : null,
      runtime: async () => ({ fixture: true }),
    });
    writeJson({ ok: true });
    break;
  }
  case 'bootstrap-proton': {
    const fixtureArgs = process.argv.slice(3);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const home = option('--home');
    const root = option('--root');
    await runBootstrap({
      home,
      root,
      canonicalRoot: root,
      proton: async () => provisionReviewerAgent({
        home,
        passCli: option('--pass-cli'),
      }),
      runtime: async () => ({ fixture: true }),
    });
    writeJson({ ok: true });
    break;
  }
  case 'readiness-static': {
    const fixture = argument;
    const fixtureArgs = process.argv.slice(3);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const root = option('--root');
    const home = option('--home');
    const projectConfig = [
      fixture === 'commented-model'
        ? '# model = "gpt-5.6-sol"'
        : 'model = "gpt-5.6-sol"',
      'model_reasoning_effort = "high"',
      'approval_policy = "on-request"',
      'sandbox_mode = "workspace-write"',
      'web_search = "disabled"',
      '[mcp_servers.qmd]',
      '[mcp_servers.serena]',
      ...(fixture === 'missing-tool-server' ? [] : ['[mcp_servers.context7]']),
    ].join('\n');
    const hooks = fixture === 'missing-hook'
      ? { hooks: { Stop: [], SubagentStop: [] } }
      : fixture === 'misdistributed-hook'
        ? {
            hooks: {
              Stop: [{
                hooks: [
                  { command: 'plain-english-chat-hook.mjs', timeout: 60 },
                  { command: 'plain-english-chat-hook.mjs', timeout: 60 },
                ],
              }],
              SubagentStop: [],
            },
          }
        : {
          hooks: {
            Stop: [{ hooks: [{ command: 'plain-english-chat-hook.mjs', timeout: 60 }] }],
            SubagentStop: [{ hooks: [{ command: 'plain-english-chat-hook.mjs', timeout: 60 }] }],
          },
        };
    const trust = fixture === 'single-quoted-trust'
      ? `[projects.'${root}']\ntrust_level = "trusted"\n`
      : `[projects.${JSON.stringify(root)}]\ntrust_level = "trusted"\n`;
    const entries = new Map([
      [`${root}/.codex/config.toml`, projectConfig],
      [`${home}/.codex/config.toml`, trust],
      [`${root}/AGENTS.md`, '# Host Compatibility\n'],
      [`${root}/.codex/hooks.json`, JSON.stringify(hooks)],
      [`${root}/.claude/skills/df-start/SKILL.md`, '# start\n'],
      [`${root}/.agents/skills/df-start/SKILL.md`, '# start\n'],
      [`${root}/.worktreeinclude`, 'CLAUDE.md\n'],
      [`${root}/.claude/scripts/new-worktree.sh`, '#!/bin/sh\n'],
    ]);
    const roles = ['coordinator.toml', 'reviewer.toml', 'explorer.toml', 'locator.toml'];
    for (const role of roles) entries.set(`${root}/.codex/agents/${role}`, 'model = "test"\n');
    if (fixture === 'missing-role') entries.delete(`${root}/.codex/agents/reviewer.toml`);
    if (fixture === 'missing-skill-bridge') {
      entries.delete(`${root}/.agents/skills/df-start/SKILL.md`);
    }
    const report = await runStaticReadiness({
      root,
      home,
      canonicalRoot: root,
      files: {
        exists: (path) => entries.has(path),
        readText: (path) => {
          if (!entries.has(path)) throw new Error('missing fixture path');
          return entries.get(path);
        },
        list: (path) => {
          if (path === `${root}/.claude/skills`) return ['df-start'];
          return [];
        },
      },
      command: (name) => {
        if (fixture === 'missing-codex' && name === 'codex') return { status: 1, stdout: '' };
        if (fixture === 'missing-client' && name === 'qwen') return { status: 1, stdout: '' };
        return { status: 0, stdout: `${name} fixture-version` };
      },
      now: () => 0,
      runtimeCheck: async () => ({ release: 'fixture', files: 7 }),
    });
    writeJson(report);
    if (report.overall !== 'ready') process.exitCode = 1;
    break;
  }
  case 'reviewer-runtime':
  case 'reviewer-runtime-fixture':
  case 'reviewer-runtime-check': {
    const fixtureArgs = process.argv.slice(3);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const home = option('--home');
    const root = option('--root') ?? process.cwd();
    const version = option('--version') ?? 'real';
    const files = mode === 'reviewer-runtime-fixture'
      ? Object.fromEntries([
          'review-contract.mjs', 'proton-credential.mjs', 'vibe-review.mjs',
          'qwen-review.mjs', 'claude-review.mjs', 'review-ensemble.mjs',
          'review-verification.mjs',
        ].map((name) => [name, `${name}:${version}\n`]))
      : undefined;
    try {
      if (mode === 'reviewer-runtime-check') {
        writeJson(verifyReviewerRuntime({ home, root }));
      } else {
        const report = installReviewerRuntime({
          home,
          root,
          files,
          beforeActivate: fixtureArgs.includes('--fail-before-activate')
            ? () => { throw new Error('injected stop'); }
            : null,
        });
        writeJson(report);
      }
    } catch (error) {
      process.stderr.write(`${error.message}\n`);
      process.exitCode = 1;
    }
    break;
  }
  case 'readiness-live': {
    const fixture = argument;
    const doctor = {
      codexVersion: '0.149.1',
      checks: {
        'auth.credentials': { status: 'ok' },
        installation: { status: 'ok' },
        'config.load': { status: 'ok' },
        'git.environment': { status: 'ok' },
        'sandbox.helpers': { status: 'ok' },
      },
    };
    const doctorFailure = {
      'doctor-auth-fail': 'auth.credentials',
      'doctor-install-fail': 'installation',
      'doctor-config-fail': 'config.load',
      'doctor-git-fail': 'git.environment',
      'doctor-sandbox-fail': 'sandbox.helpers',
    }[fixture];
    if (doctorFailure) doctor.checks[doctorFailure].status = 'fail';
    const report = await runLiveReadiness({
      root: process.cwd(),
      command: (name, args) => {
        if (name === 'codex' && args[0] === 'doctor') {
          return { status: 0, stdout: JSON.stringify(doctor) };
        }
        return { status: 0, stdout: '' };
      },
      providerProbe: async () => {
        if (fixture === 'provider-timeout') throw new Error('provider timeout');
        return {
          marker: 'FERRY_CODEX_RUNTIME_OK',
          model: 'gpt-5.6-sol',
          qmd: 'ok',
          serena: 'ok',
          context7: fixture === 'missing-tool-server' ? 'missing' : 'ok',
        };
      },
      updateProbe: async () => {
        if (fixture === 'doctor-network-timeout') throw new Error('network timeout');
        if (fixture === 'stale-version') {
          return { current: false, installed: '0.149.1', latest: '0.150.0' };
        }
        return { current: true };
      },
      now: () => 0,
    });
    writeJson(report);
    if (report.overall !== 'ready') process.exitCode = 1;
    break;
  }
  case 'readiness-reviewers': {
    const fixture = argument;
    const valid = (adapter, slot, model) => makeReviewRecord({
      adapter,
      slot,
      requestedModel: model,
      resolvedModel: model,
      sessionId: `${adapter}-fixture-session`,
      durationMs: 4,
      status: 'valid',
      result: { findings: [], summary: 'ready', confidence: 'high' },
    });
    const adapters = {
      vibe: async () => {
        if (fixture === 'vibe-fails' || fixture === 'all-fail-canary') {
          throw new Error('FERRY_SECRET_CANARY');
        }
        return valid('vibe', 'mistral-vibe', 'zai-glm-5-2');
      },
      qwen: async () => {
        if (fixture === 'qwen-fails' || fixture === 'all-fail-canary') {
          throw new Error('FERRY_SECRET_CANARY');
        }
        if (fixture === 'qwen-wrong-model') {
          return valid('qwen', 'qwen', 'qwen3.6-flash');
        }
        return valid('qwen', 'qwen', 'qwen3.8-max');
      },
    };
    const report = await runReviewerReadiness({
      root: process.cwd(),
      home: '/fixture-home',
      adapters,
      now: () => 0,
    });
    writeJson(report);
    if (report.overall !== 'ready') process.exitCode = 1;
    break;
  }
  case 'generated-host-secret-scan': {
    const fixture = argument;
    const canary = `sk-${'FERRY_SECRET_CANARY'.repeat(3)}`;
    const files = fixture === 'clean'
      ? [
        { host: 'qwen', content: JSON.stringify({
          modelProviders: { openai: [{ envKey: 'BAILIAN_TOKEN_PLAN_API_KEY' }] },
        }) },
        { host: 'codex', content: 'model = "gpt-5.6-sol"\n' },
        { host: 'vibe', content: 'api_key_env_var = "MISTRAL_API_KEY"\n' },
      ]
      : [{
        host: fixture,
        content: fixture === 'qwen'
          ? JSON.stringify({ env: { BAILIAN_TOKEN_PLAN_API_KEY: canary } })
          : `credential = "${canary}"\n`,
      }];
    const violations = generatedHostSecretViolations(files);
    writeJson({ violations });
    if (violations.length) process.exitCode = 1;
    break;
  }
  case 'verify-all': {
    const fixture = argument;
    const commands = [];
    const failedLayer = fixture.startsWith('fail-') ? fixture.slice(5) : null;
    const report = await runVerificationLayers({
      root: process.cwd(),
      markdownFiles: ['CHANGELOG.md'],
      helperFiles: ['scripts/agent-compat/review-contract.mjs'],
      run: async (command, args, options) => {
        commands.push([command, ...args]);
        return { status: options.layer === failedLayer ? 1 : 0 };
      },
      now: () => 0,
    });
    writeJson({
      ...report,
      commands,
      self_test_detection: {
        helper: advertisesSelfTest("function selfTest() {}\nif (args.includes('--self-test')) {}"),
        aggregate: advertisesSelfTest("commands.push(['file', '--self-test'])"),
      },
    });
    if (report.failed_layer) process.exitCode = 1;
    break;
  }
  case 'readiness-worktree': {
    const fixture = argument;
    const evidence = {
      primary: {
        markers: ['instructions', 'skill', 'qmd'],
        tree_hash_before: 'primary-hash',
        tree_hash_after: 'primary-hash',
      },
      worktree: {
        markers: ['instructions', 'skill', 'qmd'],
        tree_hash_before: 'worktree-hash',
        tree_hash_after: 'worktree-hash',
      },
      hooks: {
        session_start: 'ok',
        pre_tool_allow: 'ok',
        pre_tool_block: 'ok',
        post_tool: 'ok',
        stop_main: { status: 'ok', timeout_seconds: 60, duration_ms: 20 },
        stop_child: { status: 'ok', timeout_seconds: 60, duration_ms: 20 },
      },
      roles: {
        coordinator: { model: 'gpt-5.6-sol', sandbox: 'workspace-write', selected: true },
        reviewer: { model: 'gpt-5.6-terra', sandbox: 'read-only', selected: true },
        explorer: { model: 'gpt-5.6-terra', sandbox: 'read-only', selected: true },
        locator: { model: 'gpt-5.6-luna', sandbox: 'read-only', selected: true },
      },
    };
    if (fixture === 'missing-links') evidence.worktree.markers = ['instructions'];
    if (fixture === 'hook-nonzero') evidence.hooks.post_tool = 'failed';
    if (fixture === 'stop-ten-second') evidence.hooks.stop_main.timeout_seconds = 10;
    if (fixture === 'stop-timeout') evidence.hooks.stop_child.duration_ms = 59_000;
    if (fixture === 'wrong-role') evidence.roles.locator.model = 'gpt-5.6-terra';
    const report = await runWorktreeReadiness({
      root: process.cwd(),
      probe: async () => evidence,
      now: () => 0,
    });
    writeJson(report);
    if (report.overall !== 'ready') process.exitCode = 1;
    break;
  }
  default:
    process.stderr.write(`agent_compat_runner: unknown mode "${mode}"\n`);
    process.exitCode = 1;
}
