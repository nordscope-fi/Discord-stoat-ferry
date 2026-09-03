#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { dispatchCodexPreTool } from '../../scripts/agent-compat/codex-hook-adapter.mjs';
import {
  installReviewerRuntime,
  provisionReviewerAgent,
  runBootstrap,
  verifyReviewerRuntime,
} from '../../scripts/agent-compat/codex-bootstrap.mjs';
import {
  runBrainstormEvidenceProbe,
  runLiveReadiness,
  runReviewerReadiness,
  runStaticReadiness,
  runWorktreeReadiness,
} from '../../scripts/agent-compat/codex-readiness.mjs';
import {
  checkPlainEnglishState,
  generatedHostSecretViolations,
  runPlainEnglishInit,
} from '../../scripts/agent-compat/check.mjs';
import {
  HOOK_PARITY,
  codexPostToolMatcher,
  qwenPostToolMatcher,
  routesFor,
  vibePostToolMatcher,
} from '../../scripts/agent-compat/hook-parity.mjs';
import {
  UPSTREAM_CODEX_CHAT_COMMAND,
  canonicalCheckoutRoot,
  codexChatCommand,
  normalizeCodexChatHooks,
  plainEnglishFailure,
  probePlainEnglish,
  removeUnusedIssueChannel,
} from '../../scripts/agent-compat/plain-english-contract.mjs';
import {
  evaluatePlanGate,
  makeReviewRecord,
  planFindingDigest,
  reviewInputDigest,
  safeChildFailure,
  validateFindings,
} from '../../scripts/agent-compat/review-contract.mjs';
import {
  claimPlanReviewAttempt,
  parseArgs as parseReviewArgs,
  runBudgetedPlanReview,
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
import {
  parseQwenResponse,
  requestQwen,
  runQwenReview,
} from '../../scripts/agent-compat/qwen-review.mjs';
import * as qwenReview from '../../scripts/agent-compat/qwen-review.mjs';
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
  const brainstormEntries = HOOK_PARITY
    .filter(entry => entry.id.startsWith('project.brainstorm-'))
    .map(entry => ({
      id: entry.id,
      event: entry.event,
      codex_disposition: entry.disposition,
      vibe_disposition: entry.vibeDisposition,
      qwen_disposition: entry.qwenDisposition,
    }));
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
    brainstorm_entries: brainstormEntries,
    brainstorm_codex_external_before: routesFor(
      'PreToolUse',
      'mcp__context7__query-docs',
      'codex',
    ),
    brainstorm_codex_external_after: routesFor(
      'PostToolUse',
      'mcp__context7__query-docs',
      'codex',
    ),
    brainstorm_codex_post_matcher: codexPostToolMatcher(),
    vibe_post_matcher: vibePostToolMatcher(),
    qwen_post_matcher: qwenPostToolMatcher(),
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
  const evidence = new Set(['brainstorm-evidence']);
  writeJson({
    plain_english_children: routes.filter(route => plainEnglish.has(route)).length,
    evidence_children: routes.filter(route => evidence.has(route)),
    security_children: routes.filter(route =>
      !plainEnglish.has(route) && !evidence.has(route)),
    duration_ms: durationMs,
  });
}

function nativeChatDocument(command, timeout = 10) {
  const group = () => ({
    matcher: '*',
    hooks: [{ type: 'command', command, timeout }],
  });
  return {
    hooks: {
      Issue: [{ matcher: 'mcp__linear__', hooks: [] }],
      Stop: [group()],
      SubagentStop: [group()],
    },
  };
}

function plainEnglishContract(fixture) {
  const results = {
    accepted: { status: 0, stdout: '1.0.0\n', stderr: '' },
    missing: { status: null, stdout: '', stderr: '', error: { code: 'ENOENT' } },
    failed: { status: 1, stdout: '', stderr: 'failed' },
    empty: { status: 0, stdout: '', stderr: '' },
    prefixed: { status: 0, stdout: 'plain-english 1.0.0\n', stderr: '' },
    old: { status: 0, stdout: '0.24.1\n', stderr: '' },
    'near-miss': { status: 0, stdout: '1.0.1\n', stderr: '' },
    prerelease: { status: 0, stdout: '1.0.0-beta.1\n', stderr: '' },
  };
  if (!Object.hasOwn(results, fixture)) throw new Error(`unknown version fixture: ${fixture}`);
  const result = probePlainEnglish({ run: () => results[fixture] });
  writeJson({ ...result, message: result.status === 'accepted' ? null : plainEnglishFailure(result) });
}

function hookRegeneration(shape) {
  const ownerIndex = process.argv.indexOf('--owner');
  const owner = ownerIndex === -1 ? null : process.argv[ownerIndex + 1];
  if (!owner) throw new Error('--owner is required');
  const command = shape === 'altered-command'
    ? `${UPSTREAM_CODEX_CHAT_COMMAND} --changed`
    : UPSTREAM_CODEX_CHAT_COMMAND;
  const timeout = shape === 'timeout-30' ? 30 : 10;
  const runsIndex = process.argv.indexOf('--runs');
  const runs = runsIndex === -1 ? 1 : Number(process.argv[runsIndex + 1]);
  const document = nativeChatDocument(command, timeout);
  removeUnusedIssueChannel(document);
  let previous = null;
  for (let index = 0; index < runs; index += 1) {
    normalizeCodexChatHooks(document, owner);
    const current = JSON.stringify(document);
    if (previous !== null && current !== previous) throw new Error('hook regeneration drifted');
    previous = current;
  }
  const hooks = ['Stop', 'SubagentStop'].flatMap((event) =>
    document.hooks[event].flatMap((group) => group.hooks));
  writeJson({
    hook_count: hooks.length,
    commands: hooks.map((hook) => hook.command),
    timeouts: hooks.map((hook) => hook.timeout),
  });
}

function hookCommandExecution() {
  const baseIndex = process.argv.indexOf('--base');
  const base = baseIndex === -1 ? null : process.argv[baseIndex + 1];
  if (!base) throw new Error('--base is required');
  const ownerToken = "a b'c$HOME$(printf expanded)*?[ab]";
  const owner = join(base, ownerToken);
  const launcher = join(owner, '.codex', 'hooks', 'plain-english.mjs');
  mkdirSync(join(owner, '.codex', 'hooks'), { recursive: true });
  writeFileSync(
    launcher,
    "process.stdout.write(JSON.stringify(process.argv.slice(2)));\n",
  );
  for (const token of ['star', 'question', 'class-a', 'class-b']) {
    mkdirSync(join(base, token), { recursive: true });
  }
  const document = nativeChatDocument(UPSTREAM_CODEX_CHAT_COMMAND);
  normalizeCodexChatHooks(document, owner);
  const command = document.hooks.Stop[0].hooks[0].command;
  const eventCwd = join(base, 'event');
  mkdirSync(eventCwd);
  const result = spawnSync('/bin/sh', ['-c', command], {
    cwd: eventCwd,
    encoding: 'utf8',
    stdio: 'pipe',
  });
  writeJson({
    command,
    launcher,
    status: result.status,
    stdout: result.stdout,
    stderr: result.stderr,
  });
}

