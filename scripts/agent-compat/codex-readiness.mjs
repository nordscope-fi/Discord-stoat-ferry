#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  canonicalCheckoutRoot,
  inspectProjectTrustToml,
  verifyReviewerRuntime,
} from './codex-bootstrap.mjs';
import { buildReviewPrompt } from './review-contract.mjs';
import { codexChatCommand } from './plain-english-contract.mjs';
import { runQwenReview } from './qwen-review.mjs';
import { authorizeVerificationCommand } from './review-verification.mjs';
import { runVibeReview } from './vibe-review.mjs';

export function readinessRecord(
  id,
  className,
  status,
  durationMs,
  remediation = null,
  details = {},
) {
  if (!['ok', 'warning', 'fail'].includes(status)) {
    throw new Error(`invalid status for ${id}`);
  }
  return {
    id,
    class: className,
    status,
    duration_ms: Math.max(0, Math.round(durationMs)),
    remediation,
    details,
  };
}

export function safeReadinessFailure(id, className, reason, remediation, durationMs = 0) {
  return readinessRecord(id, className, 'fail', durationMs, remediation, { reason });
}

const liveFiles = {
  exists: (path) => existsSync(path),
  readText: (path) => readFileSync(path, 'utf8'),
  list: (path) => readdirSync(path),
};

function liveCommand(name, args, options = {}) {
  const result = spawnSync(name, args, {
    cwd: options.cwd,
    input: options.input,
    encoding: 'utf8',
    stdio: ['pipe', 'pipe', 'pipe'],
    timeout: options.timeoutMs ?? 15_000,
    maxBuffer: 2 * 1024 * 1024,
  });
  return {
    status: result.status ?? 1,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

async function defaultProviderProbe({ root, command }) {
  const prompt = [
    'Return the marker FERRY_CODEX_RUNTIME_OK after completing three read-only tool calls.',
    'Call qmd status with {}.',
    'Call Serena get_symbols_overview for src/discord_ferry/__init__.py at depth 0.',
    'Call Context7 resolve-library-id for aiohttp.',
    'End with one line: FERRY_CODEX_RUNTIME_OK.',
  ].join('\n');
  const result = command('codex', [
    'exec', '--ephemeral', '--sandbox', 'read-only', '--model', 'gpt-5.6-sol', '--json', '-',
  ], { cwd: root, input: prompt, timeoutMs: 180_000 });
  if (result.status !== 0) throw new Error('provider smoke failed');
  return parseProviderEvents(result.stdout);
}

export function parseProviderEvents(stdout) {
  const events = stdout.split(/\r?\n/u).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
  const completed = (server, tool) => events.some((event) =>
    event?.type === 'item.completed' && event?.item?.type === 'mcp_tool_call' &&
    event.item.server === server && event.item.tool === tool && event.item.status === 'completed');
  const marker = events.some((event) =>
    event?.type === 'item.completed' && event?.item?.type === 'agent_message' &&
    event.item.text?.trim() === 'FERRY_CODEX_RUNTIME_OK');
  if (!marker || !completed('qmd', 'status') ||
      !completed('serena', 'get_symbols_overview') ||
      !completed('context7', 'resolve-library-id')) {
    throw new Error('provider evidence incomplete');
  }
  return {
    marker: 'FERRY_CODEX_RUNTIME_OK',
    model: 'gpt-5.6-sol',
    qmd: 'ok',
    serena: 'ok',
    context7: 'ok',
  };
}

async function defaultUpdateProbe({ installedVersion, command }) {
  let latest = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const result = command('npm', ['view', '@openai/codex', 'version'], { timeoutMs: 20_000 });
    if (result.status === 0) {
      latest = result.stdout.trim().match(/^\d+(?:\.\d+){2}(?:[-+][\w.-]+)?$/u)?.[0] ?? null;
      if (latest) break;
    }
  }
  if (!latest) throw new Error('registry version unavailable');
  return { current: installedVersion === latest, installed: installedVersion, latest };
}

function requireText(files, path, label) {
  if (!files.exists(path)) throw new Error(`${label} missing`);
  try {
    return files.readText(path);
  } catch {
    throw new Error(`${label} unreadable`);
  }
}

async function checkRecord({ id, className, remediation, reason, now }, check) {
  const started = now();
  try {
    const details = await check();
    return readinessRecord(id, className, 'ok', now() - started, null, details ?? {});
  } catch {
    return safeReadinessFailure(id, className, reason, remediation, now() - started);
  }
}

function hasNativeChatHooks(document, ownerRoot) {
  const expectedCommand = codexChatCommand(ownerRoot);
  return ['Stop', 'SubagentStop'].every((event) => {
    const native = (document.hooks?.[event] ?? []).flatMap((group) => group.hooks ?? [])
      .filter((hook) => hook.command === expectedCommand);
    return native.length === 1 && native[0].timeout === 60;
  });
}

function hasActiveLine(source, expected) {
  return source.split(/\r?\n/u).some((line) => line.trim() === expected);
}

function hasReviewBoundary(source) {
  const normalized = source.split(/\s+/u).join(' ').toLowerCase();
  return [
    'live provider collection uses',
    'codex requests escalated execution on the first attempt',
    'never tries the workspace sandbox first',
    'never runs a separate credential login',
    'verdict evaluation stay in the workspace sandbox',
  ].every((text) => normalized.includes(text));
}

function parseRole(path) {
  const source = readFileSync(path, 'utf8');
  const model = source.match(/^model\s*=\s*"([^"]+)"/mu)?.[1] ?? null;
  const sandbox = source.match(/^sandbox_mode\s*=\s*"([^"]+)"/mu)?.[1] ?? null;
  return { model, sandbox };
}

