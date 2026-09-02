#!/usr/bin/env node

import { createHash, randomUUID } from 'node:crypto';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

export const CRITIQUE_BUDGET = 3;
export const CRITIQUE_POLICY = 'ferry-critique-budget-v1';
export const STATE_VERSION = 1;

const DESIGN_PATTERN = /^docs\/plans\/designs\/[^/]+\.md$/u;
const STATE_DIRECTORY = 'docs/plans/.review/critique-budget';
const ATTEMPT_STATUSES = new Set(['started', 'completed', 'failed', 'timed_out']);
const CYCLE_STATES = new Set(['active', 'owner_review', 'closed']);
const OPENED_BY_VALUES = new Set(['initial', 'owner_restart']);
const REVIEW_OUTCOMES = new Set(['pass', 'iterate', 'rethink', 'failed', 'timed_out']);
const OWNER_DECISIONS = Object.freeze(['accept', 'return-to-design', 'restart']);
const EVIDENCE_SOURCE_KINDS = new Set(['repo', 'official-docs', 'immutable']);
const EVIDENCE_RESULTS = new Set(['confirmed', 'contradicted', 'not_found']);
const MAX_EVIDENCE_BYTES = 2_097_152;
const MAX_EVIDENCE_ENTRIES = 256;
const MAX_IDENTIFIER_LENGTH = 128;