function stagedPlainEnglishTimeout(targetAgent) {
  const baseIndex = process.argv.indexOf('--base');
  const base = baseIndex === -1 ? null : process.argv[baseIndex + 1];
  if (!base) throw new Error('--base is required');
  const owner = join(base, 'generated-owner');
  const codexHooksPath = join(owner, '.codex', 'hooks.json');
  mkdirSync(join(owner, '.codex'), { recursive: true });
  writeFileSync(codexHooksPath, '{}\n');
  const calls = [];
  let timeoutMs = null;
  let killSignal = null;
  let comparisons = 0;
  checkPlainEnglishState({
    codexHooksPath,
    stageParent: base,
    instructions: '# Fixture instructions\n',
    runInit(stage, agent, options) {
      calls.push(agent);
      if (agent !== targetAgent) return true;
      return runPlainEnglishInit(stage, agent, {
        timeoutMs: options.timeoutMs,
        run(command, args, spawnOptions) {
          timeoutMs = spawnOptions.timeout;
          killSignal = spawnOptions.killSignal;
          return { status: null, stdout: '', stderr: '', error: { code: 'ETIMEDOUT' } };
        },
      });
    },
    compareCodex() {
      comparisons += 1;
    },
    compareVibe() {
      comparisons += 1;
    },
  });
  rmSync(owner, { recursive: true, force: true });
  writeJson({
    calls,
    timeout_ms: timeoutMs,
    kill_signal: killSignal,
    comparisons,
    stage_removed: readdirSync(base).length === 0,
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

function installPlainEnglishFixture(source, fakeBin) {
  const command = join(fakeBin, 'plain-english');
  cpSync(join(source, 'tests', 'fixtures', 'plain_english_cli_fixture.cjs'), command);
  chmodSync(command, 0o755);
}

function installLinkedHosts() {
  const baseIndex = process.argv.indexOf('--base');
  const base = baseIndex === -1 ? null : process.argv[baseIndex + 1];
  if (!base) throw new Error('--base is required');
  const repo = process.cwd();
  const primary = join(base, 'primary');
  const checkout = join(base, 'checkout');
  const home = join(base, 'home');
  const fakeBin = join(base, 'bin');
  mkdirSync(primary, { recursive: true });
  mkdirSync(checkout, { recursive: true });
  mkdirSync(home, { recursive: true });
  mkdirSync(fakeBin, { recursive: true });
  installPlainEnglishFixture(repo, fakeBin);
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
      env: {
        ...process.env,
        HOME: home,
        PATH: `${fakeBin}:${process.env.PATH}`,
        FERRY_PLAIN_ENGLISH_VERSION: '1.0.0',
      },
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
  cpSync(join(source, 'CLAUDE.md'), join(root, 'CLAUDE.md'));
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
reviewer_state="$HOME/.config/fake-reviewer-agent"
context7_state="$HOME/.config/fake-context7-agent"
if [ "$1 $2 $3" = "agent create --help" ]; then
  printf 'NAME --expiration 3m --vault\\n'
elif [ "$1 $2 $3 $4" = "agent access grant --help" ]; then
  printf '%s\\n' '--item-title --role'
elif [ "$1 $2" = "vault list" ]; then
  printf '[{"name":"PortalPilot"},{"name":"Personal"}]\\n'
elif [ "$1 $2" = "item list" ]; then
  printf '[{"title":"Context7 API Key"}]\\n'
elif [ "$1 $2" = "agent list" ]; then
  printf '['
  separator=''
  if [ -f "$reviewer_state" ]; then
    printf '{"id":"reviewer-id","name":"discord-ferry-reviewers","expire_time":1999999999}'
    separator=','
  fi
  if [ -f "$context7_state" ]; then
    printf '%s{"pat_id":"context7-id","name":"discord-ferry-context7","expire_time":1999999999}' "$separator"
  fi
  printf ']\\n'
elif [ "$1 $2" = "agent create" ]; then
  mkdir -p "$HOME/.config"
  if [ "$3" = "discord-ferry-reviewers" ]; then
    : > "$reviewer_state"
    printf '{"token":"PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_12345678901234567890123456789012"}\\n'
  else
    : > "$context7_state"
    printf '{"token":"PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_abcdefghijklmnopqrstuvwxyz12345678901234","instruction":"fixture"}\\n'
  fi
elif [ "$1 $2 $3" = "agent access grant" ]; then
  exit 0
elif [ "$1 $2 $3" = "personal-access-token access list-access" ]; then
  printf '[{"type":"item","item_title":"Context7 API Key","role":"Viewer","share_id":"context7-item-share-id","item_id":"context7-item-id"}]\\n'
elif [ "$1" = "login" ]; then
  exit 0
elif [ "$1 $2" = "item view" ]; then
  printf 'FERRY_CONTEXT7_FIXTURE_VALUE\\n'
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
  installPlainEnglishFixture(source, fakeBin);
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
    PATH: `${join(home, '.local', 'bin')}:${fakeBin}:${process.env.PATH}`,
    FERRY_PLAIN_ENGLISH_VERSION: '1.0.0',
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
  case 'plain-english-contract':
    plainEnglishContract(argument);
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
  case 'hook-command-execution':
    hookCommandExecution();
    break;
  case 'plain-english-staged-timeout':
    stagedPlainEnglishTimeout(argument);
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
  case 'review-schema': {
    const outcome = (exitCode, contains = null, excludes = null) => ({
      exit_code: exitCode,
      stdout_contains: contains,
      stdout_excludes: excludes,
    });
    const finding = {
      severity: 'important',
      category: 'correctness',
      file: 'scripts/agent-compat/review-contract.mjs',
      line: null,
      description: 'fixture finding',
      suggestion: 'fixture suggestion',
      verification: {
        command: 'rg -n -- finding scripts/agent-compat/review-contract.mjs',
        confirms_if: outcome(0, 'finding'),
        refutes_if: outcome(1),
      },
    };
    const valid = (candidate) => validateFindings({
      findings: [candidate], summary: 'fixture', confidence: 'high',
    });
    writeJson({
      structured: valid(finding),
      prose: valid({
        ...finding,
        verification: {
          ...finding.verification,
          confirms_if: 'present',
          refutes_if: 'absent',
        },
      }),
      identical: valid({
        ...finding,
        verification: { ...finding.verification, refutes_if: outcome(0, 'finding') },
      }),
      overlapping: valid({
        ...finding,
        verification: {
          ...finding.verification,
          confirms_if: outcome(0),
          refutes_if: outcome(0, null, 'absent'),
        },
      }),
      exclusive_same_exit: valid({
        ...finding,
        verification: {
          ...finding.verification,
          confirms_if: outcome(0, 'finding'),
          refutes_if: outcome(0, null, 'finding'),
        },
      }),
      unreachable_non_search_exit: valid({
        ...finding,
        verification: {
          command: 'git status --short',
          confirms_if: outcome(0),
          refutes_if: outcome(1),
        },
      }),
      empty_text: valid({
        ...finding,
        verification: { ...finding.verification, confirms_if: outcome(0, '') },
      }),
      invalid_exit: valid({
        ...finding,
        verification: { ...finding.verification, confirms_if: outcome(2) },
      }),
    });
    break;
  }
  case 'review-provider-authorization': {
    const tmpIndex = process.argv.indexOf('--tmp');
    const tmp = tmpIndex === -1 ? null : process.argv[tmpIndex + 1];
    if (!tmp) throw new Error('review-provider-authorization requires --tmp');
    const checkout = join(tmp, 'checkout');
    const source = join(checkout, 'src');
    mkdirSync(source, { recursive: true });
    writeFileSync(join(source, 'inside.txt'), 'needle\n');
    const outside = join(tmp, 'outside.txt');
    writeFileSync(outside, 'needle\n');
    symlinkSync(outside, join(source, 'linked.txt'));
    const outcome = (exitCode, contains = null, excludes = null) => ({
      exit_code: exitCode,
      stdout_contains: contains,
      stdout_excludes: excludes,
    });
    const findingFor = (command) => ({
      severity: 'important',
      category: 'correctness',
      file: 'src/inside.txt',
      line: null,
      description: 'fixture finding',
      suggestion: 'fixture suggestion',
      verification: {
        command,
        confirms_if: outcome(0, 'needle'),
        refutes_if: outcome(0, null, 'needle'),
      },
    });
    const recordFor = (command, slot = 'plan-qwen') => makeReviewRecord({
      adapter: 'qwen-api',
      slot,
      requestedModel: 'qwen3.8-max',
      resolvedModel: 'qwen3.8-max',
      sessionId: 'authorization-fixture',
      durationMs: 1,
      status: 'valid',
      root: checkout,
      result: {
        findings: [findingFor(command)],
        summary: 'fixture',
        confidence: 'high',
      },
    });
    const commands = {
      safe: 'rg -n -- needle src/inside.txt',
      shell: 'rg -n -- needle src/inside.txt | sh',
      python: 'python -c print(1)',
      missing: 'rg -n -- needle src/missing.txt',
      outside: `rg -n -- needle ${outside}`,
      linked: 'rg -n -- needle src/linked.txt',
    };
    const recordValid = {};
    const commandAuthorized = {};
    for (const [name, command] of Object.entries(commands)) {
      try {
        recordFor(command);
        recordValid[name] = true;
      } catch (error) {
        recordValid[name] = false;
      }
      commandAuthorized[name] = authorizeVerificationCommand(command, { root: checkout }).authorized;
    }
    const unsafe = commands.shell;
    const qwen = async ({ slot }) => recordFor(unsafe, slot);
    const vibe = async ({ slot }) => makeReviewRecord({
      adapter: 'vibe',
      slot,
      requestedModel: 'zai-glm-5-2',
      resolvedModel: 'zai-glm-5-2',
      sessionId: 'clean-vibe',
      durationMs: 1,
      status: 'valid',
      root: checkout,
      result: { findings: [], summary: 'clean', confidence: 'high' },
    });
    const ensemble = await runEnsemble({ prompt: 'fixture' }, { vibe, qwen });
    const plan = await runPlanReview({
      request: { prompt: 'fixture' },
      adapters: { qwen },
      inputSha256: reviewInputDigest('authorization fixture'),
    });
    const commandsRun = [];
    const verification = await verifyFindings([
      findingFor(commands.safe),
      findingFor(unsafe),
    ], {
      root: checkout,
      run: async (authorization) => {
        commandsRun.push(authorization.argv[0]);
        return { status: 'completed', exit_code: 0, stdout: 'needle', stderr: '' };
      },
    });
    writeJson({
      record_valid: recordValid,
      command_authorized: commandAuthorized,
      commands_run: commandsRun,
      verdicts: verification.records.map((record) => record.verdict),
      ensemble_status: ensemble.slots.qwen.status,
      plan_status: plan.attempts[0].status,
      plan_accepted: plan.accepted !== null,
    });
    break;
  }
  case 'proton-field-descriptor': {
    const homeIndex = process.argv.indexOf('--home');
    const home = homeIndex === -1 ? null : process.argv[homeIndex + 1];
    if (!home) throw new Error('proton-field-descriptor requires --home');
    const credential = await import('../../scripts/agent-compat/proton-credential.mjs');
    const tokenDirectory = join(home, '.config', 'discord-ferry');
    mkdirSync(tokenDirectory, { recursive: true });
    const context7Token = `pst_${'c'.repeat(40)}`;
    const reviewerToken = `pst_${'r'.repeat(40)}`;
    writeFileSync(join(tokenDirectory, 'context7-agent.pat'), context7Token, { mode: 0o600 });
    writeFileSync(join(tokenDirectory, 'reviewer-agent.pat'), reviewerToken, { mode: 0o600 });
    const calls = [];
    const sessions = new Set();
    const run = async (command, args, options) => {
      if (command !== 'pass-cli') throw new Error('unexpected command');
      sessions.add(options.env.PROTON_PASS_SESSION_DIR);
      calls.push({ args, token: options.env.PROTON_PASS_PERSONAL_ACCESS_TOKEN });
      return { stdout: args[0] === 'item' ? 'FIXTURE_FIELD_VALUE\n' : '' };
    };
    await credential.readProtonField({
      tokenFile: 'context7-agent.pat',
      shareId: 'context7-share-id',
      itemId: 'context7-item-id',
      field: 'API Key',
      reason: 'Start Context7',
      home,
      run,
    });
    await credential.readReviewerField({
      itemTitle: 'Mistral Vibe API Key',
      reason: 'Review Ferry code',
      home,
      run,
    });
    const itemCalls = calls.filter(call => call.args[0] === 'item');
    writeJson({
      context7_args: itemCalls[0].args,
      context7_token_selected: itemCalls[0].token === context7Token,
      reviewer_vault: itemCalls[1].args[itemCalls[1].args.indexOf('--vault-name') + 1],
      reviewer_token_selected: itemCalls[1].token === reviewerToken,
      sessions_removed: [...sessions].every(path => !existsSync(path)),
    });
    break;
  }
  case 'proton-field': {
    const homeIndex = process.argv.indexOf('--home');
    const home = homeIndex === -1 ? null : process.argv[homeIndex + 1];
    if (!['canary-child-error', 'symlink-token'].includes(argument) || !home) {
      throw new Error('invalid proton field fixture');
    }
    const tokenDirectory = join(home, '.config', 'discord-ferry');
    mkdirSync(tokenDirectory, { recursive: true });
    const tokenPath = join(tokenDirectory, 'reviewer-agent.pat');
    if (argument === 'symlink-token') {
      const outsideToken = join(home, 'outside-agent.pat');
      writeFileSync(outsideToken, `pst_${'x'.repeat(40)}`, { mode: 0o600 });
      symlinkSync(outsideToken, tokenPath);
    } else {
      writeFileSync(tokenPath, `pst_${'x'.repeat(40)}`, { mode: 0o600 });
    }
    let childCalls = 0;
    try {
      await readReviewerField({
        itemTitle: 'Mistral Vibe API Key',
        reason: 'fixture',
        home,
        run: async () => {
          childCalls += 1;
          const error = new Error('FERRY_SECRET_CANARY');
          error.status = 23;
          error.stdout = 'FERRY_SECRET_CANARY';
          error.stderr = 'FERRY_SECRET_CANARY';
          throw error;
        },
      });
    } catch (error) {
      writeJson({ stage: error.stage ?? 'local', error: error.message, child_calls: childCalls });
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
  case 'qwen-request-contract': {
    let request = null;
    await requestQwen({
      apiKey: 'fixture-api-key',
      prompt: 'fixture prompt',
      fetcher: async (url, options) => {
        request = {
          url,
          authorization: options.headers.Authorization,
          body: JSON.parse(options.body),
        };
        const content = JSON.stringify({
          findings: [],
          summary: 'clean',
          confidence: 'high',
        });
        const stream = [
          `data: ${JSON.stringify({
            id: 'fixture-review',
            model: 'qwen3.8-max',
            choices: [{ delta: { content }, finish_reason: null }],
          })}\n\n`,
          `data: ${JSON.stringify({
            id: 'fixture-review',
            model: 'qwen3.8-max',
            choices: [{ delta: {}, finish_reason: 'stop' }],
          })}\n\n`,
          'data: [DONE]\n\n',
        ].join('');
        return {
          ok: true,
          body: new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(stream));
              controller.close();
            },
          }),
        };
      },
    });
    writeJson({
      url: request.url,
      authorization_is_bearer: request.authorization === 'Bearer fixture-api-key',
      request: {
        model: request.body.model,
        stream: request.body.stream ?? null,
        enable_thinking: request.body.enable_thinking ?? null,
        reasoning_effort: request.body.reasoning_effort ?? null,
        max_completion_tokens: request.body.max_completion_tokens ?? null,
        response_format_type: request.body.response_format?.type ?? null,
        schema_name: request.body.response_format?.json_schema?.name ?? null,
        strict: request.body.response_format?.json_schema?.strict ?? null,
        schema: request.body.response_format?.json_schema?.schema ?? null,
      },
    });
    break;
  }
  case 'qwen-stream': {
    const review = JSON.stringify({
      findings: [],
      summary: 'clean café',
      confidence: 'high',
    });
    const events = [
      `data: ${JSON.stringify({
        id: 'stream-session',
        model: 'qwen3.8-max',
        choices: [{ delta: { reasoning_content: 'private reasoning' }, finish_reason: null }],
      })}\r\n\r\n`,
      `data: ${JSON.stringify({
        id: 'stream-session',
        model: 'qwen3.8-max',
        choices: [{ delta: { content: review.slice(0, 23) }, finish_reason: null }],
      })}\n\n`,
      `data: ${JSON.stringify({
        id: 'stream-session',
        model: 'qwen3.8-max',
        choices: [{ delta: { content: review.slice(23) }, finish_reason: null }],
      })}\n\n`,
      `data: ${JSON.stringify({
        id: 'stream-session',
        model: 'qwen3.8-max',
        choices: [{ delta: {}, finish_reason: 'stop' }],
      })}\n\n`,
      'data: [DONE]\n\n',
    ];
    if (argument === 'missing-done') events.pop();
    if (argument === 'length') {
      events[3] = `data: ${JSON.stringify({
        id: 'stream-session',
        model: 'qwen3.8-max',
        choices: [{ delta: {}, finish_reason: 'length' }],
      })}\n\n`;
    }
    if (argument === 'missing-content') events.splice(1, 2);
    if (argument === 'invalid-event') events[0] = 'data: {\n\n';
    if (argument === 'wrong-then-right') {
      events.unshift(`data: ${JSON.stringify({
        id: 'wrong-session',
        model: 'other',
        choices: [{ delta: {}, finish_reason: null }],
      })}\n\n`);
    }
    const raw = events.join('');
    const bytes = new TextEncoder().encode(raw);
    const body = new ReadableStream({
      start(controller) {
        for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
        controller.close();
      },
    });
    try {
      const response = await requestQwen({
        apiKey: 'fixture-api-key',
        prompt: 'fixture prompt',
        fetcher: async () => ({ ok: true, status: 200, body }),
      });
      const parsed = parseQwenResponse(response);
      writeJson({
        ok: true,
        session_id: parsed.sessionId,
        result: parsed.result,
      });
    } catch (error) {
      writeJson({ ok: false, code: error.code ?? null, message: error.message });
    }
    break;
  }
  case 'qwen-schema-reason': {
    if (argument === 'declared') {
      writeJson({ reasons: qwenReview.QWEN_SCHEMA_FAILURE_REASONS ?? [] });
      break;
    }
    const clean = JSON.stringify({ findings: [], summary: 'clean', confidence: 'high' });
    const event = ({ model = 'qwen3.8-max', delta = {}, finishReason = null } = {}) =>
      `data: ${JSON.stringify({
        id: 'schema-session',
        ...(model === null ? {} : { model }),
        choices: [{ delta, finish_reason: finishReason }],
      })}\n\n`;
    const streamBody = (source) => {
      const bytes = new TextEncoder().encode(source);
      return new ReadableStream({
        start(controller) {
          controller.enqueue(bytes);
          controller.close();
        },
      });
    };
    const responseBody = (content) => ({
      id: 'schema-session',
      model: 'qwen3.8-max',
      choices: [{ message: { role: 'assistant', content } }],
    });
    try {
      if (argument === 'stream-body') {
        await qwenReview.readQwenStream(null);
      } else if (argument === 'invalid-event') {
        await qwenReview.readQwenStream(streamBody('data: {\n\n'));
      } else if (argument === 'missing-done') {
        await qwenReview.readQwenStream(streamBody([
          event({ delta: { content: clean } }),
          event({ finishReason: 'stop' }),
        ].join('')));
      } else if (argument === 'trailing-data') {
        await qwenReview.readQwenStream(streamBody([
          event({ delta: { content: clean } }),
          event({ finishReason: 'stop' }),
          'data: [DONE]\n\ntrailing',
        ].join('')));
      } else if (argument === 'missing-model') {
        await qwenReview.readQwenStream(streamBody([
          event({ model: null, delta: { content: clean } }),
          event({ model: null, finishReason: 'stop' }),
          'data: [DONE]\n\n',
        ].join('')));
      } else if (argument === 'length') {
        await qwenReview.readQwenStream(streamBody([
          event({ delta: { content: clean } }),
          event({ finishReason: 'length' }),
          'data: [DONE]\n\n',
        ].join('')));
      } else if (argument === 'missing-content') {
        await qwenReview.readQwenStream(streamBody([
          event(),
          event({ finishReason: 'stop' }),
          'data: [DONE]\n\n',
        ].join('')));
      } else if (argument === 'response-envelope') {
        parseQwenResponse(null);
      } else if (argument === 'response-json') {
        parseQwenResponse(responseBody('{'));
      } else if (argument === 'response-findings') {
        parseQwenResponse(responseBody('{"findings":[]}'));
      } else if (argument === 'response-unclassified') {
        await runQwenReview({
          prompt: 'fixture',
          home: process.cwd(),
          credential: async () => 'fixture-api-key',
          request: async () => {
            const error = new Error('FERRY_SECRET_CANARY');
            error.code = 'INVALID_SCHEMA';
            error.failureReason = 'FERRY_SECRET_CANARY';
            throw error;
          },
        });
      } else {
        throw new Error(`unknown Qwen schema-reason fixture: ${argument}`);
      }
      throw new Error(`Qwen schema-reason fixture did not reject: ${argument}`);
    } catch (error) {
      writeJson({
        code: error.code ?? null,
        failure_reason: error.failureReason ?? null,
      });
    }
    break;
  }
  case 'qwen-deadline': {
    const clean = JSON.stringify({ findings: [], summary: 'clean', confidence: 'high' });
    const event = (delta, finishReason = null) => `data: ${JSON.stringify({
      id: 'deadline-session',
      model: 'qwen3.8-max',
      choices: [{ delta, finish_reason: finishReason }],
    })}\n\n`;
    const streamResponse = (kind) => {
      let cancelled = false;
      return {
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            if (kind === 'partial-event') {
              const bytes = new TextEncoder().encode(event({ reasoning_content: 'unfinished' }));
              const emit = (index) => {
                if (cancelled) return;
                controller.enqueue(Uint8Array.of(bytes[index]));
                if (index + 1 < bytes.length) setTimeout(() => emit(index + 1), 2);
              };
              emit(0);
              return;
            }
          if (kind === 'idle' || kind === 'total-stream') {
            controller.enqueue(new TextEncoder().encode(event({ reasoning_content: 'started' })));
            return;
          }
          const chunks = [
            event({ reasoning_content: 'working' }),
            event({ content: clean }),
            event({}, 'stop'),
            'data: [DONE]\n\n',
          ];
          chunks.forEach((chunk, index) => setTimeout(() => {
            controller.enqueue(new TextEncoder().encode(chunk));
            if (index === chunks.length - 1) controller.close();
          }, index * 2));
          },
          cancel() { cancelled = true; },
        }),
      };
    };
    const fetcher = async (url, options) => {
      if (argument === 'connection') {
        return new Promise((resolve, reject) => {
          options.signal.addEventListener('abort', () => reject(options.signal.reason), {
            once: true,
          });
        });
      }
      return streamResponse(argument);
    };
    const review = runQwenReview({
      prompt: 'fixture prompt',
      home: process.cwd(),
      credential: argument === 'total-credential'
        ? async () => new Promise(() => {})
        : async () => 'fixture-api-key',
      request: (options) => requestQwen({ ...options, fetcher }),
      deadlines: {
        connectionMs: 5,
        idleMs: argument === 'total-stream' ? 100 : 5,
        totalMs: argument === 'total-credential' || argument === 'total-stream' ? 10 : 100,
      },
    });
    const outcome = await Promise.race([
      review.then((record) => ({ ok: true, record }), (error) => ({
        ok: false,
        code: error.code ?? null,
        deadline: error.deadline ?? null,
        duration_ms: error.durationMs ?? null,
      })),
      new Promise((resolve) => setTimeout(() => resolve({ ok: false, stuck: true }), 100)),
    ]);
    writeJson(outcome);
    break;
  }
  case 'qwen-failure-record': {
    const clean = JSON.stringify({ findings: [], summary: 'clean', confidence: 'high' });
    let tick = 100;
    const clock = { now: () => {
      tick += 25;
      return tick;
    } };
    const credential = async () => {
      if (argument !== 'credential') return 'fixture-api-key';
      const error = new Error('FERRY_SECRET_CANARY');
      error.code = 'CREDENTIAL';
      error.stderr = 'FERRY_SECRET_CANARY';
      throw error;
    };
    const request = async () => {
      if (argument === 'wrong-model') {
        return {
          id: 'wrong-model-session',
          model: 'other',
          choices: [{ message: { content: clean } }],
        };
      }
      if (argument === 'schema') {
        return {
          id: 'schema-session',
          model: 'qwen3.8-max',
          choices: [{ message: { content: '{"findings":[]}' } }],
        };
      }
      const error = new Error('FERRY_SECRET_CANARY');
      error.responseBody = 'FERRY_SECRET_CANARY';
      error.stdout = 'FERRY_SECRET_CANARY';
      error.stderr = 'FERRY_SECRET_CANARY';
      if (argument === 'timeout') {
        error.code = 'ETIMEDOUT';
        error.deadline = 'idle';
      } else if (argument.startsWith('http-')) {
        error.httpStatus = Number(argument.slice(5));
      }
      throw error;
    };
    const qwen = (options) => runQwenReview({
      ...options,
      home: process.cwd(),
      credential,
      request,
      clock,
    });
    const vibe = async ({ slot }) => makeReviewRecord({
      adapter: 'vibe',
      slot,
      requestedModel: 'zai-glm-5-2',
      resolvedModel: 'zai-glm-5-2',
      sessionId: 'vibe-session',
      durationMs: 1,
      status: 'valid',
      result: { findings: [], summary: 'clean', confidence: 'high' },
    });
    const advisory = await runEnsemble({ prompt: 'fixture' }, { vibe, qwen });
    const plan = await runPlanReview({
      request: { prompt: 'fixture' },
      adapters: { qwen },
      inputSha256: reviewInputDigest('fixture'),
    });
    writeJson({ advisory: advisory.slots.qwen, plan: plan.attempts[0] });
    break;
  }
  case 'provider-failure-reasons': {
    if (argument !== 'matrix') throw new Error('invalid provider failure-reason fixture');
    const clean = { findings: [], summary: 'clean', confidence: 'high' };
    const vibeRecord = () => makeReviewRecord({
      adapter: 'vibe',
      slot: 'mistral-vibe',
      requestedModel: 'zai-glm-5-2',
      resolvedModel: 'zai-glm-5-2',
      sessionId: 'vibe-session',
      durationMs: 1,
      status: 'valid',
      result: clean,
    });
    const qwenRecord = () => makeReviewRecord({
      adapter: 'qwen-api',
      slot: 'qwen',
      requestedModel: 'qwen3.8-max',
      resolvedModel: 'qwen3.8-max',
      sessionId: 'qwen-session',
      durationMs: 1,
      status: 'valid',
      result: clean,
    });
    const failedVibe = await runEnsemble({ prompt: 'fixture' }, {
      vibe: async () => ({ ...vibeRecord(), confidence: 'certain' }),
      qwen: async () => qwenRecord(),
    });
    const validProviders = await runEnsemble({ prompt: 'fixture' }, {
      vibe: async () => vibeRecord(),
      qwen: async () => qwenRecord(),
    });
    const failedSonnet = await runPlanReview({
      request: { prompt: 'fixture' },
      inputSha256: reviewInputDigest('fixture'),
      selectedProvider: 'sonnet',
      adapters: {
        sonnet: async () => ({
          ...makeReviewRecord({
            adapter: 'claude',
            slot: 'plan-sonnet',
            requestedModel: 'sonnet',
            resolvedModel: 'claude-sonnet-5',
            sessionId: 'sonnet-session',
            durationMs: 1,
            status: 'valid',
            result: clean,
          }),
          confidence: 'certain',
        }),
      },
    });
    writeJson({
      vibe_schema: failedVibe.slots['mistral-vibe'],
      sonnet_schema: failedSonnet.attempts[0],
      qwen_valid: failedVibe.slots.qwen,
      vibe_valid: validProviders.slots['mistral-vibe'],
    });
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
  case 'ensemble-no-sonnet':
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
    let sonnetCalls = 0;
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
      sonnet: async () => { sonnetCalls += 1; },
    };
    const report = await runEnsemble({ prompt: 'fixture' }, adapters);
    writeJson(mode === 'ensemble-no-sonnet' ? { ...report, sonnet_calls: sonnetCalls } : report);
    break;
  }
  case 'review-verification': {
    if (argument !== 'three') throw new Error('invalid verification fixture');
    const commandsRun = [];
    const findings = ['a', 'b', 'c'].map((name) => ({
      description: `finding-${name}`,
      verification: {
        command: `verify-${name}`,
        confirms_if: {
          exit_code: 0,
          stdout_contains: `CONFIRM-${name}`,
          stdout_excludes: null,
        },
        refutes_if: {
          exit_code: 0,
          stdout_contains: `REFUTE-${name}`,
          stdout_excludes: null,
        },
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
        return {
          status: 'completed',
          exit_code: 0,
          stdout: outputs.get(argv[0]),
          stderr: '',
        };
      },
    });
    writeJson({ ...report, commands_run: commandsRun });
    break;
  }
  case 'review-classification': {
    if (argument !== 'structured') throw new Error('invalid classification fixture');
    const outcome = (exitCode, contains = null, excludes = null) => ({
      exit_code: exitCode,
      stdout_contains: contains,
      stdout_excludes: excludes,
    });
    const cases = [
      {
        name: 'rg-match',
        command: 'rg -n -- needle scripts/agent-compat',
        confirms: outcome(0, 'needle'),
        refutes: outcome(1),
        result: { status: 'completed', exit_code: 0, stdout: 'needle', stderr: '' },
      },
      {
        name: 'rg-no-match',
        command: 'rg -n -- needle scripts/agent-compat',
        confirms: outcome(0, 'needle'),
        refutes: outcome(1),
        result: { status: 'completed', exit_code: 1, stdout: '', stderr: '' },
      },
      {
        name: 'non-search-nonzero',
        command: 'head -- pyproject.toml',
        confirms: outcome(1, 'needle'),
        refutes: outcome(0, null, 'needle'),
        result: { status: 'completed', exit_code: 1, stdout: 'needle', stderr: '' },
      },
      {
        name: 'search-error',
        command: 'git grep -n -- needle scripts/agent-compat',
        confirms: outcome(0, 'needle'),
        refutes: outcome(1),
        result: { status: 'completed', exit_code: 2, stdout: 'needle', stderr: '' },
      },
      {
        name: 'timeout',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(0, null, 'needle'),
        result: { status: 'failed', failure_class: 'timeout' },
      },
      {
        name: 'signal',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(0, null, 'needle'),
        result: { status: 'failed', failure_class: 'signal' },
      },
      {
        name: 'approval-denied',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(0, null, 'needle'),
        result: { status: 'completed', exit_code: 0, stdout: 'needle', stderr: '' },
        denied: true,
      },
      {
        name: 'stderr-only',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(1),
        result: { status: 'completed', exit_code: 0, stdout: '', stderr: 'needle' },
      },
      {
        name: 'both-match',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(0, null, 'missing'),
        result: { status: 'completed', exit_code: 0, stdout: 'needle', stderr: '' },
      },
      {
        name: 'neither-match',
        command: 'head -- pyproject.toml',
        confirms: outcome(0, 'needle'),
        refutes: outcome(1),
        result: { status: 'completed', exit_code: 0, stdout: 'other', stderr: '' },
      },
    ];
    const records = [];
    for (const candidate of cases) {
      const finding = {
        description: candidate.name,
        verification: {
          command: candidate.command,
          confirms_if: candidate.confirms,
          refutes_if: candidate.refutes,
        },
      };
      const report = await verifyFindings([finding], {
        authorize: () => candidate.denied
          ? { authorized: false, argv: [], reason: 'approval denied' }
          : { authorized: true, argv: candidate.command.split(' ') },
        run: async () => candidate.result,
      });
      records.push({
        name: candidate.name,
        verdict: report.records[0].verdict,
        result: report.records[0].result,
      });
    }
    writeJson(records);
    break;
  }
  case 'review-artifacts': {
    if (argument !== 'bounded') throw new Error('invalid artifact fixture');
    const tmpIndex = process.argv.indexOf('--tmp');
    const tmp = tmpIndex === -1 ? null : process.argv[tmpIndex + 1];
    if (!tmp) throw new Error('review-artifacts requires --tmp');
    const checkout = join(tmp, 'checkout');
    const artifacts = join(checkout, 'docs', 'plans', '.review', 'verification');
    mkdirSync(artifacts, { recursive: true });
    mkdirSync(join(artifacts, 'directory.json'));
    const finding = {
      description: 'artifact fixture',
      verification: {
        command: 'rg -n -- needle src/inside.txt',
        confirms_if: {
          exit_code: 0,
          stdout_contains: 'needle',
          stdout_excludes: null,
        },
        refutes_if: {
          exit_code: 1,
          stdout_contains: null,
          stdout_excludes: null,
        },
      },
    };
    mkdirSync(join(checkout, 'src'));
    writeFileSync(join(checkout, 'src', 'inside.txt'), 'needle\n');
    const findingFile = join(artifacts, 'finding.json');
    const resultFile = join(artifacts, 'result.json');
    writeFileSync(findingFile, JSON.stringify(finding));
    writeFileSync(resultFile, JSON.stringify({
      status: 'completed', exit_code: 0, stdout: 'needle', stderr: 'advisory',
    }));
    const malformed = join(artifacts, 'malformed.json');
    writeFileSync(malformed, '{');
    const oversized = join(artifacts, 'oversized.json');
    writeFileSync(oversized, 'x'.repeat(2_097_153));
    const exact = join(artifacts, 'exact.json');
    const findingJson = JSON.stringify(finding);
    writeFileSync(exact, findingJson + ' '.repeat(2_097_152 - findingJson.length));
    const outside = join(tmp, 'outside.json');
    writeFileSync(outside, JSON.stringify(finding));
    symlinkSync(outside, join(artifacts, 'linked.json'));
    const outsideDirectory = join(tmp, 'outside-directory');
    mkdirSync(outsideDirectory);
    writeFileSync(join(outsideDirectory, 'finding.json'), JSON.stringify(finding));
    symlinkSync(outsideDirectory, join(artifacts, 'linked-directory'));
    const verifier = join(process.cwd(), 'scripts', 'agent-compat', 'review-verification.mjs');
    const invoke = (...args) => spawnSync('node', [verifier, ...args, '--root', checkout], {
      encoding: 'utf8',
    });
    const authorization = invoke('--authorize-finding', findingFile);
    const classification = invoke(
      '--classify-files', '--finding-file', findingFile, '--result-file', resultFile,
    );
    const exactResult = invoke('--authorize-finding', exact);
    const rejected = {};
    for (const [name, file] of Object.entries({
      linked: join(artifacts, 'linked.json'),
      linked_directory: join(artifacts, 'linked-directory', 'finding.json'),
      outside,
      malformed,
      directory: join(artifacts, 'directory.json'),
      oversized,
    })) {
      rejected[name] = invoke('--authorize-finding', file).status !== 0;
    }
    const rawAuthorize = invoke('--authorize-command', finding.verification.command);
    const rawClassify = invoke('--classify-results', '--finding', JSON.stringify(finding));
    writeJson({
      authorization_status: authorization.status,
      authorization: authorization.status === 0 ? JSON.parse(authorization.stdout) : null,
      classification_status: classification.status,
      classification: classification.status === 0 ? JSON.parse(classification.stdout) : null,
      exact_limit_status: exactResult.status,
      rejected,
      raw_modes_rejected: rawAuthorize.status !== 0 && rawClassify.status !== 0,
    });
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
          confirms_if: {
            exit_code: 0,
            stdout_contains: 'CONFIRMED',
            stdout_excludes: null,
          },
          refutes_if: {
            exit_code: 0,
            stdout_contains: 'REFUTED',
            stdout_excludes: null,
          },
        },
      }], {
        authorize: (value) => ({ authorized: true, argv: [value] }),
        run: async ({ argv }) => {
          commandsRun.push(argv[0]);
          return { status: 'completed', exit_code: 0, stdout: 'REFUTED', stderr: '' };
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
        confirms_if: {
          exit_code: 0,
          stdout_contains: 'CONFIRMED',
          stdout_excludes: null,
        },
        refutes_if: {
          exit_code: 0,
          stdout_contains: 'REFUTED',
          stdout_excludes: null,
        },
      },
    };
    const authorize = (command) => ({ authorized: true, argv: [command] });
    const first = await verifyFindings([finding], {
      authorize,
      run: async ({ argv }) => {
        commandsRun.push(argv[0]);
        return { status: 'completed', exit_code: 0, stdout: 'CONFIRMED', stderr: '' };
      },
    });
    const second = await reverifyAfterFix(first.records, {
      run: async ({ argv }) => {
        commandsRun.push(argv[0]);
        return { status: 'completed', exit_code: 0, stdout: 'REFUTED', stderr: '' };
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
    let sonnetCalls = 0;
    let sonnetRecord = null;
    const recordFor = (provider) => makeReviewRecord({
      adapter: provider === 'qwen' ? 'qwen-api' : 'claude',
      slot: provider === 'qwen' ? 'plan-qwen' : 'plan-sonnet',
      requestedModel: provider === 'qwen' ? 'qwen3.8-max' : 'sonnet',
      resolvedModel: provider === 'qwen' ? 'qwen3.8-max' : 'claude-sonnet-5',
      sessionId: `plan-${provider}-session`,
      durationMs: 1,
      status: 'valid',
      result: { findings: [], summary: 'clean', confidence: 'high' },
    });
    const selectedProvider = argument.startsWith('sonnet') || argument === 'unselected-sonnet'
      ? 'sonnet'
      : 'qwen';
    const route = await runPlanReview({
      request: { prompt: 'fixture' },
      inputSha256: reviewInputDigest('fixture'),
      selectedProvider,
      adapters: {
        qwen: async () => {
          qwenCalls += 1;
          if (argument === 'qwen-fails') {
            const error = new Error('FERRY_SECRET_CANARY');
            error.code = 'CREDENTIAL';
            throw error;
          }
          if (argument === 'wrong-selected-model') {
            return { ...recordFor('qwen'), resolved_model: 'other' };
          }
          return recordFor('qwen');
        },
        sonnet: async (options) => {
          sonnetCalls += 1;
          sonnetRecord = options.record ?? null;
          if (argument === 'sonnet-fails') {
            const error = new Error('FERRY_SECRET_CANARY');
            error.code = 'ENOENT';
            throw error;
          }
          if (argument === 'sonnet-schema') {
            return { ...recordFor('sonnet'), confidence: 'certain' };
          }
          return recordFor('sonnet');
        },
      },
    });
    if (argument === 'unselected-sonnet') route.selected_provider = 'qwen';
    if (argument === 'mixed-attempts') route.attempts.push(recordFor('sonnet'));
    if (argument === 'substituted' && route.accepted) {
      route.accepted.substitution_for = 'plan-sonnet';
      route.accepted.substitution_reason = 'timeout';
    }
    if (argument === 'wrong-requested-model' && route.accepted) {
      route.accepted.requested_model = 'other';
    }
    const decision = evaluatePlanGate(route, [], reviewInputDigest('fixture'));
    writeJson({
      attempt_slots: route.attempts.map((record) => record.slot),
      qwen_calls: qwenCalls,
      sonnet_calls: sonnetCalls,
      sonnet_record: sonnetRecord,
      selected_provider: route.selected_provider ?? null,
      accepted_model: route.accepted?.resolved_model ?? null,
      failure_class: route.attempts[0]?.failure_class ?? null,
      automatic_sonnet_calls: route.automatic_sonnet_calls,
      owner_selected_sonnet_calls: route.owner_selected_sonnet_calls ?? null,
      ready: decision.ready,
    });
    if (!decision.ready) process.exitCode = 1;
    break;
  }
  case 'plan-budget': {
    const tmpIndex = process.argv.indexOf('--tmp');
    const tmp = tmpIndex === -1 ? null : process.argv[tmpIndex + 1];
    if (!tmp) throw new Error('plan-budget requires --tmp');
    const ledgerPath = join(tmp, 'plan-review-ledger.json');
    const planId = 'docs/plans/fixture.md';
    const inputSha256 = reviewInputDigest('fixture');
    let qwenCalls = 0;
    let sonnetCalls = 0;
    const adapters = {
      qwen: async () => {
        qwenCalls += 1;
        if (argument === 'failure-counts') {
          const error = new Error('timeout');
          error.code = 'ETIMEDOUT';
          throw error;
        }
        return makeReviewRecord({
          adapter: 'qwen-api',
          slot: 'plan-qwen',
          requestedModel: 'qwen3.8-max',
          resolvedModel: 'qwen3.8-max',
          sessionId: `qwen-${qwenCalls}`,
          durationMs: 1,
          status: 'valid',
          result: { findings: [], summary: 'clean', confidence: 'high' },
        });
      },
      sonnet: async () => {
        sonnetCalls += 1;
        return makeReviewRecord({
          adapter: 'claude',
          slot: 'plan-sonnet',
          requestedModel: 'sonnet',
          resolvedModel: 'claude-sonnet-5',
          sessionId: `sonnet-${sonnetCalls}`,
          durationMs: 1,
          status: 'valid',
          result: { findings: [], summary: 'clean', confidence: 'high' },
        });
      },
    };
    const run = (selectedProvider = 'qwen') => runBudgetedPlanReview({
      request: { prompt: 'fixture' },
      adapters,
      inputSha256,
      selectedProvider,
      planId,
      ledgerPath,
      root: tmp,
    });
    const rounds = [];
    let rejected = null;
    if (argument === 'started-counts') {
      rounds.push(claimPlanReviewAttempt({
        inputSha256,
        selectedProvider: 'qwen',
        planId,
        ledgerPath,
        root: tmp,
      }).round);
    } else {
      rounds.push((await run()).review_round);
    }
    try {
      rounds.push((await run(argument === 'provider-lock' ? 'sonnet' : 'qwen')).review_round);
    } catch (error) {
      rejected = error.message;
    }
    if (argument !== 'provider-lock') {
      try {
        await run();
      } catch (error) {
        rejected = error.message;
      }
    }
    writeJson({
      rounds,
      qwen_calls: qwenCalls,
      sonnet_calls: sonnetCalls,
      rejected,
      ledger: JSON.parse(readFileSync(ledgerPath, 'utf8')),
    });
    break;
  }
  case 'plan-budget-safety': {
    const tmpIndex = process.argv.indexOf('--tmp');
    const tmp = tmpIndex === -1 ? null : process.argv[tmpIndex + 1];
    if (!tmp) throw new Error('plan-budget-safety requires --tmp');
    const checkout = join(tmp, 'checkout');
    const outside = join(tmp, 'outside');
    mkdirSync(checkout, { recursive: true });
    mkdirSync(outside, { recursive: true });
    const planId = 'docs/plans/fixture.md';
    const inputSha256 = reviewInputDigest('fixture');
    if (argument === 'symlink-escape') {
      symlinkSync(outside, join(checkout, 'linked'), process.platform === 'win32' ? 'junction' : 'dir');
      let rejected = null;
      try {
        claimPlanReviewAttempt({
          inputSha256,
          selectedProvider: 'qwen',
          planId,
          ledgerPath: 'linked/review.json',
          root: checkout,
        });
      } catch (error) {
        rejected = error.message;
      }
      writeJson({
        rejected,
        outside_ledger_exists: existsSync(join(outside, 'review.json')),
      });
      break;
    }
    let qwenCalls = 0;
    const releases = [];
    const adapters = {
      qwen: async () => {
        const call = ++qwenCalls;
        await new Promise((resolveCall) => releases.push(resolveCall));
        return makeReviewRecord({
          adapter: 'qwen-api',
          slot: 'plan-qwen',
          requestedModel: 'qwen3.8-max',
          resolvedModel: 'qwen3.8-max',
          sessionId: `qwen-${call}`,
          durationMs: 1,
          status: 'valid',
          result: { findings: [], summary: 'clean', confidence: 'high' },
        });
      },
      sonnet: async () => null,
    };
    const options = {
      request: { prompt: 'fixture' },
      adapters,
      inputSha256,
      selectedProvider: 'qwen',
      planId,
      ledgerPath: join(checkout, 'review.json'),
      root: checkout,
    };
    const first = runBudgetedPlanReview(options);
    const second = runBudgetedPlanReview(options);
    releases[1]();
    await second;
    releases[0]();
    const outcomes = await Promise.allSettled([first, second]);
    writeJson({
      outcomes: outcomes.map((outcome) => outcome.status),
      attempts: JSON.parse(readFileSync(options.ledgerPath, 'utf8')).attempts,
    });
    break;
  }
  case 'plan-budget-gate': {
    const inputSha256 = reviewInputDigest('current plan');
    const planId = 'docs/plans/fixture.md';
    const finding = {
      severity: 'important',
      category: 'correctness',
      file: 'docs/plans/fixture.md',
      line: null,
      description: 'The plan keeps the review loop open.',
      suggestion: 'Cap the review count.',
      verification: {
        command: 'rg -n -- review scripts/agent-compat/review-contract.mjs',
        confirms_if: {
          exit_code: 0,
          stdout_contains: 'the unbounded instruction is present',
          stdout_excludes: null,
        },
        refutes_if: {
          exit_code: 1,
          stdout_contains: null,
          stdout_excludes: null,
        },
      },
    };
    const validRecord = makeReviewRecord({
      adapter: 'qwen-api',
      slot: 'plan-qwen',
      requestedModel: 'qwen3.8-max',
      resolvedModel: 'qwen3.8-max',
      sessionId: 'qwen-round-2',
      durationMs: 1,
      status: 'valid',
      result: { findings: [finding], summary: 'blocked', confidence: 'high' },
    });
    const failedRecord = {
      ...makeReviewRecord({
        adapter: 'qwen-api',
        slot: 'plan-qwen',
        requestedModel: 'qwen3.8-max',
        sessionId: null,
        durationMs: 4,
        status: 'failed',
      }),
      failure_class: 'schema',
      failure_stage: 'qwen-response',
      http_status: null,
      failure_reason: 'response-findings',
    };
    const failureVariants = new Set([
      'failure-advisory',
      'altered-failure-reason',
      'missing-failure-reason',
      'undeclared-record-failure-reason',
      'missing-record-failure-reason',
      'non-schema-record-failure-reason',
    ]);
    const selectedRecord = failureVariants.has(argument) ? failedRecord : validRecord;
    const round = failureVariants.has(argument) ? 1 : 2;
    const route = {
      policy: 'ferry-bounded-plan-v4',
      plan_id: planId,
      selected_provider: 'qwen',
      review_round: round,
      review_budget: 2,
      budget_remaining: 2 - round,
      attempts: [selectedRecord],
      accepted: selectedRecord.status === 'valid' ? selectedRecord : null,
      input_sha256: inputSha256,
      automatic_sonnet_calls: 0,
      owner_selected_sonnet_calls: 0,
    };
    const toLedgerAttempt = (record, attemptRound, digest) => ({
      round: attemptRound,
      input_sha256: digest,
      status: record.status,
      slot: record.slot,
      requested_model: record.requested_model,
      resolved_model: record.resolved_model,
      session_id: record.session_id,
      failure_class: record.failure_class ?? null,
      failure_reason: record.failure_reason ?? null,
      record_sha256: createHash('sha256').update(JSON.stringify(record)).digest('hex'),
    });
    const ledger = {
      policy: 'ferry-plan-review-budget-v2',
      plan_id: planId,
      selected_provider: 'qwen',
      attempts: round === 1
        ? [toLedgerAttempt(selectedRecord, 1, inputSha256)]
        : [
            toLedgerAttempt(validRecord, 1, 'b'.repeat(64)),
            toLedgerAttempt(selectedRecord, 2, inputSha256),
          ],
    };
    let ownerDecision = null;
    if (argument === 'accepted-risk' || argument === 'stale-risk') {
      ownerDecision = {
        decision: 'accept_recorded_risk',
        plan_id: planId,
        input_sha256: argument === 'stale-risk' ? 'c'.repeat(64) : inputSha256,
        review_round: 2,
        finding_sha256: planFindingDigest([finding]),
      };
    }
    if (argument === 'altered-route') {
      route.accepted.findings[0].description = 'altered after the ledger was written';
    }
    if (argument === 'altered-failure-reason') {
      ledger.attempts[0].failure_reason = 'response-json';
    }
    if (argument === 'missing-failure-reason') {
      delete ledger.attempts[0].failure_reason;
    }
    if (argument === 'undeclared-record-failure-reason') {
      selectedRecord.failure_reason = 'FERRY_SECRET_CANARY';
      ledger.attempts[0].failure_reason = selectedRecord.failure_reason;
      ledger.attempts[0].record_sha256 = createHash('sha256')
        .update(JSON.stringify(selectedRecord)).digest('hex');
    }
    if (argument === 'missing-record-failure-reason') {
      delete selectedRecord.failure_reason;
      delete ledger.attempts[0].failure_reason;
      ledger.attempts[0].record_sha256 = createHash('sha256')
        .update(JSON.stringify(selectedRecord)).digest('hex');
    }
    if (argument === 'non-schema-record-failure-reason') {
      selectedRecord.status = 'timed_out';
      selectedRecord.failure_class = 'timeout';
      selectedRecord.failure_stage = 'total-timeout';
      ledger.attempts[0].status = selectedRecord.status;
      ledger.attempts[0].failure_class = selectedRecord.failure_class;
      ledger.attempts[0].record_sha256 = createHash('sha256')
        .update(JSON.stringify(selectedRecord)).digest('hex');
    }
    const legacyVariants = new Set([
      'legacy-opus-slot',
      'legacy-opus-model',
      'legacy-ledger-policy',
    ]);
    if (argument === 'legacy-opus-slot') {
      selectedRecord.slot = 'plan-opus';
      ledger.attempts.at(-1).slot = selectedRecord.slot;
      ledger.attempts.at(-1).record_sha256 = createHash('sha256')
        .update(JSON.stringify(selectedRecord)).digest('hex');
    }
    if (argument === 'legacy-opus-model') {
      selectedRecord.requested_model = 'opus';
      selectedRecord.resolved_model = 'claude-opus-5';
      ledger.attempts.at(-1).requested_model = selectedRecord.requested_model;
      ledger.attempts.at(-1).resolved_model = selectedRecord.resolved_model;
      ledger.attempts.at(-1).record_sha256 = createHash('sha256')
        .update(JSON.stringify(selectedRecord)).digest('hex');
    }
    if (argument === 'legacy-ledger-policy') {
      ledger.policy = 'ferry-plan-review-budget-v1';
    }
    const decision = evaluatePlanGate(
      route,
      selectedRecord.status === 'valid'
        ? [argument === 'inconclusive' ? 'INCONCLUSIVE' : 'CONFIRMED']
        : [],
      inputSha256,
      { ledger, ownerDecision },
    );
    const ledgerOutputVariants = new Set([
      'failure-advisory',
      'altered-failure-reason',
      'missing-failure-reason',
    ]);
    writeJson(legacyVariants.has(argument)
      ? { decision, provider_calls: 0 }
      : failureVariants.has(argument)
      ? {
          decision,
          ...(ledgerOutputVariants.has(argument)
            ? { ledger_attempt: ledger.attempts.at(-1) }
            : {}),
        }
      : decision);
    break;
  }
  case 'plan-args': {
    const bounded = [
      '--plan-id',
      'docs/plans/fixture.md',
      '--plan-ledger',
      'docs/plans/.review/fixture-ledger.json',
    ];
    const variants = {
      default: ['--plan', ...bounded],
      sonnet: ['--plan', '--plan-provider', 'sonnet', ...bounded],
      'legacy-opus-provider': ['--plan', '--plan-provider', 'opus', ...bounded],
      invalid: ['--plan', '--plan-provider', 'other', ...bounded],
      'missing-id': ['--plan', '--plan-ledger', 'docs/plans/.review/fixture-ledger.json'],
      'missing-ledger': ['--plan', '--plan-id', 'docs/plans/fixture.md'],
      'without-plan-qwen': ['--plan-provider', 'qwen'],
      'without-plan-sonnet': ['--plan-provider', 'sonnet'],
    };
    try {
      const parsed = parseReviewArgs(variants[argument] ?? []);
      writeJson({
        ok: true,
        plan: parsed.plan,
        plan_provider: parsed.planProvider,
        plan_id: parsed.planId,
        plan_ledger: parsed.planLedger,
      });
    } catch (error) {
      writeJson({ ok: false, message: error.message });
      process.exitCode = 1;
    }
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
    let sonnetCalls = 0;
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
                command: 'rg -n -- finding scripts/agent-compat/review-contract.mjs',
                confirms_if: {
                  exit_code: 0,
                  stdout_contains: 'present',
                  stdout_excludes: null,
                },
                refutes_if: {
                  exit_code: 1,
                  stdout_contains: null,
                  stdout_excludes: null,
                },
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
          sonnet: async () => { sonnetCalls += 1; },
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
      sonnet_calls: sonnetCalls,
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
  case 'bootstrap-provisioners': {
    const { provisionAgentCredentials } = await import(
      '../../scripts/agent-compat/codex-bootstrap.mjs'
    );
    const calls = [];
    const report = await provisionAgentCredentials({
      home: '/fixture/home',
      passCli: '/fixture/pass-cli',
      reviewer: async (options) => {
        calls.push(['reviewer', options.home, options.passCli]);
        return { created: false, renewed: false };
      },
      context7: async (options) => {
        calls.push(['context7', options.home, options.passCli]);
        return { created: true, renewed: false, recovered: false };
      },
    });
    writeJson({ calls, report });
    break;
  }
  case 'bootstrap-claude-handoff': {
    const fixtureArgs = process.argv.slice(3);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const home = option('--home');
    const root = option('--root');
    const { renderBootstrapMessage } = await import(
      '../../scripts/agent-compat/codex-bootstrap.mjs'
    );
    const currentPath = join(
      home, '.local', 'share', 'discord-ferry', 'reviewer-runtime', 'current',
    );
    const report = await runBootstrap({
      home,
      root,
      canonicalRoot: root,
      runtime: async () => ({ fixture: true, currentPath }),
      proton: async () => ({
        reviewer: { created: false, renewed: false },
        context7: { created: false, renewed: false, recovered: false },
      }),
    });
    writeJson({ report, human: renderBootstrapMessage(report) });
    break;
  }
  case 'context7-agent': {
    const fixtureArgs = process.argv.slice(3);
    const option = (name) => {
      const index = fixtureArgs.indexOf(name);
      return index === -1 ? undefined : fixtureArgs[index + 1];
    };
    const fixture = argument;
    const home = option('--home');
    const supported = new Set([
      'create-repeat',
      'expired',
      'interrupted',
      'post-create-list-failure',
      'unmanaged',
      'duplicate-agent',
      'duplicate-item',
      'unsafe-token',
      'unsafe-ownership',
      'invalid-ownership',
    ]);
    if (!supported.has(fixture) || !home) {
      throw new Error('invalid context7-agent fixture');
    }
    const { provisionContext7Agent } = await import(
      '../../scripts/agent-compat/codex-bootstrap.mjs'
    );
    const calls = [];
    let agents = fixture === 'unmanaged'
      ? [{ pat_id: 'unmanaged-id', name: 'discord-ferry-context7', expire_time: 1999999999 }]
      : fixture === 'duplicate-agent'
        ? [
            { pat_id: 'duplicate-1', name: 'discord-ferry-context7' },
            { pat_id: 'duplicate-2', name: 'discord-ferry-context7' },
          ]
        : [];
    let fieldReads = 0;
    let accessGrants = [];
    let generation = 0;
    let failGrant = fixture === 'interrupted';
    let failPostCreateList = false;
    const run = (_passCli, args) => {
      calls.push(args);
      if (args.join(' ') === 'agent access grant --help') return '--item-title --role';
      if (args[0] === 'vault') return JSON.stringify({ vaults: [{ name: 'Personal' }] });
      if (args[0] === 'item') return JSON.stringify({ items: fixture === 'duplicate-item'
        ? [{ title: 'Context7 API Key' }, { title: 'Context7 API Key' }]
        : [{ title: 'Context7 API Key' }] });
      if (args[0] === 'agent' && args[1] === 'list') {
        if (failPostCreateList) {
          failPostCreateList = false;
          throw new Error('agent list failed');
        }
        return JSON.stringify({ agents });
      }
      if (args[0] === 'agent' && args[1] === 'create') {
        generation += 1;
        const id = `context7-agent-id-${generation}`;
        agents = [{
          pat_id: id,
          name: 'discord-ferry-context7',
          expire_time: 1999999999,
        }];
        failPostCreateList = fixture === 'post-create-list-failure' && generation === 1;
        return JSON.stringify({
          token: `PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_${'n'.repeat(40)}`,
          instruction: 'Use this token with Proton Pass CLI.',
        });
      }
      if (args[0] === 'agent' && args[1] === 'access' && args[2] === 'grant') {
        if (failGrant) {
          failGrant = false;
          throw new Error('grant failed');
        }
        accessGrants = [{
          type: 'item',
          item_title: 'Context7 API Key',
          role: 'Viewer',
          share_id: 'context7-item-share-id',
          item_id: 'context7-item-id',
        }];
        return '';
      }
      if (args[0] === 'personal-access-token' && args[1] === 'access'
          && args[2] === 'list-access') return JSON.stringify(accessGrants);
      if (args[0] === 'agent' && args[1] === 'renew') {
        agents[0].expire_time = 1999999999;
        return JSON.stringify({ token: `pst_${'w'.repeat(40)}` });
      }
      if (args[0] === 'agent' && args[1] === 'delete') {
        agents = [];
        accessGrants = [];
        return '';
      }
      throw new Error(`unexpected pass-cli call: ${args.join(' ')}`);
    };
    const fieldReader = async (descriptor) => {
      fieldReads += 1;
      if (descriptor.tokenFile !== 'context7-agent.pat'
          || descriptor.shareId !== 'context7-item-share-id'
          || descriptor.itemId !== 'context7-item-id'
          || descriptor.field !== 'API Key') {
        throw new Error('wrong Context7 field descriptor');
      }
      return 'FERRY_SECRET_CANARY';
    };
    const provision = () => provisionContext7Agent({
      home, passCli: '/fixture/pass-cli', run, fieldReader,
    });
    let first = null;
    let second = null;
    let error = null;
    try {
      first = await provision();
    } catch (caught) {
      error = caught.message;
    }
    const tokenPath = join(home, '.config', 'discord-ferry', 'context7-agent.pat');
    const ownershipPath = join(home, '.config', 'discord-ferry', 'context7-agent.json');
    if (fixture === 'expired' && first) agents[0].expire_time = 1;
    if (fixture === 'unsafe-token' && first) chmodSync(tokenPath, 0o644);
    if (fixture === 'unsafe-ownership' && first) chmodSync(ownershipPath, 0o644);
    if (fixture === 'invalid-ownership' && first) {
      const document = JSON.parse(readFileSync(ownershipPath, 'utf8'));
      writeFileSync(ownershipPath, JSON.stringify({ ...document, extra: true }), { mode: 0o600 });
    }
    if (['create-repeat', 'expired', 'interrupted', 'post-create-list-failure', 'unsafe-token',
      'unsafe-ownership', 'invalid-ownership'].includes(fixture)) {
      try {
        second = await provision();
      } catch (caught) {
        error = caught.message;
      }
    }
    const isGrant = args => args[0] === 'agent' && args[1] === 'access'
      && args[2] === 'grant' && args[3] !== '--help';
    const grant = calls.find(isGrant);
    const ownershipExists = existsSync(ownershipPath);
    writeJson({
      first,
      second,
      error,
      grant,
      grant_count: calls.filter(isGrant).length,
      create_count: calls.filter(args => args[0] === 'agent' && args[1] === 'create').length,
      renew_count: calls.filter(args => args[0] === 'agent' && args[1] === 'renew').length,
      delete_calls: calls.filter(args => args[0] === 'agent' && args[1] === 'delete'),
      field_reads: fieldReads,
      token_mode: existsSync(tokenPath) ? lstatSync(tokenPath).mode & 0o777 : null,
      ownership_mode: ownershipExists ? lstatSync(ownershipPath).mode & 0o777 : null,
      ownership: ownershipExists ? JSON.parse(readFileSync(ownershipPath, 'utf8')) : null,
    });
    break;
  }
  case 'context7-launch': {
    const fixture = argument;
    const supported = new Set(['success', 'signal', 'credential-failure', 'check']);
    if (!supported.has(fixture)) throw new Error('invalid context7-launch fixture');
    const { EventEmitter } = await import('node:events');
    const { context7Environment, runContext7 } = await import(
      '../../scripts/agent-compat/context7-mcp.mjs'
    );
    const sourceEnvironment = {
      PATH: '/fixture/bin',
      HOME: '/fixture/home',
      TMPDIR: '/fixture/tmp',
      LANG: 'en_US.UTF-8',
      LC_ALL: undefined,
      NODE_EXTRA_CA_CERTS: '/fixture/ca.pem',
      OPENAI_API_KEY: 'FERRY_SECRET_CANARY',
    };
    const parent = new EventEmitter();
    const child = new EventEmitter();
    child.pid = 123;
    const forwarded = [];
    child.kill = signal => forwarded.push(signal);
    const spawns = [];
    const spawnChild = (command, args, options) => {
      spawns.push({ command, args, options });
      queueMicrotask(() => {
        if (fixture === 'signal') parent.emit('SIGTERM');
        child.emit('close', fixture === 'success' ? 23 : 0, fixture === 'signal' ? 'SIGTERM' : null);
      });
      return child;
    };
    let fieldDescriptor = null;
    const fieldReader = async (descriptor) => {
      fieldDescriptor = descriptor;
      if (fixture === 'credential-failure') throw new Error('FERRY_SECRET_CANARY');
      return 'FERRY_CONTEXT7_KEY_CANARY';
    };
    const accessReader = () => ({
      shareId: 'context7-item-share-id',
      itemId: 'context7-item-id',
    });
    let result = null;
    let error = null;
    try {
      result = await runContext7({
        home: '/fixture/home',
        check: fixture === 'check',
        fieldReader,
        accessReader,
        spawnChild,
        environment: sourceEnvironment,
        parent,
      });
    } catch (caught) {
      error = caught.message;
    }
    const spawn = spawns[0] ?? null;
    writeJson({
      filtered_environment: context7Environment(sourceEnvironment, 'REDACTED'),
      result,
      error,
      spawn_count: spawns.length,
      command: spawn?.command ?? null,
      args: spawn?.args ?? null,
      stdio: spawn?.options?.stdio ?? null,
      child_env_names: spawn ? Object.keys(spawn.options.env).sort() : [],
      child_has_context7_key: spawn?.options?.env?.CONTEXT7_API_KEY === 'FERRY_CONTEXT7_KEY_CANARY',
      child_has_parent_canary: spawn ? 'OPENAI_API_KEY' in spawn.options.env : false,
      field_descriptor: fieldDescriptor,
      forwarded,
      remaining_signal_listeners: parent.eventNames().length,
    });
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
    const networkGrant = fixture === 'missing-network-grant'
      ? []
      : ['[sandbox_workspace_write]', 'network_access = true'];
    const proxyEnabled = fixture === 'disabled-network-proxy'
      ? 'enabled = false'
      : 'enabled = true';
    const proxyDomains = fixture === 'incomplete-network-policy'
      ? 'domains = { "api.github.com" = "allow" }'
      : fixture === 'widened-network-policy'
        ? 'domains = { "api.github.com" = "allow", "github.com" = "allow", ' +
          '"example.com" = "allow" }'
        : 'domains = { "api.github.com" = "allow", "github.com" = "allow" }';
    const projectConfig = [
      fixture === 'commented-model'
        ? '# model = "gpt-5.6-sol"'
        : 'model = "gpt-5.6-sol"',
      'model_reasoning_effort = "high"',
      'approval_policy = "on-request"',
      'sandbox_mode = "workspace-write"',
      'web_search = "disabled"',
      ...networkGrant,
      '[features]',
      'hooks = true',
      '[features.network_proxy]',
      proxyEnabled,
      ...(fixture === 'local-binding-network-policy'
        ? ['allow_local_binding = true']
        : []),
      proxyDomains,
      '[mcp_servers.qmd]',
      '[mcp_servers.serena]',
      ...(fixture === 'missing-tool-server' ? [] : ['[mcp_servers.context7]']),
    ].join('\n');
    const canonicalHookCommand = codexChatCommand(root);
    const hookCommand = fixture === 'stale-wrapper'
      ? 'plain-english-chat-hook.mjs'
      : canonicalHookCommand;
    const adapterCommand = (mode) =>
      `node "${root}/scripts/agent-compat/codex-hook-adapter.mjs" ${mode}`;
    const codexGroup = (event, mode, matcher = null) => ({
      ...(matcher === null ? {} : { matcher }),
      hooks: [{ type: 'command', command: adapterCommand(mode), timeout: 10 }],
    });
    const hooks = {
      hooks: {
        UserPromptSubmit: [codexGroup('UserPromptSubmit', 'user-prompt')],
        PreToolUse: [codexGroup('PreToolUse', 'pre-tool', '.*')],
        PostToolUse: [codexGroup('PostToolUse', 'post-tool', codexPostToolMatcher())],
        Stop: [
          { hooks: [{ command: hookCommand, timeout: 60 }] },
          codexGroup('Stop', 'stop'),
        ],
        SubagentStop: [{ hooks: [{ command: hookCommand, timeout: 60 }] }],
      },
    };
    if (fixture === 'missing-hook') {
      hooks.hooks.Stop = [codexGroup('Stop', 'stop')];
      hooks.hooks.SubagentStop = [];
    }
    if (fixture === 'misdistributed-hook') {
      hooks.hooks.Stop[0].hooks.push({ command: canonicalHookCommand, timeout: 60 });
      hooks.hooks.SubagentStop = [];
    }
    if (fixture === 'missing-brainstorm-prompt') hooks.hooks.UserPromptSubmit = [];
    if (fixture === 'broken-brainstorm-matcher') {
      hooks.hooks.PostToolUse[0].matcher = '^(Write)$';
    }
    const directBrainstorm =
      'node "$CLAUDE_PROJECT_DIR/scripts/agent-compat/brainstorm-evidence.mjs" --host claude';
    const claudeGroup = (matcher = null) => ({
      ...(matcher === null ? {} : { matcher }),
      hooks: [{ type: 'command', command: directBrainstorm, timeout: 10 }],
    });
    const claudeSettings = {
      hooks: {
        UserPromptSubmit: [claudeGroup()],
        PreToolUse: [claudeGroup('.*')],
        PostToolUse: [claudeGroup('.*')],
        PostToolUseFailure: [claudeGroup('.*')],
        Stop: [claudeGroup()],
      },
    };
    if (fixture === 'missing-brainstorm-failed-tool') {
      claudeSettings.hooks.PostToolUseFailure = [];
    }
    const trust = fixture === 'single-quoted-trust'
      ? `[projects.'${root}']\ntrust_level = "trusted"\n`
      : `[projects.${JSON.stringify(root)}]\ntrust_level = "trusted"\n`;
    const instructions = fixture === 'missing-review-boundary'
      ? '# Host Compatibility\n'
      : [
          '# Host Compatibility',
          'Live provider collection uses the active host credential and network route.',
          'Codex requests escalated execution on the first attempt.',
          'It never tries the workspace sandbox first and never runs a separate credential login.',
          'Local authorization, command execution, classification, and verdict evaluation stay',
          'in the workspace sandbox.',
          'During the build, run `mkdir -p docs/plans`, then write',
          '`docs/plans/change-manifest.md`.',
        ].join('\n');
    const manifestWriter = [
      'During the build, run `mkdir -p docs/plans`, then write',
      '`docs/plans/change-manifest.md`.',
    ].join('\n');
    const manifestReader = [
      'head -1 docs/plans/change-manifest.md',
      'If the manifest names this branch or this change, use it.',
      'If no manifest exists, grep the codebase pattern.',
      'If `docs/plans/change-manifest.md` exists, include it as manifest context.',
    ].join('\n');
    const entries = new Map([
      [`${root}/.codex/config.toml`, projectConfig],
      [`${home}/.codex/config.toml`, trust],
      [`${root}/AGENTS.md`, instructions],
      [`${root}/CLAUDE.md`, manifestWriter],
      [`${root}/.codex/hooks.json`, JSON.stringify(hooks)],
      [`${root}/.claude/settings.json`, JSON.stringify(claudeSettings)],
      [`${root}/.claude/skills/df-start/SKILL.md`, '# start\n'],
      [`${root}/.agents/skills/df-start/SKILL.md`, '# start\n'],
      [`${root}/.worktreeinclude`, 'CLAUDE.md\n'],
      [`${root}/.claude/scripts/new-worktree.sh`, '#!/bin/sh\n'],
      [`${root}/.claude/skills/df-ship/SKILL.md`, manifestReader],
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
      command: (name, args = []) => {
        if (name === process.execPath && args.includes('--check-worktree-contract')) {
          const expected = [
            join(root, 'scripts', 'agent-compat', 'check.mjs'),
            '--check-worktree-contract',
            join(root, '.claude', 'scripts', 'new-worktree.sh'),
            join(root, '.worktreeinclude'),
            join(root, 'AGENTS.md'),
            join(root, 'CLAUDE.md'),
            join(root, '.claude', 'skills', 'df-ship', 'SKILL.md'),
          ];
          return { status: JSON.stringify(args) === JSON.stringify(expected) ? 0 : 1, stdout: '' };
        }
        const dataHome = process.env.XDG_DATA_HOME || join(home, '.local', 'share');
        const context7Launcher = join(
          dataHome,
          'discord-ferry',
          'reviewer-runtime',
          'current',
          'context7-mcp.mjs',
        );
        if (name === process.execPath
            && args[0] === context7Launcher
            && args[1] === '--check') {
          return fixture === 'context7-unavailable'
            ? { status: 1, stdout: '', stderr: 'FERRY_SECRET_CANARY' }
            : { status: 0, stdout: 'context7 credential ready\n', stderr: '' };
        }
        if (fixture === 'missing-codex' && name === 'codex') return { status: 1, stdout: '' };
        if (fixture === 'missing-client' && name === 'qwen') return { status: 1, stdout: '' };
        return { status: 0, stdout: `${name} fixture-version` };
      },
      now: () => 0,
      runtimeCheck: async () => ({ release: 'fixture', files: 8 }),
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
          'review-contract.mjs', 'proton-credential.mjs', 'context7-mcp.mjs',
          'vibe-review.mjs', 'qwen-review.mjs', 'claude-review.mjs',
          'review-ensemble.mjs', 'review-verification.mjs',
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
  case 'brainstorm-readiness-live': {
    const rootIndex = process.argv.indexOf('--root');
    const eventIndex = process.argv.indexOf('--event-cwd');
    if (rootIndex < 0 || eventIndex < 0) throw new Error('root and event cwd are required');
    writeJson(runBrainstormEvidenceProbe(
      process.argv[rootIndex + 1],
      process.argv[eventIndex + 1],
    ));
    break;
  }
  case 'readiness-reviewers': {
    const fixture = argument;
    let vibeCalls = 0;
    let qwenCalls = 0;
    const valid = (adapter, slot, model, findings = []) => makeReviewRecord({
      adapter,
      slot,
      requestedModel: model,
      resolvedModel: model,
      sessionId: `${adapter}-fixture-session`,
      durationMs: 4,
      status: 'valid',
      result: { findings, summary: 'ready', confidence: 'high' },
    });
    const qwenFinding = (command) => ({
      severity: 'minor',
      category: 'maintainability',
      file: 'pyproject.toml',
      line: null,
      description: 'The reviewer readiness marker is present.',
      suggestion: 'Keep the marker available to reviewer readiness.',
      verification: {
        command,
        confirms_if: { exit_code: 0, stdout_contains: 'discord-ferry', stdout_excludes: null },
        refutes_if: { exit_code: 1, stdout_contains: null, stdout_excludes: null },
      },
    });
    const adapters = {
      vibe: async () => {
        vibeCalls += 1;
        if (fixture === 'vibe-fails' || fixture === 'all-fail-canary') {
          throw new Error('FERRY_SECRET_CANARY');
        }
        return valid('vibe', 'mistral-vibe', 'zai-glm-5-2');
      },
      qwen: async () => {
        qwenCalls += 1;
        if (fixture === 'qwen-fails' || fixture === 'all-fail-canary') {
          throw new Error('FERRY_SECRET_CANARY');
        }
        if (fixture === 'qwen-wrong-model') {
          return valid('qwen', 'qwen', 'qwen3.6-flash');
        }
        if (fixture === 'qwen-empty') return valid('qwen', 'qwen', 'qwen3.8-max');
        const command = fixture === 'qwen-denied'
          ? 'rg -n -- FERRY_COMMAND_CANARY pyproject.toml | sh'
          : 'rg -n -- discord-ferry pyproject.toml';
        return valid('qwen', 'qwen', 'qwen3.8-max', [qwenFinding(command)]);
      },
    };
    const report = await runReviewerReadiness({
      root: process.cwd(),
      home: '/fixture-home',
      adapters,
      now: () => 0,
    });
    writeJson({ ...report, calls: { vibe: vibeCalls, qwen: qwenCalls } });
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
  case 'worktree-manifest-isolation': {
    const fixtureArgs = process.argv.slice(3);
    const baseIndex = fixtureArgs.indexOf('--base');
    const base = baseIndex === -1 ? null : fixtureArgs[baseIndex + 1];
    if (!base) throw new Error('--base is required');

    const source = process.cwd();
    const root = join(base, 'primary');
    const first = join(root, '.worktrees', 'manifest-first');
    const second = join(root, '.worktrees', 'manifest-second');
    const helper = join(root, '.claude', 'scripts', 'new-worktree.sh');
    const run = (command, args, cwd = root) => {
      const result = spawnSync(command, args, { cwd, encoding: 'utf8' });
      if (result.status !== 0) {
        throw new Error(`${command} ${args.join(' ')} failed: ${result.stderr || result.stdout}`);
      }
      return result.stdout.trim();
    };
    const localManifest = (checkout) => {
      const path = join(checkout, 'docs', 'plans', 'change-manifest.md');
      return existsSync(path) ? path : null;
    };

    mkdirSync(root, { recursive: true });
    run('git', ['init', '-q', '-b', 'main', root], base);
    cpSync(join(source, '.gitignore'), join(root, '.gitignore'));
    writeFileSync(join(root, 'seed.txt'), 'fixture\n');
    mkdirSync(join(root, 'docs'), { recursive: true });
    writeFileSync(join(root, 'docs', 'README.md'), '# Fixture docs\n');
    run('git', ['add', '.gitignore', 'seed.txt', 'docs/README.md']);
    run('git', [
      '-c', 'user.name=Ferry Fixture',
      '-c', 'user.email=ferry-fixture@example.invalid',
      '-c', 'commit.gpgsign=false',
      'commit', '-q', '-m', 'seed',
    ]);

    mkdirSync(join(root, '.claude', 'scripts'), { recursive: true });
    mkdirSync(join(root, '.claude', 'rules'), { recursive: true });
    mkdirSync(join(root, '.claude', 'skills', 'df-ship'), { recursive: true });
    mkdirSync(join(root, 'docs', 'architecture', 'adr'), { recursive: true });
    for (const host of ['.agents', '.codex', '.vibe', '.qwen']) {
      mkdirSync(join(root, host), { recursive: true });
      writeFileSync(join(root, host, 'fixture.txt'), `${host}\n`);
    }
    cpSync(join(source, '.claude', 'scripts', 'new-worktree.sh'), helper);
    chmodSync(helper, 0o755);
    writeFileSync(join(root, 'CLAUDE.md'), '# Original writer\n');
    writeFileSync(join(root, 'AGENTS.md'), '# Original writer\n');
    writeFileSync(join(root, '.claude', 'rules', 'fixture.md'), '# Shared rule\n');
    writeFileSync(
      join(root, '.claude', 'skills', 'df-ship', 'SKILL.md'),
      '# Original reader\n',
    );
    writeFileSync(join(root, 'docs', 'architecture', 'adr', 'fixture.md'), '# Shared ADR\n');

    const worktrees = [];
    let report;
    try {
      run(helper, ['manifest-first', 'main']);
      worktrees.push(first);
      const firstGitDirBefore = run('git', ['rev-parse', '--absolute-git-dir'], first);

      const writer = [
        'During the build, run `mkdir -p docs/plans`, then write',
        '`docs/plans/change-manifest.md`.',
      ].join('\n');
      writeFileSync(join(root, 'CLAUDE.md'), `${writer}\n`);
      writeFileSync(join(root, 'AGENTS.md'), `${writer}\n`);
      writeFileSync(
        join(root, '.claude', 'skills', 'df-ship', 'SKILL.md'),
        'head -1 docs/plans/change-manifest.md\n',
      );

      run(helper, ['manifest-second', 'main']);
      worktrees.push(second);

      const primaryPlans = join(root, 'docs', 'plans');
      const firstPlans = join(first, 'docs', 'plans');
      const secondPlans = join(second, 'docs', 'plans');
      mkdirSync(primaryPlans, { recursive: true });
      mkdirSync(firstPlans, { recursive: true });
      mkdirSync(firstPlans, { recursive: true });
      mkdirSync(secondPlans, { recursive: true });
      mkdirSync(secondPlans, { recursive: true });

      const primaryManifest = join(primaryPlans, 'change-manifest.md');
      const firstManifest = join(firstPlans, 'change-manifest.md');
      const secondManifest = join(secondPlans, 'change-manifest.md');
      writeFileSync(primaryManifest, '# Primary manifest\n');
      writeFileSync(firstManifest, '# First manifest\n');
      writeFileSync(secondManifest, '# Second manifest\n');
      writeFileSync(join(root, '.claude', 'change-manifest.md'), '# Legacy manifest\n');

      const firstBefore = readFileSync(firstManifest);
      const primaryBefore = readFileSync(primaryManifest);
      const instructionsBefore = directorySnapshot(join(root, '.claude'));
      writeFileSync(secondManifest, '# Second manifest rewritten\n');

      unlinkSync(firstManifest);
      const legacyIgnored = localManifest(first) === null &&
        existsSync(join(first, '.claude', 'change-manifest.md'));
      writeFileSync(firstManifest, firstBefore);

      const sharedLinks = [
        join(first, 'CLAUDE.md'),
        join(first, 'AGENTS.md'),
        join(first, '.claude'),
        join(first, '.agents'),
        join(first, '.codex'),
        join(first, '.vibe'),
        join(first, '.qwen'),
        join(first, 'docs', 'architecture'),
      ];
      const statuses = [root, first, second].map((path) =>
        run('git', ['status', '--porcelain'], path));
      const firstGitDirAfter = run('git', ['rev-parse', '--absolute-git-dir'], first);

      report = {
        distinct_paths: new Set([
          realpathSync(primaryManifest),
          realpathSync(firstManifest),
          realpathSync(secondManifest),
        ]).size === 3,
        first_unchanged_after_second_write:
          readFileSync(firstManifest).equals(firstBefore) &&
          readFileSync(secondManifest, 'utf8') === '# Second manifest rewritten\n',
        primary_unchanged:
          readFileSync(primaryManifest).equals(primaryBefore) &&
          directorySnapshot(join(root, '.claude')) === instructionsBefore,
        legacy_ignored: legacyIgnored,
        parent_created_twice: existsSync(firstPlans) && existsSync(secondPlans),
        statuses_clean: statuses.every((status) => status === ''),
        shared_instruction_links:
          sharedLinks.every((path) => lstatSync(path).isSymbolicLink()) &&
          readFileSync(join(first, '.claude', 'rules', 'fixture.md'), 'utf8') === '# Shared rule\n' &&
          realpathSync(join(first, 'docs', 'architecture')) ===
            resolve(root, 'docs', 'architecture'),
        existing_worktree_reused:
          firstGitDirAfter === firstGitDirBefore &&
          run('git', ['branch', '--show-current'], first) === 'manifest-first',
      };
    } finally {
      for (const worktree of worktrees.reverse()) {
        const removed = spawnSync('git', ['worktree', 'remove', '--force', worktree], {
          cwd: root,
          encoding: 'utf8',
        });
        if (removed.status !== 0) {
          throw new Error(`git worktree remove failed: ${removed.stderr || removed.stdout}`);
        }
      }
    }
    writeJson(report);
    break;
  }
  case 'verify-all': {
    const fixture = argument;
    const commands = [];
    const failedLayer = fixture.startsWith('fail-') ? fixture.slice(5) : null;
    const failDocumentationPrerequisite = fixture === 'documentation-prerequisite-fails';
    const report = await runVerificationLayers({
      root: process.cwd(),
      markdownFiles: ['CHANGELOG.md'],
      helperFiles: ['scripts/agent-compat/review-contract.mjs'],
      run: async (command, args, options) => {
        commands.push([command, ...args]);
        const isDocumentationPrerequisite = command === process.execPath &&
          args[0] === 'scripts/agent-compat/plain-english-contract.mjs';
        return {
          status: options.layer === failedLayer ||
            (failDocumentationPrerequisite && isDocumentationPrerequisite) ? 1 : 0,
        };
      },
      now: () => 0,
    });
    writeJson({
      ...report,
      commands,
      documentation_start: 5,
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
        path: '/fixture/primary',
        markers: ['instructions', 'skill', 'qmd'],
        tree_hash_before: 'primary-hash',
        tree_hash_after: 'primary-hash',
      },
      worktree: {
        path: '/fixture/worktree',
        markers: ['instructions', 'skill', 'qmd'],
        tree_hash_before: 'worktree-hash',
        tree_hash_after: 'worktree-hash',
      },
      hooks: {
        session_start: 'ok',
        pre_tool_allow: 'ok',
        pre_tool_block: 'ok',
        post_tool: 'ok',
        brainstorm_prompt: 'ok',
        brainstorm_pre_tool: 'ok',
        brainstorm_result: 'ok',
        brainstorm_incomplete_stop: 'ok',
        brainstorm_complete_stop: 'ok',
        brainstorm_outside_directory: 'ok',
        stop_main: {
          status: 'ok',
          timeout_seconds: 60,
          duration_ms: 20,
          owner_root: '/fixture/primary',
          event_cwd: '/fixture/event',
          command: codexChatCommand('/fixture/primary'),
        },
        stop_child: {
          status: 'ok',
          timeout_seconds: 60,
          duration_ms: 20,
          owner_root: '/fixture/primary',
          event_cwd: '/fixture/event',
          command: codexChatCommand('/fixture/primary'),
        },
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
    if (fixture === 'brainstorm-missing-prompt') evidence.hooks.brainstorm_prompt = 'failed';
    if (fixture === 'brainstorm-broken-matcher') evidence.hooks.brainstorm_pre_tool = 'failed';
    if (fixture === 'brainstorm-adapter-inside-repo') {
      evidence.hooks.brainstorm_outside_directory = 'failed';
    }
    if (fixture === 'stop-ten-second') evidence.hooks.stop_main.timeout_seconds = 10;
    if (fixture === 'stop-timeout') evidence.hooks.stop_child.duration_ms = 59_000;
    if (fixture === 'stop-event-inside-owner') {
      evidence.hooks.stop_main.event_cwd = evidence.primary.path;
    }
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