function treeHash(path, command) {
  const result = command('git', ['status', '--porcelain=v1', '--untracked-files=all'], { cwd: path });
  if (result.status !== 0) throw new Error('tree status unavailable');
  return createHash('sha256').update(result.stdout).digest('hex');
}

function sessionFiles(stateRoot) {
  const root = join(stateRoot, 'sessions');
  if (!existsSync(root)) return [];
  const found = [];
  const visit = (path) => {
    for (const entry of readdirSync(path, { withFileTypes: true })) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) visit(child);
      else if (entry.isFile() && entry.name.endsWith('.jsonl')) found.push(child);
    }
  };
  visit(root);
  return found;
}

function childSessionEvidence(path, parentThreadId) {
  const events = readFileSync(path, 'utf8').split(/\r?\n/u).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
  const meta = events.find((event) => event?.type === 'session_meta')?.payload;
  const spawn = meta?.source?.subagent?.thread_spawn;
  if (spawn?.parent_thread_id !== parentThreadId || !spawn.agent_role) return null;
  const context = events.filter((event) => event?.type === 'turn_context').at(-1)?.payload;
  const completed = events.some((event) => {
    const item = event?.type === 'event_msg' ? event.payload?.item : null;
    return item?.type === 'AgentMessage' && item.content?.some((part) =>
      part?.type === 'Text' && part.text?.trim() === spawn.agent_role);
  });
  return {
    role: spawn.agent_role,
    model: context?.model ?? null,
    sandbox: context?.sandbox_policy?.type ?? null,
    completed,
  };
}

export function parseRoleProbeEvents(stdout, stderr = '', childSessions = [], expected = []) {
  if (stderr.includes('collab spawn failed')) throw new Error('role spawn failed');
  const events = stdout.split(/\r?\n/u).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
  const parentThreadId = events.find((event) => event?.type === 'thread.started')?.thread_id;
  const marker = events.find((event) =>
    event?.type === 'item.completed' && event?.item?.type === 'agent_message' &&
    event.item.text?.trim() === 'FERRY_ROLE_PROBE_OK');
  const byRole = new Map(childSessions.filter((session) =>
    session?.parent_thread_id === parentThreadId).map((session) => [session.role, session]));
  if (!parentThreadId || !marker || expected.some((role) =>
    !byRole.has(role) || byRole.get(role).completed !== true)) {
    throw new Error('role selection evidence incomplete');
  }
  return Object.fromEntries(expected.map((role) => [role, byRole.get(role)]));
}

function runRoleGroup(root, command, codexStateRoot, roles, sandbox) {
  const prompt = [
    `Spawn one child with each Ferry custom role: ${roles.join(', ')}.`,
    'Each child must make no tool calls and return only its role name.',
    `Wait for all ${roles.length} children. End with exactly FERRY_ROLE_PROBE_OK.`,
  ].join('\n');
  const before = new Set(sessionFiles(codexStateRoot));
  const result = command('codex', [
    'exec', '--sandbox', sandbox, '--model', 'gpt-5.6-sol',
    '-c', 'model_reasoning_effort="low"', '--json', '-',
  ], { cwd: root, input: prompt, timeoutMs: 240_000 });
  if (result.status !== 0) throw new Error('role probe failed');
  const parentThreadId = result.stdout.split(/\r?\n/u).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  }).find((event) => event?.type === 'thread.started')?.thread_id;
  const childSessions = sessionFiles(codexStateRoot)
    .filter((path) => !before.has(path))
    .map((path) => childSessionEvidence(path, parentThreadId))
    .filter(Boolean)
    .map((session) => ({ ...session, parent_thread_id: parentThreadId }));
  return parseRoleProbeEvents(result.stdout, result.stderr, childSessions, roles);
}