class BudgetError extends Error {
  constructor(action, message, details = {}) {
    super(message);
    this.action = action;
    this.details = details;
  }
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isSha256(value) {
  return typeof value === 'string' && /^[a-f0-9]{64}$/u.test(value);
}

function isTimestamp(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

function inside(root, candidate) {
  const offset = relative(root, candidate);
  return offset !== '' && offset !== '..' && !offset.startsWith(`..${sep}`) && !isAbsolute(offset);
}

function resolveDesign(root, designPath) {
  if (typeof designPath !== 'string' || designPath.length === 0 || isAbsolute(designPath)) {
    throw new BudgetError('invalid-design', 'design path must be relative to the checkout');
  }
  const canonicalRoot = realpathSync(root);
  const unresolved = resolve(canonicalRoot, designPath);
  if (!existsSync(unresolved)) {
    throw new BudgetError('invalid-design', 'design file does not exist');
  }
  const canonicalDesign = realpathSync(unresolved);
  if (!inside(canonicalRoot, canonicalDesign)) {
    throw new BudgetError('invalid-design', 'design path leaves the checkout');
  }
  const local = relative(canonicalRoot, canonicalDesign).split(sep).join('/');
  if (local !== designPath.split(sep).join('/') || !DESIGN_PATTERN.test(local)) {
    throw new BudgetError('invalid-design', 'design must be a direct child of docs/plans/designs');
  }
  if (!statSync(canonicalDesign).isFile()) {
    throw new BudgetError('invalid-design', 'design path must name a regular file');
  }
  return {
    root: canonicalRoot,
    path: canonicalDesign,
    local,
    sha256: sha256(readFileSync(canonicalDesign)),
  };
}

export function statePathFor(root, designPath) {
  return resolve(root, STATE_DIRECTORY, `${sha256(designPath)}.json`);
}

function ensureStateDirectory(root, recordPath) {
  const canonicalRoot = realpathSync(root);
  const stateDirectory = resolve(canonicalRoot, STATE_DIRECTORY);
  if (dirname(recordPath) !== stateDirectory) {
    throw new BudgetError('repair-state', 'state path does not match the checkout');
  }
  let current = canonicalRoot;
  for (const segment of STATE_DIRECTORY.split('/')) {
    current = resolve(current, segment);
    if (existsSync(current)) {
      const metadata = lstatSync(current);
      if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
        throw new BudgetError('repair-state', 'state directory must not traverse a link');
      }
    } else {
      mkdirSync(current, { mode: 0o700 });
    }
  }
  if (realpathSync(stateDirectory) !== stateDirectory) {
    throw new BudgetError('repair-state', 'state directory leaves the checkout');
  }
  chmodSync(stateDirectory, 0o700);
}

function writeJsonAtomic(root, path, value) {
  ensureStateDirectory(root, path);
  const content = `${JSON.stringify(value, null, 2)}\n`;
  const temporary = resolve(dirname(path), `.state.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    chmodSync(temporary, 0o600);
    if (readFileSync(temporary, 'utf8') !== content) {
      throw new BudgetError('repair-state', 'state staging verification failed');
    }
    renameSync(temporary, path);
    chmodSync(path, 0o600);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    return true;
  }
}

function readLock(lockPath) {
  try {
    const value = JSON.parse(readFileSync(lockPath, 'utf8'));
    if (!isObject(value) || !Number.isSafeInteger(value.pid) || value.pid <= 0 ||
        typeof value.token !== 'string' || value.token.length === 0) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

function acquireLock(root, lockPath) {
  ensureStateDirectory(root, lockPath);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const token = randomUUID();
    try {
      writeFileSync(lockPath, `${JSON.stringify({ pid: process.pid, token })}\n`, {
        encoding: 'utf8',
        flag: 'wx',
        mode: 0o600,
      });
      chmodSync(lockPath, 0o600);
      return token;
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      const owner = readLock(lockPath);
      if (attempt === 0 && owner && !processIsAlive(owner.pid)) {
        try {
          unlinkSync(lockPath);
        } catch (unlinkError) {
          if (unlinkError?.code !== 'ENOENT') throw unlinkError;
        }
        continue;
      }
      throw new BudgetError('busy', 'another process owns the critique record lock');
    }
  }
  throw new BudgetError('busy', 'critique record lock could not be acquired');
}

function releaseLock(lockPath, token) {
  const owner = readLock(lockPath);
  if (owner?.token !== token || owner.pid !== process.pid) return;
  try {
    unlinkSync(lockPath);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
}

function validAttempt(value, expectedRound) {
  return isObject(value)
    && value.round === expectedRound
    && ['critique', 'evidence-investigation'].includes(value.mode)
    && ATTEMPT_STATUSES.has(value.status)
    && isSha256(value.design_sha256)
    && isTimestamp(value.started_at)
    && Array.isArray(value.unresolved_ids)
    && value.unresolved_ids.every(validIdentifier)
    && (!Object.hasOwn(value, 'settled_ids') || (
      Array.isArray(value.settled_ids) && value.settled_ids.every(validIdentifier)
    ))
    && (!Object.hasOwn(value, 'evidence_sha256') || isSha256(value.evidence_sha256))
    && (!Object.hasOwn(value, 'outcome') || (
      REVIEW_OUTCOMES.has(value.outcome) || value.outcome === 'evidence'
    ));
}

function validCycle(value) {
  return isObject(value)
    && typeof value.cycle_id === 'string'
    && value.cycle_id.length > 0
    && CYCLE_STATES.has(value.state)
    && OPENED_BY_VALUES.has(value.opened_by)
    && (value.owner_decision === null || isObject(value.owner_decision))
    && Array.isArray(value.attempts)
    && value.attempts.length <= CRITIQUE_BUDGET
    && value.attempts.every((attempt, index) => validAttempt(attempt, index + 1));
}

function readState(path, designPath) {
  if (!existsSync(path)) return null;
  let state;
  try {
    state = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    throw new BudgetError('repair-state', 'critique record is not valid JSON');
  }
  const valid = isObject(state)
    && state.schema_version === STATE_VERSION
    && state.policy === CRITIQUE_POLICY
    && state.design_path === designPath
    && Array.isArray(state.cycles)
    && state.cycles.length > 0
    && state.cycles.every(validCycle);
  if (!valid) {
    throw new BudgetError('repair-state', 'critique record has an unsupported or damaged shape');
  }
  return state;
}

function newCycle(openedBy = 'initial') {
  return {
    cycle_id: randomUUID(),
    state: 'active',
    opened_by: openedBy,
    owner_decision: null,
    attempts: [],
  };
}

function ownerDecisionRequired(cycle, decisions = OWNER_DECISIONS) {
  return {
    action: 'owner-decision-required',
    cycle_id: cycle.cycle_id,
    attempts: cycle.attempts.length,
    decisions,
  };
}

function validIdentifier(value) {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= MAX_IDENTIFIER_LENGTH
    && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/u.test(value);
}

function uniqueIdentifiers(values) {
  if (!Array.isArray(values) || !values.every(validIdentifier)) {
    throw new BudgetError('invalid-completion', 'unresolved identifiers are invalid');
  }
  const unique = [...new Set(values)];
  if (unique.length !== values.length || unique.length > MAX_EVIDENCE_ENTRIES) {
    throw new BudgetError('invalid-completion', 'unresolved identifiers must be unique and bounded');
  }
  return unique;
}

function activeAttempt(state, cycleId, round, designSha256) {
  const cycle = state.cycles.at(-1);
  const attempt = cycle?.attempts.at(-1);
  if (
    cycle?.cycle_id !== cycleId
    || attempt?.round !== round
    || attempt.status !== 'started'
    || attempt.design_sha256 !== designSha256
  ) {
    throw new BudgetError('stale-completion', 'completion does not match the active attempt');
  }
  return { cycle, attempt };
}

function resolveEvidenceFile(root, evidencePath) {
  if (typeof evidencePath !== 'string' || evidencePath.length === 0 || isAbsolute(evidencePath)) {
    throw new BudgetError('invalid-evidence', 'evidence path must be relative to the checkout');
  }
  const unresolved = resolve(root, evidencePath);
  if (!existsSync(unresolved)) {
    throw new BudgetError('invalid-evidence', 'evidence file does not exist');
  }
  const canonical = realpathSync(unresolved);
  if (!inside(root, canonical) || !statSync(canonical).isFile()) {
    throw new BudgetError('invalid-evidence', 'evidence path must name a local regular file');
  }
  const bytes = readFileSync(canonical);
  if (bytes.length > MAX_EVIDENCE_BYTES) {
    throw new BudgetError('invalid-evidence', 'evidence file is too large');
  }
  let value;
  try {
    value = JSON.parse(bytes);
  } catch {
    throw new BudgetError('invalid-evidence', 'evidence file is not valid JSON');
  }
  return { value, sha256: sha256(bytes) };
}

function resolveRepositoryEvidence(root, locator) {
  if (typeof locator !== 'string' || locator.length === 0 || isAbsolute(locator)) {
    throw new BudgetError('invalid-evidence', 'repository evidence locator must be relative');
  }
  const unresolved = resolve(root, locator);
  if (!existsSync(unresolved)) {
    throw new BudgetError('invalid-evidence', 'repository evidence source does not exist');
  }
  const canonical = realpathSync(unresolved);
  if (!inside(root, canonical) || !statSync(canonical).isFile()) {
    throw new BudgetError('invalid-evidence', 'repository evidence leaves the checkout');
  }
  return sha256(readFileSync(canonical));
}

function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().startsWith(value);
}

function validateEvidenceEntry(root, entry, attempt) {
  if (!isObject(entry) || !validIdentifier(entry.finding_id)) {
    throw new BudgetError('invalid-evidence', 'evidence finding identifier is invalid');
  }
  if (!EVIDENCE_SOURCE_KINDS.has(entry.source_kind)) {
    throw new BudgetError('invalid-evidence', 'training memory is not an accepted evidence source');
  }
  if (typeof entry.locator !== 'string' || entry.locator.length === 0 ||
      entry.locator.length > 2_048 || !EVIDENCE_RESULTS.has(entry.result)) {
    throw new BudgetError('invalid-evidence', 'evidence source or result is invalid');
  }
  if (entry.source_kind === 'repo') {
    if (!isSha256(entry.source_sha256) ||
        resolveRepositoryEvidence(root, entry.locator) !== entry.source_sha256) {
      throw new BudgetError('invalid-evidence', 'repository evidence fingerprint is stale');
    }
  } else if (entry.source_kind === 'official-docs') {
    const today = new Date().toISOString().slice(0, 10);
    const attemptDate = attempt.started_at.slice(0, 10);
    if (!validDate(entry.retrieved_on) ||
        entry.retrieved_on < attemptDate || entry.retrieved_on > today) {
      throw new BudgetError(
        'invalid-evidence',
        'official documentation needs a retrieval date from the final attempt',
      );
    }
  } else if (typeof entry.version !== 'string' || entry.version.length === 0 ||
             entry.version.length > 256) {
    throw new BudgetError('invalid-evidence', 'immutable evidence needs a version');
  }
  return entry;
}

function validateRoundThreeEvidence(root, evidencePath, cycle, attempt) {
  const evidence = resolveEvidenceFile(root, evidencePath);
  const value = evidence.value;
  if (
    !isObject(value)
    || value.cycle_id !== cycle.cycle_id
    || value.round !== CRITIQUE_BUDGET
    || value.design_sha256 !== attempt.design_sha256
    || !Array.isArray(value.entries)
    || value.entries.length === 0
    || value.entries.length > MAX_EVIDENCE_ENTRIES
  ) {
    throw new BudgetError('invalid-evidence', 'evidence does not match the final attempt');
  }
  const entries = value.entries.map(entry => validateEvidenceEntry(root, entry, attempt));
  const identifiers = entries.map(entry => entry.finding_id);
  if (new Set(identifiers).size !== identifiers.length) {
    throw new BudgetError('invalid-evidence', 'evidence identifiers must be unique');
  }
  const expected = [...attempt.unresolved_ids].sort();
  if (expected.length > 0 && JSON.stringify([...identifiers].sort()) !== JSON.stringify(expected)) {
    throw new BudgetError('invalid-evidence', 'evidence must cover every unresolved identifier');
  }
  const unresolvedIds = entries
    .filter(entry => entry.result === 'not_found')
    .map(entry => entry.finding_id)
    .sort();
  const settledIds = entries
    .filter(entry => entry.result !== 'not_found')
    .map(entry => entry.finding_id)
    .sort();
  return { evidenceSha256: evidence.sha256, unresolvedIds, settledIds };
}

export function claimAttempt({ root, designPath }) {
  const design = resolveDesign(root, designPath);
  const recordPath = statePathFor(design.root, design.local);
  const lockPath = recordPath.replace(/\.json$/u, '.lock');
  const token = acquireLock(design.root, lockPath);
  try {
    const state = readState(recordPath, design.local) ?? {
      schema_version: STATE_VERSION,
      policy: CRITIQUE_POLICY,
      design_path: design.local,
      cycles: [newCycle()],
    };
    const cycle = state.cycles.at(-1);
    if (cycle.state !== 'active' || cycle.attempts.length >= CRITIQUE_BUDGET) {
      const lastAttempt = cycle.attempts.at(-1);
      if (cycle.state === 'closed' && lastAttempt?.outcome === 'pass') {
        throw new BudgetError('cycle-closed', 'critique passed and the cycle is closed', {
          cycle_id: cycle.cycle_id,
          next: 'test-scenarios',
        });
      }
      const decisions = cycle.state === 'closed' && lastAttempt?.outcome === 'rethink'
        ? ['restart']
        : OWNER_DECISIONS;
      throw new BudgetError(
        'owner-decision-required',
        'the critique cycle has no remaining attempts',
        {
          ...ownerDecisionRequired(cycle, decisions),
          design_sha256: design.sha256,
        },
      );
    }
    const round = cycle.attempts.length + 1;
    const mode = round === CRITIQUE_BUDGET ? 'evidence-investigation' : 'critique';
    const prior = cycle.attempts.at(-1);
    const unresolvedIds = prior?.unresolved_ids ?? [];
    cycle.attempts.push({
      round,
      mode,
      status: 'started',
      design_sha256: design.sha256,
      started_at: new Date().toISOString(),
      unresolved_ids: unresolvedIds,
    });
    writeJsonAtomic(design.root, recordPath, state);
    return {
      action: 'review',
      cycle_id: cycle.cycle_id,
      round,
      mode,
      design_sha256: design.sha256,
      unresolved_ids: unresolvedIds,
    };
  } finally {
    releaseLock(lockPath, token);
  }
}

export function completeAttempt({
  root,
  designPath,
  cycleId,
  round,
  outcome,
  unresolvedIds = [],
  evidencePath = null,
}) {
  const design = resolveDesign(root, designPath);
  const recordPath = statePathFor(design.root, design.local);
  const lockPath = recordPath.replace(/\.json$/u, '.lock');
  const token = acquireLock(design.root, lockPath);
  try {
    const state = readState(recordPath, design.local);
    if (!state) throw new BudgetError('stale-completion', 'critique record does not exist');
    const { cycle, attempt } = activeAttempt(state, cycleId, round, design.sha256);
    if (round < CRITIQUE_BUDGET) {
      if (!REVIEW_OUTCOMES.has(outcome)) {
        throw new BudgetError('invalid-completion', 'review outcome is invalid');
      }
      const carried = outcome === 'iterate' ? uniqueIdentifiers(unresolvedIds) : [];
      if (outcome !== 'iterate' && unresolvedIds.length > 0) {
        throw new BudgetError('invalid-completion', 'only iterate may carry unresolved identifiers');
      }
      attempt.status = ['failed', 'timed_out'].includes(outcome) ? outcome : 'completed';
      attempt.outcome = outcome;
      attempt.unresolved_ids = carried;
      if (['pass', 'rethink'].includes(outcome)) cycle.state = 'closed';
      writeJsonAtomic(design.root, recordPath, state);
      const next = outcome === 'pass'
        ? 'test-scenarios'
        : outcome === 'rethink' ? 'brainstorm' : 'critique';
      return { action: 'completed', cycle_id: cycle.cycle_id, round, outcome, next };
    }

    if (['failed', 'timed_out'].includes(outcome)) {
      attempt.status = outcome;
      attempt.outcome = outcome;
      cycle.state = 'owner_review';
      writeJsonAtomic(design.root, recordPath, state);
      return { ...ownerDecisionRequired(cycle), design_sha256: attempt.design_sha256 };
    }

    try {
      if (outcome !== 'evidence') {
        throw new BudgetError('invalid-evidence', 'the final attempt requires an evidence file');
      }
      const result = validateRoundThreeEvidence(design.root, evidencePath, cycle, attempt);
      attempt.status = 'completed';
      attempt.outcome = outcome;
      attempt.evidence_sha256 = result.evidenceSha256;
      attempt.unresolved_ids = result.unresolvedIds;
      attempt.settled_ids = result.settledIds;
      cycle.state = 'owner_review';
      writeJsonAtomic(design.root, recordPath, state);
      return {
        ...ownerDecisionRequired(cycle),
        design_sha256: attempt.design_sha256,
        unresolved_ids: result.unresolvedIds,
        settled_ids: result.settledIds,
      };
    } catch (error) {
      attempt.status = 'failed';
      attempt.outcome = outcome;
      cycle.state = 'owner_review';
      writeJsonAtomic(design.root, recordPath, state);
      if (error instanceof BudgetError) {
        throw new BudgetError('owner-decision-required', error.message, {
          ...ownerDecisionRequired(cycle),
          design_sha256: attempt.design_sha256,
        });
      }
      throw error;
    }
  } finally {
    releaseLock(lockPath, token);
  }
}

export function decideCycle({ root, designPath, cycleId, designSha256, decision }) {
  const design = resolveDesign(root, designPath);
  const recordPath = statePathFor(design.root, design.local);
  const lockPath = recordPath.replace(/\.json$/u, '.lock');
  const token = acquireLock(design.root, lockPath);
  try {
    const state = readState(recordPath, design.local);
    const cycle = state?.cycles.at(-1);
    const finalAttempt = cycle?.attempts.at(-1);
    const exhausted = cycle?.attempts.length === CRITIQUE_BUDGET
      && finalAttempt?.round === CRITIQUE_BUDGET
      && ['active', 'owner_review'].includes(cycle.state);
    const rethought = cycle?.state === 'closed' && finalAttempt?.outcome === 'rethink';
    if (!cycle || cycle.cycle_id !== cycleId || (!exhausted && !rethought)) {
      throw new BudgetError('stale-owner-decision', 'owner decision does not match an exhausted cycle');
    }
    if (!isSha256(designSha256) || design.sha256 !== designSha256 ||
        (exhausted && finalAttempt.design_sha256 !== designSha256)) {
      throw new BudgetError('stale-owner-decision', 'design changed after the final attempt');
    }
    if (!OWNER_DECISIONS.includes(decision)) {
      throw new BudgetError('invalid-owner-decision', 'owner decision is invalid');
    }
    if (rethought && decision !== 'restart') {
      throw new BudgetError('invalid-owner-decision', 'a rethought design requires owner restart');
    }
    cycle.state = 'closed';
    cycle.owner_decision = { decision, design_sha256: designSha256 };
    let next;
    let newCycleId = null;
    if (decision === 'restart') {
      const fresh = newCycle('owner_restart');
      state.cycles.push(fresh);
      newCycleId = fresh.cycle_id;
      next = 'critique';
    } else {
      next = decision === 'accept' ? 'test-scenarios' : 'brainstorm';
    }
    writeJsonAtomic(design.root, recordPath, state);
    return {
      action: 'owner-decision-recorded',
      cycle_id: cycle.cycle_id,
      decision,
      next,
      ...(newCycleId ? { new_cycle_id: newCycleId } : {}),
    };
  } finally {
    releaseLock(lockPath, token);
  }
}

function parseFlags(argv) {
  const command = argv[0];
  const values = {};
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith('--') || value === undefined || value.startsWith('--')) {
      throw new BudgetError('usage', `invalid argument near ${flag ?? '<end>'}`);
    }
    if (Object.hasOwn(values, flag)) {
      throw new BudgetError('usage', `duplicate argument ${flag}`);
    }
    values[flag] = value;
  }
  return { command, values };
}

function printResult(result, failed = false) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (failed) process.exitCode = 1;
}

export function main(argv = process.argv.slice(2)) {
  try {
    const { command, values } = parseFlags(argv);
    if (!['claim', 'complete', 'decide'].includes(command) || !values['--design']) {
      throw new BudgetError('usage', 'expected claim, complete, or decide with --design <path>');
    }
    const allowedByCommand = {
      claim: new Set(['--design', '--root']),
      complete: new Set([
        '--design', '--root', '--cycle', '--round', '--outcome', '--unresolved', '--evidence',
      ]),
      decide: new Set(['--design', '--root', '--cycle', '--design-sha', '--decision']),
    };
    const allowed = allowedByCommand[command];
    const unknown = Object.keys(values).find(flag => !allowed.has(flag));
    if (unknown) throw new BudgetError('usage', `unknown argument ${unknown}`);
    const root = values['--root'] ?? process.cwd();
    const designPath = values['--design'];
    if (command === 'claim') {
      printResult(claimAttempt({ root, designPath }));
      return;
    }
    if (command === 'complete') {
      if (!values['--cycle'] || !values['--round'] || !values['--outcome']) {
        throw new BudgetError('usage', 'complete requires cycle, round, and outcome');
      }
      const round = Number(values['--round']);
      if (!Number.isSafeInteger(round) || round < 1 || round > CRITIQUE_BUDGET) {
        throw new BudgetError('usage', 'round must be an integer from 1 through 3');
      }
      const unresolvedIds = values['--unresolved']
        ? values['--unresolved'].split(',').filter(value => value.length > 0)
        : [];
      printResult(completeAttempt({
        root,
        designPath,
        cycleId: values['--cycle'],
        round,
        outcome: values['--outcome'],
        unresolvedIds,
        evidencePath: values['--evidence'] ?? null,
      }));
      return;
    }
    if (!values['--cycle'] || !values['--design-sha'] || !values['--decision']) {
      throw new BudgetError('usage', 'decide requires cycle, design SHA, and decision');
    }
    printResult(decideCycle({
      root,
      designPath,
      cycleId: values['--cycle'],
      designSha256: values['--design-sha'],
      decision: values['--decision'],
    }));
  } catch (error) {
    if (error instanceof BudgetError) {
      printResult({ action: error.action, message: error.message, ...error.details }, true);
      return;
    }
    printResult({ action: 'internal-error', message: error?.message ?? 'unknown error' }, true);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
