#!/usr/bin/env node

import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';

export const LEDGER_SCHEMA_VERSION = 1;
export const WORKFLOW_STATES = Object.freeze([
  'prepared',
  'active',
  'suspended',
  'cancelled',
  'closed',
]);
export const CHALLENGE_METHODS = Object.freeze([
  'test_runner',
  'prototype_measurement',
  'concrete_comparison',
  'cost_comparison',
]);

const SHORT_CONTINUATIONS = new Set(['ok', 'yes', 'go ahead', 'continue', 'next']);
const STATE_DIRECTORY = 'docs/plans/.brainstorm-evidence';
const SPECIFICATION_PATTERN = /^docs\/plans\/specs\/(?!.*-scenarios\.md$).+\.md$/u;
const DESIGN_PATTERN = /^docs\/plans\/designs\/.+\.md$/u;
const MAX_RESULT_CHARACTERS = 65_536;
const MAX_RESULT_LINES = 4_000;
const SAFE_RG_FLAGS = new Set([
  '-n', '--line-number', '-F', '--fixed-strings', '-i', '--ignore-case',
  '-l', '--files-with-matches', '-c', '--count', '--no-heading', '--color=never',
]);

export function brainstormPaths(root) {
  const stateRoot = resolve(root, STATE_DIRECTORY);
  return Object.freeze({
    stateRoot,
    ledger: resolve(stateRoot, 'ledger.json'),
    promptMarkers: resolve(stateRoot, 'prompt-markers'),
    pendingReceipts: resolve(stateRoot, 'receipts', 'pending'),
    completedReceipts: resolve(stateRoot, 'receipts', 'completed'),
  });
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function readLedger(path) {
  if (!existsSync(path)) return null;
  try {
    const value = JSON.parse(readFileSync(path, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function writeJsonAtomic(path, value) {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  chmodSync(parent, 0o700);
  const content = `${JSON.stringify(value, null, 2)}\n`;
  const temporary = resolve(parent, `.state.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    chmodSync(temporary, 0o600);
    if (readFileSync(temporary, 'utf8') !== content) {
      throw new Error('brainstorm ledger staging verification failed');
    }
    renameSync(temporary, path);
    chmodSync(path, 0o600);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function writeJsonExclusive(path, value) {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  chmodSync(parent, 0o700);
  try {
    writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    });
    chmodSync(path, 0o600);
    return true;
  } catch (error) {
    if (error?.code === 'EEXIST') return false;
    throw error;
  }
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const members = Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`);
    return `{${members.join(',')}}`;
  }
  return JSON.stringify(value);
}

function jsonDigest(value) {
  return sha256(canonicalJson(value));
}

function worktreeRelativePath(root, candidate) {
  if (typeof candidate !== 'string' || candidate.length === 0) return null;
  const canonicalRoot = realpathSync(root);
  const resolvedCandidate = isAbsolute(candidate)
    ? resolve(candidate)
    : resolve(canonicalRoot, candidate);
  if (!existsSync(resolvedCandidate)) return null;
  const canonicalCandidate = realpathSync(resolvedCandidate);
  const local = relative(canonicalRoot, canonicalCandidate);
  if (local === '' || local === '..' || local.startsWith(`..${sep}`) || isAbsolute(local)) {
    return null;
  }
  return local.split(sep).join('/');
}

function requirementsLedger(root, requirementsPath) {
  return {
    schema_version: LEDGER_SCHEMA_VERSION,
    generation: randomUUID(),
    requirements_path: requirementsPath,
    requirements_sha256: sha256(readFileSync(resolve(root, requirementsPath))),
    state: 'prepared',
    required_next_step: 'brainstorm',
  };
}

function existingPlanSources(root) {
  const plansRoot = resolve(root, 'docs/plans');
  if (!existsSync(plansRoot)) return [];
  const found = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      const local = relative(root, path).split(sep).join('/');
      if (local.startsWith(`${STATE_DIRECTORY}/`) || local.startsWith('docs/plans/.review/')) {
        continue;
      }
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile() && entry.name.endsWith('.md')) found.push(local);
    }
  };
  visit(plansRoot);
  return found.sort();
}

function activateLedger(root, ledger, input, host, kind) {
  return {
    ...ledger,
    state: 'active',
    activation: activation(input, host, kind),
    activation_source_paths: existingPlanSources(root),
  };
}

function expectedDesignPath(requirementsPath) {
  return requirementsPath.replace(/^docs\/plans\/specs\//u, 'docs/plans/designs/');
}

export function handlePostEdit(input, { root }) {
  const paths = brainstormPaths(root);
  const editedPath = worktreeRelativePath(root, input?.tool_input?.file_path);
  if (editedPath === null) return null;

  if (editedPath.startsWith(`${STATE_DIRECTORY}/receipts/`)) {
    unlinkSync(resolve(root, editedPath));
    return null;
  }

  if (SPECIFICATION_PATTERN.test(editedPath)) {
    const current = readLedger(paths.ledger);
    const currentHash = sha256(readFileSync(resolve(root, editedPath)));
    if (
      current?.requirements_path === editedPath
      && current.requirements_sha256 === currentHash
    ) {
      return current;
    }
    const next = requirementsLedger(root, editedPath);
    writeJsonAtomic(paths.ledger, next);
    return next;
  }

  const current = readLedger(paths.ledger);
  if (
    current?.state !== 'active'
    || !DESIGN_PATTERN.test(editedPath)
    || expectedDesignPath(current.requirements_path) !== editedPath
  ) {
    return current;
  }
  const closed = {
    ...current,
    state: 'closed',
    design_path: editedPath,
    design_sha256: sha256(readFileSync(resolve(root, editedPath))),
  };
  writeJsonAtomic(paths.ledger, closed);
  return closed;
}

function normalizedPrompt(input) {
  return typeof input === 'string' ? input.trim().replace(/\s+/gu, ' ').toLowerCase() : '';
}

export function classifyPrompt(prompt, _ledger = null) {
  if (/^[/$]df-brainstorm\s+cancel$/iu.test(prompt)) return 'cancel';
  if (/^[/$]df-brainstorm(?:\s+.*)?$/iu.test(prompt)) return 'invoke';
  if (SHORT_CONTINUATIONS.has(normalizedPrompt(prompt))) return 'continue';
  return 'other';
}

export function promptIdentity(input, host) {
  const sessionId = input?.session_id;
  const turnId = host === 'claude' ? input?.prompt_id : input?.turn_id;
  if (
    !['claude', 'codex'].includes(host)
    || typeof sessionId !== 'string'
    || sessionId.length === 0
    || typeof turnId !== 'string'
    || turnId.length === 0
  ) {
    return null;
  }
  return {
    host,
    session_id: sessionId,
    turn_id: turnId,
  };
}

function markerKey(identity) {
  return sha256(`${identity.host}\0${identity.session_id}\0${identity.turn_id}`);
}

function markerRecords(directory) {
  if (!existsSync(directory)) return [];
  const records = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    const path = resolve(directory, entry.name);
    try {
      records.push({ path, value: JSON.parse(readFileSync(path, 'utf8')) });
    } catch {
      records.push({ path, value: null });
    }
  }
  return records;
}

function writePromptMarker(paths, identity, classification) {
  for (const record of markerRecords(paths.promptMarkers)) {
    if (
      record.value?.host === identity.host
      && record.value.session_id === identity.session_id
    ) {
      unlinkSync(record.path);
    }
  }
  const marker = {
    schema_version: LEDGER_SCHEMA_VERSION,
    ...identity,
    classification,
  };
  writeJsonAtomic(resolve(paths.promptMarkers, `${markerKey(identity)}.json`), marker);
  return marker;
}

function matchingPromptMarker(paths, input, host) {
  const sessionId = input?.session_id;
  if (typeof sessionId !== 'string' || sessionId.length === 0) return null;
  const turnId = host === 'codex' ? input?.turn_id : null;
  const records = markerRecords(paths.promptMarkers).filter((record) =>
    record.value?.schema_version === LEDGER_SCHEMA_VERSION
      && record.value.host === host
      && record.value.session_id === sessionId
      && (typeof turnId !== 'string' || record.value.turn_id === turnId));
  return records.length === 1 ? records[0] : null;
}

function clearPendingReceipts(paths) {
  if (!existsSync(paths.pendingReceipts)) return;
  for (const entry of readdirSync(paths.pendingReceipts, { withFileTypes: true })) {
    if (entry.isFile() || entry.isSymbolicLink()) {
      unlinkSync(resolve(paths.pendingReceipts, entry.name));
    }
  }
}

function activation(input, host, kind) {
  return { ...promptIdentity(input, host), kind };
}

export function handlePrompt(input, { host, root }) {
  const paths = brainstormPaths(root);
  const current = readLedger(paths.ledger);
  const kind = classifyPrompt(input?.prompt, current);
  const identity = promptIdentity(input, host);
  if (identity !== null) writePromptMarker(paths, identity, kind);
  if (current === null) return null;

  if (kind === 'cancel' && ['prepared', 'active'].includes(current.state)) {
    const cancelled = { ...current, state: 'cancelled' };
    delete cancelled.activation;
    writeJsonAtomic(paths.ledger, cancelled);
    return cancelled;
  }

  if (kind === 'continue' && current.state === 'prepared') {
    const active = activateLedger(root, current, input, host, kind);
    writeJsonAtomic(paths.ledger, active);
    return active;
  }

  if (kind === 'invoke' && current.state === 'prepared') {
    const active = activateLedger(root, current, input, host, kind);
    writeJsonAtomic(paths.ledger, active);
    return active;
  }

  if (kind === 'invoke' && ['cancelled', 'suspended'].includes(current.state)) {
    const restarted = activateLedger(
      root,
      requirementsLedger(root, current.requirements_path),
      input,
      host,
      kind,
    );
    writeJsonAtomic(paths.ledger, restarted);
    return restarted;
  }

  return current;
}

function trackedPath(root, path) {
  try {
    execFileSync('git', ['ls-files', '--error-unmatch', '--', path], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'ignore', 'ignore'],
    });
    return true;
  } catch {
    return false;
  }
}