function roleEvidence(root, command) {
  const codexStateRoot = process.env.CODEX_HOME ?? join(process.env.HOME, '.codex');
  const selection = {
    ...runRoleGroup(root, command, codexStateRoot, ['coordinator'], 'workspace-write'),
    ...runRoleGroup(
      root, command, codexStateRoot, ['reviewer', 'explorer', 'locator'], 'read-only',
    ),
  };
  return Object.fromEntries(Object.keys(selection).map((role) => [
    role,
    (() => {
      const configured = parseRole(join(root, '.codex', 'agents', `${role}.toml`));
      const actual = selection[role];
      if (actual.model !== configured.model || actual.sandbox !== configured.sandbox) {
        throw new Error(`${role} child session did not apply its generated role file`);
      }
      return { model: actual.model, sandbox: actual.sandbox, selected: true };
    })(),
  ]));
}

function sessionEvidence(path, command) {
  if (!existsSync(join(path, 'AGENTS.md')) ||
      !existsSync(join(path, '.agents', 'skills', 'df-start', 'SKILL.md'))) {
    throw new Error('session instruction links missing');
  }
  const prompt = [
    'Read AGENTS.md and .agents/skills/df-start/SKILL.md.',
    'Call qmd status with {}.',
    'Make no writes. End with exactly FERRY_WORKTREE_SESSION_OK.',
  ].join('\n');
  const result = command('codex', [
    'exec', '--ephemeral', '--sandbox', 'read-only', '--model', 'gpt-5.6-sol', '--json', '-',
  ], { cwd: path, input: prompt, timeoutMs: 180_000 });
  if (result.status !== 0) throw new Error('Codex session failed');
  const events = result.stdout.split(/\r?\n/u).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
  const qmd = events.some((event) =>
    event?.type === 'item.completed' && event?.item?.type === 'mcp_tool_call' &&
    event.item.server === 'qmd' && event.item.tool === 'status' &&
    event.item.status === 'completed');
  const marker = events.some((event) =>
    event?.type === 'item.completed' && event?.item?.type === 'agent_message' &&
    event.item.text?.trim() === 'FERRY_WORKTREE_SESSION_OK');
  if (!qmd || !marker) throw new Error('session marker evidence incomplete');
  return ['instructions', 'skill', 'qmd'];
}

function runAdapter(root, mode, payload, command) {
  const result = command(process.execPath, [
    join(root, 'scripts', 'agent-compat', 'codex-hook-adapter.mjs'), mode,
  ], { cwd: root, input: JSON.stringify(payload), timeoutMs: 15_000 });
  return result;
}

function stopEvidence(root, event, eventCwd, command) {
  const hooks = JSON.parse(readFileSync(join(root, '.codex', 'hooks.json'), 'utf8'));
  const expectedCommand = codexChatCommand(root);
  const native = (hooks.hooks?.[event] ?? []).flatMap((group) => group.hooks ?? [])
    .filter((hook) => hook.command === expectedCommand);
  if (native.length !== 1 || native[0].timeout !== 60) {
    throw new Error('native Stop hook contract mismatch');
  }
  const started = performance.now();
  const result = command('/bin/sh', ['-c', native[0].command], {
    cwd: eventCwd,
    input: JSON.stringify({
      last_assistant_message: 'Task status: verification passed. No action is required.',
    }),
    timeoutMs: 58_000,
  });
  return {
    status: result.status === 0 ? 'ok' : 'failed',
    timeout_seconds: native[0].timeout,
    duration_ms: performance.now() - started,
    owner_root: root,
    event_cwd: eventCwd,
    command: native[0].command,
  };
}

async function defaultWorktreeProbe({ root, command = liveCommand }) {
  const primaryRoot = canonicalCheckoutRoot(root);
  const name = `codex-readiness-${process.pid}-${Date.now()}`;
  const worktree = join(primaryRoot, '.worktrees', name);
  const branch = command('git', ['branch', '--show-current'], { cwd: primaryRoot }).stdout.trim();
  const added = command(
    join(primaryRoot, '.claude', 'scripts', 'new-worktree.sh'),
    [name, branch || 'main'],
    { cwd: primaryRoot, timeoutMs: 60_000 },
  );
  if (added.status !== 0) throw new Error('temporary worktree creation failed');
  let eventCwd = null;
  try {
    eventCwd = mkdtempSync(join(tmpdir(), 'ferry-codex-stop-'));
    const primaryBefore = treeHash(primaryRoot, command);
    const worktreeBefore = treeHash(worktree, command);
    const primaryMarkers = sessionEvidence(primaryRoot, command);
    const worktreeMarkers = sessionEvidence(worktree, command);

    const sessionStart = runAdapter(primaryRoot, 'session-start', {}, command);
    const preAllow = runAdapter(primaryRoot, 'pre-tool', {
      tool_name: 'Bash', tool_input: { command: 'git status --short' },
    }, command);
    const preBlock = runAdapter(primaryRoot, 'pre-tool', {
      tool_name: 'Bash', tool_input: { command: 'git reset --hard' },
    }, command);
    const postTool = runAdapter(primaryRoot, 'post-tool', {
      tool_name: 'apply_patch', tool_input: { patch: '' },
    }, command);

    return {
      primary: {
        path: primaryRoot,
        markers: primaryMarkers,
        tree_hash_before: primaryBefore,
        tree_hash_after: treeHash(primaryRoot, command),
      },
      worktree: {
        path: worktree,
        markers: worktreeMarkers,
        tree_hash_before: worktreeBefore,
        tree_hash_after: treeHash(worktree, command),
      },
      hooks: {
        session_start: sessionStart.status === 0 ? 'ok' : 'failed',
        pre_tool_allow: preAllow.status === 0 && !preAllow.stdout.includes('"decision":"block"')
          ? 'ok' : 'failed',
        pre_tool_block: preBlock.status === 0 && preBlock.stdout.includes('"decision":"block"')
          ? 'ok' : 'failed',
        post_tool: postTool.status === 0 ? 'ok' : 'failed',
        stop_main: stopEvidence(primaryRoot, 'Stop', eventCwd, command),
        stop_child: stopEvidence(primaryRoot, 'SubagentStop', eventCwd, command),
      },
      roles: roleEvidence(primaryRoot, command),
    };
  } finally {
    if (eventCwd !== null) rmSync(eventCwd, { recursive: true, force: true });
    command('git', ['worktree', 'remove', worktree], { cwd: primaryRoot, timeoutMs: 60_000 });
  }
}

function pathIsWithin(root, candidate) {
  const offset = relative(resolve(root), resolve(candidate));
  return offset === '' || (!offset.startsWith('..') && !isAbsolute(offset));
}