function repositorySource(root, ledger, candidate) {
  const path = worktreeRelativePath(root, candidate);
  if (path === null) return null;
  if (path === STATE_DIRECTORY || path.startsWith(`${STATE_DIRECTORY}/`)) return null;
  if (path === expectedDesignPath(ledger.requirements_path)) return null;
  const existedAtActivation = ledger.activation_source_paths?.includes(path) === true;
  if (!trackedPath(root, path) && !existedAtActivation) return null;
  return {
    type: 'repository',
    locator: path,
    sha256: sha256(readFileSync(resolve(root, path))),
  };
}

function publicHttpsUrl(value) {
  if (typeof value !== 'string') return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  const hostname = url.hostname.toLowerCase();
  if (
    url.protocol !== 'https:'
    || url.username !== ''
    || url.password !== ''
    || url.search !== ''
    || url.hash !== ''
    || (url.port !== '' && url.port !== '443')
    || hostname === 'localhost'
    || hostname.endsWith('.localhost')
    || hostname.endsWith('.local')
    || isIP(hostname) !== 0
  ) {
    return null;
  }
  return url.toString();
}

function literalArguments(command) {
  if (typeof command !== 'string' || command.length === 0) return null;
  if (/[;&|<>`$\\\n\r]/u.test(command) || /^[A-Za-z_][A-Za-z0-9_]*=/u.test(command)) {
    return null;
  }
  const arguments_ = [];
  let token = '';
  let quote = null;
  for (const character of command.trim()) {
    if (quote !== null) {
      if (character === quote) quote = null;
      else token += character;
    } else if (character === "'" || character === '"') {
      quote = character;
    } else if (/\s/u.test(character)) {
      if (token.length > 0) {
        arguments_.push(token);
        token = '';
      }
    } else {
      token += character;
    }
  }
  if (quote !== null) return null;
  if (token.length > 0) arguments_.push(token);
  return arguments_.length > 0 ? arguments_ : null;
}

function repositoryCommandSource(root, ledger, arguments_) {
  if (arguments_[0] === 'rg') {
    let index = 1;
    while (SAFE_RG_FLAGS.has(arguments_[index])) index += 1;
    if (arguments_[index] === '--') index += 1;
    if (index + 2 !== arguments_.length) return null;
    return repositorySource(root, ledger, arguments_[index + 1]);
  }
  if (
    arguments_[0] === 'sed'
    && arguments_.length === 4
    && arguments_[1] === '-n'
    && /^\d+(?:,\d+)?p$/u.test(arguments_[2])
  ) {
    return repositorySource(root, ledger, arguments_[3]);
  }
  return null;
}

function gitSource(root, arguments_) {
  if (arguments_[0] !== 'git' || arguments_[1] !== 'show' || arguments_.length !== 3) return null;
  const separatorIndex = arguments_[2].indexOf(':');
  if (separatorIndex <= 0) return null;
  const revision = arguments_[2].slice(0, separatorIndex);
  const path = arguments_[2].slice(separatorIndex + 1);
  if (!/^[A-Za-z0-9._~^/-]+$/u.test(revision) || !/^[A-Za-z0-9._/-]+$/u.test(path)) return null;
  let content;
  try {
    content = execFileSync('git', ['show', `${revision}:${path}`], {
      cwd: root,
      encoding: null,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
  } catch {
    return null;
  }
  return { type: 'git', locator: `${revision}:${path}`, sha256: sha256(content) };
}

function commandSource(root, ledger, command) {
  const arguments_ = literalArguments(command);
  if (arguments_ === null) return null;
  const repository = repositoryCommandSource(root, ledger, arguments_);
  if (repository !== null) return repository;
  const git = gitSource(root, arguments_);
  if (git !== null) return git;
  if (
    arguments_.length === 3
    && arguments_[0] === 'curl'
    && ['-L', '--location'].includes(arguments_[1])
  ) {
    const locator = publicHttpsUrl(arguments_[2]);
    return locator === null
      ? null
      : { type: 'web', locator, sha256: sha256(locator) };
  }
  return null;
}

function artifactFingerprint(root, candidate) {
  if (typeof candidate !== 'string' || isAbsolute(candidate)) return null;
  const path = worktreeRelativePath(root, candidate);
  if (path === null || path.startsWith(`${STATE_DIRECTORY}/`)) return null;
  if (!statSync(resolve(root, path)).isFile()) return null;
  return { path, sha256: sha256(readFileSync(resolve(root, path))) };
}

export function measureArtifacts(root, relativePaths) {
  if (!Array.isArray(relativePaths) || relativePaths.length === 0) return null;
  const fingerprints = [];
  let substantiveLines = 0;
  for (const candidate of [...new Set(relativePaths)]) {
    const fingerprint = artifactFingerprint(root, candidate);
    if (fingerprint === null) return null;
    fingerprints.push(fingerprint);
    const text = readFileSync(resolve(root, fingerprint.path), 'utf8');
    substantiveLines += text.replace(/\r\n?/gu, '\n').split('\n')
      .filter((line) => line.trim().length > 0).length;
  }
  return {
    file_count: fingerprints.length,
    substantive_lines: substantiveLines,
    fingerprints,
  };
}

function supportedRunner(arguments_) {
  if (
    arguments_.length === 4
    && arguments_[0] === 'uv'
    && arguments_[1] === 'run'
    && arguments_[2] === 'pytest'
    && arguments_[3].endsWith('.py')
  ) {
    return { runner: 'pytest', path: arguments_[3] };
  }
  if (
    arguments_.length === 3
    && arguments_[0] === 'node'
    && arguments_[1] === '--test'
    && /\.(?:c|m)?js$/u.test(arguments_[2])
  ) {
    return { runner: 'node_test', path: arguments_[2] };
  }
  return null;
}

function supportedPrototype(arguments_) {
  if (
    arguments_.length === 2
    && arguments_[0] === 'node'
    && arguments_[1].endsWith('.mjs')
  ) {
    return { runner: 'node', path: arguments_[1] };
  }
  if (
    arguments_.length === 4
    && arguments_[0] === 'uv'
    && arguments_[1] === 'run'
    && arguments_[2] === 'python'
    && arguments_[3].endsWith('.py')
  ) {
    return { runner: 'python', path: arguments_[3] };
  }
  return null;
}

function completedReceipt(paths, receiptId, ledger) {
  if (typeof receiptId !== 'string' || !/^[a-f0-9]{64}$/u.test(receiptId)) return null;
  const path = resolve(paths.completedReceipts, `${receiptId}.json`);
  const receipt = readLedger(path);
  if (receipt === null || receipt.integrity_sha256 === undefined) return null;
  const { integrity_sha256: integritySha256, ...withoutIntegrity } = receipt;
  if (
    integritySha256 !== jsonDigest(withoutIntegrity)
    || receipt.kind !== 'source'
    || receipt.status !== 'completed'
    || receipt.generation !== ledger.generation
    || receipt.requirements_sha256 !== ledger.requirements_sha256
  ) {
    return null;
  }
  return receipt;
}

function costCalculation(challenge, paths, ledger) {
  if (
    !Array.isArray(challenge.inputs)
    || challenge.inputs.length === 0
    || challenge.calculation?.operator !== 'divide'
    || typeof challenge.calculation.left !== 'string'
    || typeof challenge.calculation.right !== 'string'
  ) {
    return null;
  }
  const inputs = new Map();
  const receiptIds = [];
  for (const input of challenge.inputs) {
    if (
      typeof input?.name !== 'string'
      || typeof input.value !== 'number'
      || !Number.isFinite(input.value)
      || completedReceipt(paths, input.receipt_id, ledger) === null
      || inputs.has(input.name)
    ) {
      return null;
    }
    inputs.set(input.name, input.value);
    receiptIds.push(input.receipt_id);
  }
  const left = inputs.get(challenge.calculation.left);
  const right = inputs.get(challenge.calculation.right);
  if (left === undefined || right === undefined || right === 0) return null;
  return {
    value: left / right,
    source_receipt_ids: [...new Set(receiptIds)].sort(),
  };
}

function normalizedChallengeDeclaration(challenge, input, { root, ledger }) {
  if (
    challenge === null
    || typeof challenge !== 'object'
    || typeof challenge.id !== 'string'
    || challenge.id.length === 0
    || typeof challenge.alternative_id !== 'string'
    || challenge.alternative_id.length === 0
    || typeof challenge.claim !== 'string'
    || challenge.claim.length === 0
    || challenge.falsifying_outcome === null
    || typeof challenge.falsifying_outcome !== 'object'
    || Array.isArray(challenge.falsifying_outcome)
    || !CHALLENGE_METHODS.includes(challenge.method)
  ) {
    return null;
  }
  const toolName = input?.tool_name;
  if (!['Bash', 'bash', 'exec_command'].includes(toolName)) return null;
  const arguments_ = literalArguments(input?.tool_input?.command ?? input?.tool_input?.cmd);
  if (arguments_ === null) return null;
  const paths = brainstormPaths(root);
  let runner;
  let measures;
  let artifactPaths;
  let cost = null;

  if (challenge.method === 'test_runner') {
    runner = supportedRunner(arguments_);
    artifactPaths = challenge.artifacts;
    if (
      runner === null
      || !Array.isArray(artifactPaths)
      || artifactPaths.length !== 1
      || artifactPaths[0] !== runner.path
    ) {
      return null;
    }
    measures = measureArtifacts(root, artifactPaths);
  } else if (challenge.method === 'prototype_measurement') {
    runner = supportedPrototype(arguments_);
    artifactPaths = challenge.artifacts;
    if (
      runner === null
      || !Array.isArray(artifactPaths)
      || !artifactPaths.includes(runner.path)
    ) {
      return null;
    }
    measures = measureArtifacts(root, artifactPaths);
  } else if (challenge.method === 'concrete_comparison') {
    runner = supportedPrototype(arguments_);
    const selected = challenge.artifact_sets?.selected;
    const alternative = challenge.artifact_sets?.alternative;
    if (
      runner === null
      || runner.path !== challenge.runner_path
      || !Array.isArray(selected)
      || !Array.isArray(alternative)
      || selected.length === 0
      || alternative.length === 0
    ) {
      return null;
    }
    const selectedMeasures = measureArtifacts(root, selected);
    const alternativeMeasures = measureArtifacts(root, alternative);
    if (selectedMeasures === null || alternativeMeasures === null) return null;
    artifactPaths = [...new Set([...selected, ...alternative])];
    measures = { selected: selectedMeasures, alternative: alternativeMeasures };
  } else {
    runner = supportedPrototype(arguments_);
    artifactPaths = challenge.artifacts;
    if (
      runner === null
      || runner.path !== challenge.runner_path
      || !Array.isArray(artifactPaths)
      || !artifactPaths.includes(runner.path)
    ) {
      return null;
    }
    measures = measureArtifacts(root, artifactPaths);
    cost = costCalculation(challenge, paths, ledger);
    if (cost === null) return null;
  }
  if (measures === null) return null;
  return {
    challenge_id: challenge.id,
    alternative_id: challenge.alternative_id,
    method: challenge.method,
    runner: runner.runner,
    claim_sha256: sha256(challenge.claim),
    falsifying_outcome_sha256: jsonDigest(challenge.falsifying_outcome),
    falsifying_outcome: challenge.falsifying_outcome,
    artifact_measurements: measures,
    ...(cost === null ? {} : { cost }),
  };
}

export function normalizeChallenge(input, ledger, options) {
  if (!Array.isArray(ledger?.alternative_challenges)) return null;
  const matches = ledger.alternative_challenges
    .map((challenge) => normalizedChallengeDeclaration(challenge, input, {
      ...options,
      ledger,
    }))
    .filter((challenge) => challenge !== null);
  return matches.length === 1 ? matches[0] : null;
}

export function normalizeSourceCall(input, { root, ledger }) {
  const toolName = input?.tool_name;
  const toolInput = input?.tool_input;
  if (typeof toolName !== 'string' || toolInput === null || typeof toolInput !== 'object') {
    return null;
  }
  if (['Read', 'read_file'].includes(toolName)) {
    return repositorySource(root, ledger, toolInput.file_path ?? toolInput.path);
  }
  if (['Grep', 'grep', 'grep_search'].includes(toolName)) {
    return repositorySource(root, ledger, toolInput.path ?? toolInput.file_path);
  }
  if (['Bash', 'bash', 'exec_command'].includes(toolName)) {
    return commandSource(root, ledger, toolInput.command ?? toolInput.cmd);
  }
  if (['WebFetch', 'web_fetch'].includes(toolName)) {
    const locator = publicHttpsUrl(toolInput.url);
    return locator === null
      ? null
      : { type: 'web', locator, sha256: sha256(locator) };
  }
  if (
    ['mcp__context7__query-docs', 'mcp__context7__query_docs'].includes(toolName)
    && typeof toolInput.libraryId === 'string'
    && /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/u.test(toolInput.libraryId)
  ) {
    const locator = toolInput.libraryId;
    return {
      type: 'documentation',
      locator,
      sha256: sha256(`context7\0${locator}`),
    };
  }
  return null;
}

function receiptCoordinates(input, host) {
  const sessionId = input?.session_id;
  const toolUseId = input?.tool_use_id;
  const turnId = input?.turn_id ?? null;
  if (
    typeof sessionId !== 'string'
    || sessionId.length === 0
    || typeof toolUseId !== 'string'
    || toolUseId.length === 0
  ) {
    return null;
  }
  return { host, sessionId, turnId, toolUseId };
}

export function receiptKey({ host, sessionId, turnId, toolUseId }) {
  return sha256(canonicalJson({ host, sessionId, turnId, toolUseId }));
}

export function handleBeforeTool(input, { host, root }) {
  const paths = brainstormPaths(root);
  const ledger = readLedger(paths.ledger);
  if (ledger?.state !== 'active') return null;
  const coordinates = receiptCoordinates(input, host);
  if (coordinates === null) return null;
  const challenge = normalizeChallenge(input, ledger, { root });
  const source = normalizeSourceCall(input, { root, ledger });
  if (challenge === null && source === null) return null;
  const id = receiptKey(coordinates);
  const common = {
    schema_version: LEDGER_SCHEMA_VERSION,
    receipt_id: id,
    status: 'pending',
    host,
    session_id: coordinates.sessionId,
    turn_id: coordinates.turnId,
    tool_use_id: coordinates.toolUseId,
    tool_name: input.tool_name,
    input_sha256: jsonDigest({ tool_name: input.tool_name, tool_input: input.tool_input }),
    generation: ledger.generation,
    requirements_sha256: ledger.requirements_sha256,
    ledger_sha256: jsonDigest(ledger),
  };
  const pending = challenge === null
    ? { ...common, kind: 'source', source }
    : { ...common, kind: 'challenge', ...challenge };
  const pendingPath = resolve(paths.pendingReceipts, `${id}.json`);
  return writeJsonExclusive(pendingPath, pending) ? pending : null;
}

function responseText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(responseText).filter(Boolean).join('\n');
  if (value === null || typeof value !== 'object') return '';
  for (const key of ['text', 'output', 'stdout', 'content', 'result']) {
    if (key in value) {
      const text = responseText(value[key]);
      if (text !== '') return text;
    }
  }
  return '';
}

function normalizedResult(value) {
  const boundedText = responseText(value).slice(0, MAX_RESULT_CHARACTERS);
  const lines = boundedText.replace(/\r\n?/gu, '\n').split('\n').slice(0, MAX_RESULT_LINES);
  const normalizedLines = lines
    .map((line) => line.trim().replace(/^\d+[:-]/u, '').trim().replace(/\s+/gu, ' '))
    .filter(Boolean);
  return {
    result_sha256: sha256(boundedText),
    line_hashes: [...new Set(normalizedLines.map((line) => sha256(line)))],
  };
}

export function normalizeToolOutcome(input, eventName, _host) {
  const response = input?.tool_response;
  if (response === null || typeof response !== 'object') return null;
  const exitCode = response.exit_code;
  if (!Number.isInteger(exitCode)) return null;
  if (eventName === 'PostToolUseFailure' && exitCode === 0) return null;
  return {
    exit_code: exitCode,
    status: exitCode === 0 ? 'success' : 'failure',
  };
}

function falsifyingOutcomeMatched(pending, actualResult) {
  const expected = pending.falsifying_outcome;
  if (pending.method === 'test_runner') {
    return Number.isInteger(expected?.exit_code)
      && actualResult.exit_code === expected.exit_code;
  }
  if (actualResult.status !== 'success') return false;
  if (pending.method === 'prototype_measurement') {
    return typeof expected?.max_substantive_lines === 'number'
      && pending.artifact_measurements.substantive_lines <= expected.max_substantive_lines;
  }
  if (pending.method === 'concrete_comparison') {
    const left = pending.artifact_measurements?.[expected?.left]?.[expected?.metric];
    const right = pending.artifact_measurements?.[expected?.right]?.[expected?.metric];
    return expected?.operator === 'lte'
      && typeof left === 'number'
      && typeof right === 'number'
      && left <= right;
  }
  return expected?.operator === 'gte'
    && typeof expected.value === 'number'
    && pending.cost?.value >= expected.value;
}

function completeChallengeReceipt(input, eventName, host, root, pending, ledger) {
  const current = normalizeChallenge(input, ledger, { root });
  const outcome = normalizeToolOutcome(input, eventName, host);
  if (
    current === null
    || outcome === null
    || canonicalJson(current) !== canonicalJson({
      challenge_id: pending.challenge_id,
      alternative_id: pending.alternative_id,
      method: pending.method,
      runner: pending.runner,
      claim_sha256: pending.claim_sha256,
      falsifying_outcome_sha256: pending.falsifying_outcome_sha256,
      falsifying_outcome: pending.falsifying_outcome,
      artifact_measurements: pending.artifact_measurements,
      ...(pending.cost === undefined ? {} : { cost: pending.cost }),
    })
  ) {
    return null;
  }
  const actualResult = outcome;
  const completedWithoutIntegrity = {
    ...pending,
    status: 'completed',
    outcome: outcome.status,
    result_sha256: jsonDigest(actualResult),
    actual_result: actualResult,
    falsified: falsifyingOutcomeMatched(pending, actualResult),
  };
  return {
    ...completedWithoutIntegrity,
    integrity_sha256: jsonDigest(completedWithoutIntegrity),
  };
}

export function handleToolResult(input, { host, root }) {
  const paths = brainstormPaths(root);
  const coordinates = receiptCoordinates(input, host);
  if (coordinates === null) return null;
  const id = receiptKey(coordinates);
  const pendingPath = resolve(paths.pendingReceipts, `${id}.json`);
  if (!existsSync(pendingPath)) return null;
  const completedPath = resolve(paths.completedReceipts, `${id}.json`);
  if (existsSync(completedPath)) {
    unlinkSync(pendingPath);
    return null;
  }
  const pending = readLedger(pendingPath);
  const ledger = readLedger(paths.ledger);
  const inputSha256 = jsonDigest({ tool_name: input?.tool_name, tool_input: input?.tool_input });
  const commonValid = pending?.status === 'pending'
    && ledger?.state === 'active'
    && pending.generation === ledger.generation
    && pending.requirements_sha256 === ledger.requirements_sha256
    && pending.ledger_sha256 === jsonDigest(ledger)
    && pending.input_sha256 === inputSha256;
  if (!commonValid) {
    unlinkSync(pendingPath);
    return null;
  }
  const eventName = input?.hook_event_name ?? input?.event_name ?? input?.event;
  if (pending.kind === 'challenge') {
    const completed = completeChallengeReceipt(input, eventName, host, root, pending, ledger);
    if (completed === null) {
      unlinkSync(pendingPath);
      return null;
    }
    writeJsonAtomic(completedPath, completed);
    unlinkSync(pendingPath);
    return completed;
  }
  const source = normalizeSourceCall(input, { root, ledger });
  if (
    eventName !== 'PostToolUse'
    ||
    pending.kind !== 'source'
    || source === null
    || canonicalJson(pending.source) !== canonicalJson(source)
  ) {
    unlinkSync(pendingPath);
    return null;
  }
  const result = normalizedResult(input?.tool_response);
  const completedWithoutIntegrity = {
    ...pending,
    status: 'completed',
    outcome: 'success',
    ...result,
  };
  const completed = {
    ...completedWithoutIntegrity,
    integrity_sha256: jsonDigest(completedWithoutIntegrity),
  };
  writeJsonAtomic(completedPath, completed);
  unlinkSync(pendingPath);
  return completed;
}

function completedReceiptRecords(paths) {
  if (!existsSync(paths.completedReceipts)) return [];
  return readdirSync(paths.completedReceipts, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith('.json'))
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((entry) => ({
      file_id: entry.name.slice(0, -'.json'.length),
      receipt: readLedger(resolve(paths.completedReceipts, entry.name)),
    }));
}

function validationFinding(check, identifiers, reason) {
  return { check, identifiers, reason };
}

function normalizedQuotedLine(value) {
  return typeof value === 'string' ? value.trim().replace(/\s+/gu, ' ') : '';
}

function receiptIntegrityFinding(record, ledger) {
  const id = record.file_id;
  const receipt = record.receipt;
  if (receipt?.schema_version !== LEDGER_SCHEMA_VERSION || receipt.receipt_id !== id) {
    return validationFinding(
      'receipt_schema',
      [id],
      `Brainstorm evidence record has an invalid schema: ${id}.`,
    );
  }
  if (receipt.generation !== ledger.generation) {
    return validationFinding(
      'receipt_generation',
      [id],
      `Brainstorm evidence belongs to another generation: ${id}.`,
    );
  }
  if (receipt.requirements_sha256 !== ledger.requirements_sha256) {
    return validationFinding(
      'receipt_requirements',
      [id],
      `Brainstorm evidence belongs to different requirements: ${id}.`,
    );
  }
  const { integrity_sha256: integritySha256, ...withoutIntegrity } = receipt;
  if (integritySha256 !== jsonDigest(withoutIntegrity)) {
    return validationFinding(
      'receipt_integrity',
      [id],
      `Brainstorm evidence record is damaged: ${id}.`,
    );
  }
  return null;
}

function currentArtifactFingerprints(root, measurements) {
  if (measurements === null || typeof measurements !== 'object') return null;
  if (Array.isArray(measurements.fingerprints)) {
    const current = [];
    for (const stored of measurements.fingerprints) {
      const fingerprint = artifactFingerprint(root, stored?.path);
      if (fingerprint === null) return null;
      current.push(fingerprint);
    }
    return current;
  }
  const current = {};
  for (const [name, value] of Object.entries(measurements)) {
    const fingerprints = currentArtifactFingerprints(root, value);
    if (fingerprints === null) return null;
    current[name] = fingerprints;
  }
  return current;
}

function storedArtifactFingerprints(measurements) {
  if (measurements === null || typeof measurements !== 'object') return null;
  if (Array.isArray(measurements.fingerprints)) return measurements.fingerprints;
  const stored = {};
  for (const [name, value] of Object.entries(measurements)) {
    const fingerprints = storedArtifactFingerprints(value);
    if (fingerprints === null) return null;
    stored[name] = fingerprints;
  }
  return stored;
}

function challengeArtifactsUnchanged(root, receipt) {
  const stored = storedArtifactFingerprints(receipt.artifact_measurements);
  const current = currentArtifactFingerprints(root, receipt.artifact_measurements);
  return stored !== null && current !== null && canonicalJson(stored) === canonicalJson(current);
}

function drawbackIdentifiers(approaches) {
  const identifiers = [];
  for (const approach of approaches) {
    if (!Array.isArray(approach?.drawbacks)) return null;
    for (const drawback of approach.drawbacks) {
      if (typeof drawback?.id !== 'string' || drawback.id.length === 0) return null;
      identifiers.push(drawback.id);
    }
  }
  return identifiers;
}

export function validateRecommendation(ledger, receipts, { root }) {
  if (
    ledger?.schema_version !== LEDGER_SCHEMA_VERSION
    || typeof ledger.generation !== 'string'
    || ledger.generation.length === 0
    || typeof ledger.requirements_path !== 'string'
    || !SPECIFICATION_PATTERN.test(ledger.requirements_path)
    || worktreeRelativePath(root, ledger.requirements_path) !== ledger.requirements_path
    || typeof ledger.requirements_sha256 !== 'string'
  ) {
    return validationFinding(
      'ledger_schema',
      ['ledger'],
      'Brainstorm evidence state is invalid: ledger.',
    );
  }
  const requirementsPath = resolve(root, ledger.requirements_path);
  if (
    !existsSync(requirementsPath)
    || sha256(readFileSync(requirementsPath)) !== ledger.requirements_sha256
  ) {
    return validationFinding(
      'requirements',
      [ledger.requirements_path],
      `Brainstorm requirements changed: ${ledger.requirements_path}.`,
    );
  }
  const receiptById = new Map();
  for (const record of receipts) {
    const finding = receiptIntegrityFinding(record, ledger);
    if (finding !== null) return finding;
    if (receiptById.has(record.receipt.receipt_id)) {
      return validationFinding(
        'receipt_duplicate',
        [record.receipt.receipt_id],
        `Brainstorm evidence record is duplicated: ${record.receipt.receipt_id}.`,
      );
    }
    receiptById.set(record.receipt.receipt_id, record.receipt);
  }
  if (!Array.isArray(ledger.approaches) || ledger.approaches.length === 0) {
    return validationFinding(
      'approaches',
      ['approaches'],
      'Brainstorm recommendation is missing required evidence: approaches.',
    );
  }
  const approachIds = ledger.approaches.map((approach) => approach?.id);
  if (
    approachIds.some((id) => typeof id !== 'string' || id.length === 0)
    || new Set(approachIds).size !== approachIds.length
  ) {
    return validationFinding(
      'approaches',
      ['approaches'],
      'Brainstorm approach identifiers are invalid: approaches.',
    );
  }
  const drawbacks = drawbackIdentifiers(ledger.approaches);
  if (drawbacks === null || new Set(drawbacks).size !== drawbacks.length) {
    return validationFinding(
      'drawbacks',
      ['drawbacks'],
      'Brainstorm drawback identifiers are invalid: drawbacks.',
    );
  }
  const resolutions = Array.isArray(ledger.drawback_resolutions)
    ? ledger.drawback_resolutions
    : [];
  for (const drawbackId of drawbacks) {
    const matches = resolutions.filter((resolution) => resolution?.drawback_id === drawbackId);
    const resolution = matches.length === 1 ? matches[0] : null;
    const receipt = receiptById.get(resolution?.receipt_id);
    const quote = normalizedQuotedLine(resolution?.quote);
    if (
      resolution?.status !== 'resolved'
      || typeof resolution.finding !== 'string'
      || resolution.finding.length === 0
      || receipt?.kind !== 'source'
      || receipt.status !== 'completed'
      || quote.length === 0
      || !receipt.line_hashes?.includes(sha256(quote))
    ) {
      return validationFinding(
        'drawback',
        [drawbackId],
        `Resolve brainstorm drawback: ${drawbackId}.`,
      );
    }
  }
  const recommendation = ledger.recommendation;
  const selected = recommendation?.selected_approach_id;
  if (typeof selected !== 'string' || !approachIds.includes(selected)) {
    return validationFinding(
      'recommendation',
      ['recommendation'],
      'Choose a valid brainstorm approach: recommendation.',
    );
  }
  const expectedRejected = approachIds.filter((id) => id !== selected).sort();
  const recordedRejected = Array.isArray(recommendation.rejected_alternative_ids)
    ? [...new Set(recommendation.rejected_alternative_ids)].sort()
    : [];
  const challenges = Array.isArray(ledger.alternative_challenges)
    ? ledger.alternative_challenges
    : [];
  for (const alternativeId of expectedRejected) {
    if (!recordedRejected.includes(alternativeId)) {
      return validationFinding(
        'rejection',
        [alternativeId],
        `Record the rejected brainstorm alternative: ${alternativeId}.`,
      );
    }
    const matches = challenges.filter((challenge) => challenge?.alternative_id === alternativeId);
    const challenge = matches.length === 1 ? matches[0] : null;
    const receipt = receiptById.get(challenge?.receipt_id);
    if (
      challenge === null
      || challenge.result !== 'completed'
      || receipt?.kind !== 'challenge'
      || receipt.status !== 'completed'
      || receipt.challenge_id !== challenge.id
      || receipt.alternative_id !== alternativeId
      || receipt.method !== challenge.method
      || receipt.claim_sha256 !== sha256(challenge.claim ?? '')
      || receipt.falsifying_outcome_sha256 !== jsonDigest(challenge.falsifying_outcome)
      || !challengeArtifactsUnchanged(root, receipt)
    ) {
      return validationFinding(
        'challenge',
        [alternativeId],
        `Run the predeclared challenge for rejected alternative: ${alternativeId}.`,
      );
    }
    if (receipt.falsified === true) {
      return validationFinding(
        'falsified_rejection',
        [alternativeId],
        `Rejection claim was falsified; choose again: ${alternativeId}.`,
      );
    }
  }
  if (
    recordedRejected.length !== expectedRejected.length
    || recordedRejected.some((id) => !expectedRejected.includes(id))
  ) {
    return validationFinding(
      'rejection',
      ['recommendation'],
      'Rejected brainstorm alternatives do not match: recommendation.',
    );
  }
  if (ledger.design_path !== undefined || ledger.design_sha256 !== undefined) {
    const designPath = expectedDesignPath(ledger.requirements_path);
    if (
      ledger.design_path !== designPath
      || !existsSync(resolve(root, designPath))
      || ledger.design_sha256 !== sha256(readFileSync(resolve(root, designPath)))
    ) {
      return validationFinding(
        'design_closure',
        [designPath],
        `Brainstorm design closure does not match: ${designPath}.`,
      );
    }
  }
  return null;
}

export function hostStopDecision(finding, _host) {
  return finding === null ? null : { decision: 'block', reason: finding.reason };
}

export function recommendationRequested(message) {
  if (typeof message !== 'string') return false;
  return /^#{1,6}\s+Recommendation\b/imu.test(message)
    || /\b(?:I|we) recommend\b/iu.test(message)
    || /\b(?:my|our) recommendation\b/iu.test(message)
    || /\brecommended approach\b/iu.test(message);
}

export function handleStop(input, { host, root }) {
  const paths = brainstormPaths(root);
  const current = readLedger(paths.ledger);
  const marker = matchingPromptMarker(paths, input, host);

  if (input?.stop_hook_active === true) {
    if (marker !== null) unlinkSync(marker.path);
    return null;
  }

  if (current?.state !== 'active') {
    if (marker !== null) unlinkSync(marker.path);
    return null;
  }

  if (marker === null) {
    clearPendingReceipts(paths);
    const suspended = { ...current, state: 'suspended' };
    delete suspended.activation;
    writeJsonAtomic(paths.ledger, suspended);
    return null;
  }

  unlinkSync(marker.path);
  if (!['invoke', 'continue'].includes(marker.value.classification)) return null;
  if (!recommendationRequested(input?.last_assistant_message)) return null;
  const finding = validateRecommendation(
    current,
    completedReceiptRecords(paths),
    { root },
  );
  return hostStopDecision(finding, host);
}

export function handleBrainstormHook(input, options) {
  const eventName = input?.hook_event_name ?? input?.event_name ?? input?.event;
  if (eventName === 'PreToolUse') return handleBeforeTool(input, options);
  if (eventName === 'PostToolUse') {
    const editResult = handlePostEdit(input, options);
    return handleToolResult(input, options) ?? editResult;
  }
  if (eventName === 'PostToolUseFailure') return handleToolResult(input, options);
  if (eventName === 'UserPromptSubmit') return handlePrompt(input, options);
  if (eventName === 'Stop') return handleStop(input, options);
  return null;
}

function optionValue(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1] ?? null;
}

function invokedAsMain() {
  if (!process.argv[1]) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (invokedAsMain()) {
  let input;
  try {
    input = JSON.parse(readFileSync(0, 'utf8'));
  } catch {
    input = {};
  }
  const root = optionValue('--root') ?? process.env.CLAUDE_PROJECT_DIR ?? process.cwd();
  const host = optionValue('--host') ?? 'claude';
  const result = handleBrainstormHook(input, { host, root });
  if (result?.decision === 'block') process.stdout.write(JSON.stringify(result));
}