export async function runStaticReadiness({
  root,
  home,
  canonicalRoot = null,
  files = liveFiles,
  command = liveCommand,
  now = () => performance.now(),
  runtimeCheck = verifyReviewerRuntime,
}) {
  if (!root || !home) throw new Error('static readiness requires root and home');
  const projectRoot = resolve(root);
  const ownedRoot = resolve(canonicalRoot ?? canonicalCheckoutRoot(projectRoot));
  const records = [];
  const add = async (definition, check) => {
    records.push(await checkRecord({ ...definition, now }, check));
  };

  await add({
    id: 'codex-version',
    className: 'runtime',
    reason: 'Codex CLI is not installed or does not start',
    remediation: 'Install or repair the Codex CLI, then rerun readiness.',
  }, () => {
    const result = command('codex', ['--version'], { cwd: projectRoot });
    if (result.status !== 0) throw new Error('codex unavailable');
    const version = result.stdout.match(/\d+(?:\.\d+){1,3}(?:[-+][\w.-]+)?/u)?.[0] ?? 'installed';
    return { version };
  });

  await add({
    id: 'project-config',
    className: 'configuration',
    reason: 'Ferry project Codex configuration is missing or incomplete',
    remediation: 'Run ./scripts/agent-install.sh from the primary checkout.',
  }, () => {
    const source = requireText(files, join(projectRoot, '.codex', 'config.toml'), 'project config');
    const required = [
      'model = "gpt-5.6-sol"',
      'model_reasoning_effort = "high"',
      'approval_policy = "on-request"',
      'sandbox_mode = "workspace-write"',
      'web_search = "disabled"',
    ];
    if (required.some((entry) => !hasActiveLine(source, entry))) {
      throw new Error('project pins missing');
    }
    return { pins: required.length };
  });

  await add({
    id: 'global-trust',
    className: 'configuration',
    reason: 'Ferry canonical checkout trust is missing or ambiguous',
    remediation: 'Run node scripts/agent-compat/codex-bootstrap.mjs.',
  }, () => {
    const source = requireText(files, join(home, '.codex', 'config.toml'), 'global config');
    const semantic = inspectProjectTrustToml(source, ownedRoot);
    if (!semantic.trusted) throw new Error('trust missing');
    return { canonical: true };
  });

  await add({
    id: 'instructions',
    className: 'instructions',
    reason: 'Shared Ferry agent instructions are missing',
    remediation: 'Restore AGENTS.md from the instruction snapshot.',
  }, () => {
    const source = requireText(files, join(projectRoot, 'AGENTS.md'), 'instructions');
    if (!source.includes('Host Compatibility') || !hasReviewBoundary(source)) {
      throw new Error('contract missing');
    }
    return { shared_contract: true, review_boundary: true };
  });

  await add({
    id: 'skills',
    className: 'instructions',
    reason: 'One or more Ferry skills are not bridged for Codex',
    remediation: 'Run ./scripts/agent-install.sh to rebuild the skill bridge.',
  }, () => {
    const sourceDir = join(projectRoot, '.claude', 'skills');
    const names = files.list(sourceDir).filter((name) =>
      files.exists(join(sourceDir, name, 'SKILL.md')));
    if (names.length === 0 || names.some((name) =>
      !files.exists(join(projectRoot, '.agents', 'skills', name, 'SKILL.md')))) {
      throw new Error('skill bridge incomplete');
    }
    return { count: names.length };
  });

  await add({
    id: 'hooks',
    className: 'hooks',
    reason: 'Codex hooks are missing the two 60-second native plain-English chat hooks',
    remediation: 'Run ./scripts/agent-install.sh to regenerate Codex hooks.',
  }, () => {
    const source = requireText(files, join(projectRoot, '.codex', 'hooks.json'), 'Codex hooks');
    let document;
    try { document = JSON.parse(source); } catch { throw new Error('hooks invalid'); }
    if (!hasNativeChatHooks(document, ownedRoot)) throw new Error('native chat hooks missing');
    return { native_chat_hooks: 2, timeout_seconds: 60 };
  });

  await add({
    id: 'roles',
    className: 'configuration',
    reason: 'One or more Codex role files are missing',
    remediation: 'Run ./scripts/agent-install.sh to restore Codex role files.',
  }, () => {
    const roles = ['coordinator.toml', 'reviewer.toml', 'explorer.toml', 'locator.toml'];
    if (roles.some((role) => !files.exists(join(projectRoot, '.codex', 'agents', role)))) {
      throw new Error('role missing');
    }
    return { count: roles.length };
  });

  await add({
    id: 'mcp-registration',
    className: 'tool-servers',
    reason: 'One or more required Codex tool servers are not registered',
    remediation: 'Run ./scripts/agent-install.sh to restore qmd, Serena, and Context7.',
  }, () => {
    const source = requireText(files, join(projectRoot, '.codex', 'config.toml'), 'project config');
    const servers = ['qmd', 'serena', 'context7'];
    if (servers.some((server) => !hasActiveLine(source, `[mcp_servers.${server}]`))) {
      throw new Error('tool server missing');
    }
    return { servers };
  });

  await add({
    id: 'worktree-parity',
    className: 'worktrees',
    reason: 'The linked-worktree host contract is incomplete',
    remediation: 'Restore the canonical four-host worktree link contract.',
  }, () => {
    const result = command(process.execPath, [
      join(projectRoot, 'scripts', 'agent-compat', 'check.mjs'),
      '--check-worktree-contract',
      join(ownedRoot, '.claude', 'scripts', 'new-worktree.sh'),
      join(ownedRoot, '.worktreeinclude'),
      join(ownedRoot, 'AGENTS.md'),
      join(ownedRoot, 'CLAUDE.md'),
      join(ownedRoot, '.claude', 'skills', 'df-ship', 'SKILL.md'),
    ], { cwd: projectRoot });
    if (result.status !== 0) throw new Error('worktree check failed');
    return { canonical_hosts: 4, manifest_contract_sources: 3 };
  });

  await add({
    id: 'reviewer-clients',
    className: 'reviewers',
    reason: 'A required cross-host reviewer client is missing',
    remediation: 'Install or repair the Vibe, Qwen, and Claude clients.',
  }, () => {
    const clients = ['vibe', 'qwen', 'claude'];
    if (clients.some((client) => command(client, ['--help'], { cwd: projectRoot }).status !== 0)) {
      throw new Error('reviewer client missing');
    }
    return { clients };
  });

  await add({
    id: 'reviewer-runtime',
    className: 'reviewers',
    reason: 'The approved reviewer runtime is missing, stale, or writable',
    remediation: 'Run node scripts/agent-compat/codex-bootstrap.mjs.',
  }, () => runtimeCheck({ home, root: projectRoot, part: 'runtime' }));

  await add({
    id: 'command-rules',
    className: 'reviewers',
    reason: 'The reviewer command rules do not match the approved runtime',
    remediation: 'Run node scripts/agent-compat/codex-bootstrap.mjs.',
  }, () => runtimeCheck({ home, root: projectRoot, part: 'rules' }));

  await add({
    id: 'generated-state',
    className: 'configuration',
    reason: 'The generated agent state does not match its tracked inputs',
    remediation: 'Run ./scripts/agent-install.sh, then rerun readiness.',
  }, () => {
    const result = command(process.execPath, [
      join(projectRoot, 'scripts', 'agent-compat', 'check.mjs'),
      '--strict',
    ], { cwd: projectRoot });
    if (result.status !== 0) throw new Error('generated state drift');
    return { checked: true };
  });

  return {
    mode: 'static',
    overall: records.some((record) => record.status !== 'ok') ? 'incomplete' : 'ready',
    records,
  };
}

export async function runLiveReadiness({
  root,
  command = liveCommand,
  providerProbe = defaultProviderProbe,
  updateProbe = defaultUpdateProbe,
  now = () => performance.now(),
}) {
  if (!root) throw new Error('live readiness requires root');
  const projectRoot = resolve(root);
  const doctorResult = command('codex', ['doctor', '--json'], { cwd: projectRoot });
  let doctor = null;
  if (doctorResult.status === 0) {
    try { doctor = JSON.parse(doctorResult.stdout); } catch { doctor = null; }
  }
  const records = [];
  const doctorChecks = [
    ['codex-auth', 'authentication', 'auth.credentials', 'Run codex login and rerun readiness.'],
    ['codex-install', 'runtime', 'installation', 'Repair the Codex CLI installation.'],
    ['codex-config', 'configuration', 'config.load', 'Repair Codex config, then rerun readiness.'],
    ['codex-git', 'runtime', 'git.environment', 'Repair Git access for this checkout.'],
    ['codex-sandbox', 'security', 'sandbox.helpers', 'Repair Codex sandbox helpers.'],
  ];
  for (const [id, className, checkId, remediation] of doctorChecks) {
    const started = now();
    const status = doctor?.checks?.[checkId]?.status;
    if (status === 'ok') {
      records.push(readinessRecord(id, className, 'ok', now() - started, null, {
        doctor_check: checkId,
      }));
    } else {
      records.push(safeReadinessFailure(
        id,
        className,
        `Codex doctor check ${checkId} did not pass`,
        remediation,
        now() - started,
      ));
    }
  }

  records.push(await checkRecord({
    id: 'codex-provider-smoke',
    className: 'provider',
    reason: 'The Codex provider or required tool-server calls did not complete',
    remediation: 'Repair Codex provider access and qmd, Serena, or Context7 registration.',
    now,
  }, async () => {
    const details = await providerProbe({ root: projectRoot, command });
    if (details?.marker !== 'FERRY_CODEX_RUNTIME_OK' || details?.model !== 'gpt-5.6-sol' ||
        details?.qmd !== 'ok' || details?.serena !== 'ok' || details?.context7 !== 'ok') {
      throw new Error('provider evidence incomplete');
    }
    return details;
  }));

  const updateStarted = now();
  try {
    const update = await updateProbe({
      installedVersion: doctor?.codexVersion ?? null,
      command,
    });
    records.push(readinessRecord(
      'codex-update-probe',
      'runtime',
      update.current ? 'ok' : 'warning',
      now() - updateStarted,
      update.current ? null : 'Update the Codex CLI and rerun readiness.',
      update.current
        ? { current: true }
        : { current: false, installed: update.installed, latest: update.latest },
    ));
  } catch {
    records.push(readinessRecord(
      'codex-update-probe',
      'runtime',
      'warning',
      now() - updateStarted,
      'Retry readiness when the package registry is reachable.',
      { reason: 'Current-version evidence is unavailable' },
    ));
  }

  return {
    mode: 'live',
    overall: records.some((record) => record.status !== 'ok') ? 'incomplete' : 'ready',
    records,
  };
}

export async function runReviewerReadiness({
  root,
  home = process.env.HOME,
  adapters = { vibe: runVibeReview, qwen: runQwenReview },
  now = () => performance.now(),
}) {
  if (!root || !home) throw new Error('reviewer readiness requires root and home');
  const cleanPrompt = `${buildReviewPrompt({
    mode: 'chunk',
    title: 'Reviewer readiness',
    focus: 'Return a clean review. This payload tests exact-model access only.',
  })}\n\nReviewer readiness marker. No project finding is requested.`;
  const findingPrompt = `${buildReviewPrompt({
    mode: 'chunk',
    title: 'Reviewer readiness',
    focus: [
      'Return exactly one Minor maintainability finding for the readiness marker.',
      'Use verification command exactly: rg -n -- discord-ferry pyproject.toml',
    ].join(' '),
  })}\n\npyproject.toml contains the discord-ferry reviewer readiness marker.`;
  const records = [];
  for (const reviewer of [
    {
      id: 'vibe-reviewer',
      adapter: adapters.vibe,
      slot: 'mistral-vibe',
      model: 'zai-glm-5-2',
      prompt: cleanPrompt,
      requiresFinding: false,
      remediation: 'Repair the Vibe reviewer route or its Proton credential.',
    },
    {
      id: 'qwen-reviewer',
      adapter: adapters.qwen,
      slot: 'qwen',
      model: 'qwen3.8-max',
      prompt: findingPrompt,
      requiresFinding: true,
      remediation: 'Repair the Qwen reviewer route or its Proton credential.',
    },
  ]) {
    const started = now();
    try {
      const result = await reviewer.adapter({
        prompt: reviewer.prompt,
        home,
        slot: reviewer.slot,
      });
      if (result?.status !== 'valid' || result?.resolved_model !== reviewer.model) {
        throw new Error('reviewer evidence mismatch');
      }
      const findingAuthorized = result?.findings?.length === 1
        && authorizeVerificationCommand(
          result.findings[0].verification.command,
          { root },
        ).authorized;
      if (reviewer.requiresFinding && !findingAuthorized) {
        throw new Error('reviewer evidence mismatch');
      }
      records.push(readinessRecord(
        reviewer.id,
        'reviewers',
        'ok',
        now() - started,
        null,
        { model: reviewer.model },
      ));
    } catch {
      records.push(safeReadinessFailure(
        reviewer.id,
        'reviewers',
        'Exact reviewer probe failed',
        reviewer.remediation,
        now() - started,
      ));
    }
  }
  return {
    mode: 'reviewers',
    overall: records.some((record) => record.status !== 'ok') ? 'incomplete' : 'ready',
    records,
  };
}

export async function runWorktreeReadiness({
  root,
  probe = defaultWorktreeProbe,
  command = liveCommand,
  now = () => performance.now(),
}) {
  if (!root || typeof probe !== 'function') {
    throw new Error('worktree readiness requires root and a probe');
  }
  const evidence = await probe({ root: resolve(root), command });
  const records = [];
  const add = async (definition, check) => {
    records.push(await checkRecord({ ...definition, now }, check));
  };

  for (const [id, key] of [
    ['primary-session', 'primary'],
    ['worktree-session', 'worktree'],
  ]) {
    await add({
      id,
      className: 'sessions',
      reason: `${id} did not preserve its instruction, skill, tool, and tree markers`,
      remediation: 'Repair linked-worktree setup and rerun the live readiness probe.',
    }, () => {
      const details = evidence?.[key];
      if (!Array.isArray(details?.markers) || details.markers.length === 0 ||
          details.tree_hash_before !== details.tree_hash_after) {
        throw new Error('session evidence incomplete');
      }
      return details;
    });
  }
  if (JSON.stringify(evidence?.primary?.markers) !== JSON.stringify(evidence?.worktree?.markers)) {
    const worktree = records.find((record) => record.id === 'worktree-session');
    Object.assign(worktree, safeReadinessFailure(
      'worktree-session',
      'sessions',
      'Primary and worktree session markers differ',
      'Repair linked instruction and tool-server state.',
    ));
  }

  for (const [id, key] of [
    ['hook-session-start', 'session_start'],
    ['hook-pre-tool-allow', 'pre_tool_allow'],
    ['hook-pre-tool-block', 'pre_tool_block'],
    ['hook-post-tool', 'post_tool'],
  ]) {
    await add({
      id,
      className: 'hooks',
      reason: `${id} did not return its expected result`,
      remediation: 'Regenerate hooks and rerun the live hook probe.',
    }, () => {
      if (evidence?.hooks?.[key] !== 'ok') throw new Error('hook result mismatch');
      return { result: 'ok' };
    });
  }

  for (const [id, key] of [
    ['stop-main-agent', 'stop_main'],
    ['stop-child-agent', 'stop_child'],
  ]) {
    await add({
      id,
      className: 'hooks',
      reason: `${id} did not finish inside its 60-second outer budget`,
      remediation: 'Regenerate the 60-second native plain-English chat hooks.',
    }, () => {
      const details = evidence?.hooks?.[key];
      if (details?.status !== 'ok' || details?.timeout_seconds !== 60 ||
          !Number.isFinite(details?.duration_ms) || details.duration_ms > 58_000 ||
          details.owner_root !== evidence?.primary?.path ||
          details.command !== codexChatCommand(evidence.primary.path) ||
          typeof details.event_cwd !== 'string' ||
          pathIsWithin(evidence.primary.path, details.event_cwd) ||
          pathIsWithin(evidence.worktree.path, details.event_cwd)) {
        throw new Error('Stop-hook evidence mismatch');
      }
      return {
        timeout_seconds: 60,
        duration_ms: details.duration_ms,
        owner_root: details.owner_root,
        event_cwd: details.event_cwd,
        command: details.command,
      };
    });
  }

  const expectedRoles = {
    coordinator: { model: 'gpt-5.6-sol', sandbox: 'workspace-write' },
    reviewer: { model: 'gpt-5.6-terra', sandbox: 'read-only' },
    explorer: { model: 'gpt-5.6-terra', sandbox: 'read-only' },
    locator: { model: 'gpt-5.6-luna', sandbox: 'read-only' },
  };
  for (const [role, expected] of Object.entries(expectedRoles)) {
    await add({
      id: `role-${role}`,
      className: 'roles',
      reason: `${role} role model or sandbox differs from the generated contract`,
      remediation: 'Regenerate Codex role files and rerun the live role probe.',
    }, () => {
      const details = evidence?.roles?.[role];
      if (details?.model !== expected.model || details?.sandbox !== expected.sandbox ||
          details?.selected !== true) {
        throw new Error('role evidence mismatch');
      }
      return details;
    });
  }

  return {
    mode: 'worktree',
    overall: records.some((record) => record.status !== 'ok') ? 'incomplete' : 'ready',
    records,
  };
}

function selfTest() {
  const record = readinessRecord('self-test', 'internal', 'ok', 0, null, { checked: true });
  if (Object.keys(record).sort().join(',') !==
      'class,details,duration_ms,id,remediation,status') {
    throw new Error('readiness record shape changed');
  }
  const event = (item) => JSON.stringify({ type: 'item.completed', item });
  parseProviderEvents([
    event({ type: 'mcp_tool_call', server: 'qmd', tool: 'status', status: 'completed' }),
    event({
      type: 'mcp_tool_call', server: 'serena', tool: 'get_symbols_overview', status: 'completed',
    }),
    event({
      type: 'mcp_tool_call', server: 'context7', tool: 'resolve-library-id', status: 'completed',
    }),
    event({ type: 'agent_message', text: 'FERRY_CODEX_RUNTIME_OK' }),
  ].join('\n'));
  const parentThreadId = 'parent-thread';
  const roleSessions = ['coordinator', 'reviewer', 'explorer', 'locator'].map((role) => ({
    parent_thread_id: parentThreadId,
    role,
    model: 'fixture',
    sandbox: 'read-only',
    completed: true,
  }));
  parseRoleProbeEvents([
    JSON.stringify({ type: 'thread.started', thread_id: parentThreadId }),
    event({ type: 'agent_message', text: 'FERRY_ROLE_PROBE_OK' }),
  ].join('\n'), '', roleSessions, ['coordinator', 'reviewer', 'explorer', 'locator']);
  process.stdout.write('codex-readiness self-test: all checks passed\n');
}

function option(args, name) {
  const index = args.indexOf(name);
  return index === -1 ? null : args[index + 1];
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('--self-test')) {
    selfTest();
    return;
  }
  const known = new Set(['--root', '--home', '--json', '--live', '--worktree', '--reviewers']);
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (!known.has(argument)) throw new Error(`unknown argument: ${argument}`);
    if (argument === '--root' || argument === '--home') index += 1;
  }
  const root = option(args, '--root') ?? process.cwd();
  const home = option(args, '--home') ?? process.env.HOME;
  let report;
  const requested = [];
  if (args.includes('--live')) requested.push(await runLiveReadiness({ root }));
  if (args.includes('--worktree')) requested.push(await runWorktreeReadiness({ root }));
  if (args.includes('--reviewers')) {
    requested.push(await runReviewerReadiness({ root, home }));
  }
  if (requested.length) {
    report = {
      mode: requested.map((item) => item.mode).join('+'),
      overall: requested.every((item) => item.overall === 'ready') ? 'ready' : 'incomplete',
      records: requested.flatMap((item) => item.records),
    };
  } else report = await runStaticReadiness({ root, home });
  if (args.includes('--json')) process.stdout.write(`${JSON.stringify(report)}\n`);
  else {
    for (const record of report.records) {
      process.stdout.write(`${record.status.toUpperCase()} ${record.id}\n`);
    }
  }
  if (report.overall !== 'ready') process.exitCode = 1;
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch { /* an invalid entrypoint cannot be the current module */ }
}

if (invokedAsMain) {
  main().catch((error) => {
    process.stderr.write(`codex-readiness: ${error.message}\n`);
    process.exitCode = 1;
  });
}
