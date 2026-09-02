#!/usr/bin/env node

import { spawn } from 'node:child_process';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  lstatSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { classifyReviewFailure, safeChildFailure } from './review-contract.mjs';

const MAX_STDOUT_BYTES = 2 * 1024 * 1024;

function stopProcessGroup(child, signal) {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch {
    // The child may already have exited.
  }
}

export function runBoundedChild(command, args, { env, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      detached: true,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let timedOut = false;
    let overflowed = false;
    let spawnError = null;
    let killTimer = null;

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (overflowed) return;
      stdout += chunk;
      if (Buffer.byteLength(stdout) > MAX_STDOUT_BYTES) {
        overflowed = true;
        stdout = '';
        stopProcessGroup(child, 'SIGTERM');
      }
    });
    child.stderr.resume();
    child.once('error', (error) => {
      spawnError = error;
    });

    const timeout = setTimeout(() => {
      timedOut = true;
      stopProcessGroup(child, 'SIGTERM');
      killTimer = setTimeout(() => stopProcessGroup(child, 'SIGKILL'), 2000);
      killTimer.unref();
    }, timeoutMs);

    child.once('close', (status, signal) => {
      clearTimeout(timeout);
      if (killTimer) clearTimeout(killTimer);
      if (spawnError) {
        reject(spawnError);
        return;
      }
      if (timedOut) {
        const error = new Error('child timed out');
        error.name = 'TimeoutError';
        reject(error);
        return;
      }
      if (overflowed) {
        const error = new Error('child output exceeded limit');
        error.status = status ?? 1;
        reject(error);
        return;
      }
      if (status !== 0) {
        const error = new Error('child failed');
        error.status = status;
        error.signal = signal;
        reject(error);
        return;
      }
      resolve({ stdout });
    });
  });
}

function stageFailure(stage, error) {
  const wrapped = new Error(safeChildFailure(`pass-cli ${stage}`, error));
  wrapped.stage = stage;
  wrapped.classification = classifyReviewFailure(error);
  return wrapped;
}

export async function readProtonField({
  tokenFile,
  vaultName,
  itemTitle,
  field,
  reason,
  home,
  run = runBoundedChild,
}) {
  if (![tokenFile, vaultName, itemTitle, field, reason, home].every(Boolean)) {
    throw new Error('Proton field access requires token, vault, item, field, reason, and home');
  }
  const tokenPath = join(home, '.config', 'discord-ferry', tokenFile);
  const tokenStat = lstatSync(tokenPath);
  if (!tokenStat.isFile() || tokenStat.isSymbolicLink()) {
    throw new Error(`${tokenFile} must be a regular file`);
  }
  const tokenMode = tokenStat.mode & 0o777;
  if (tokenMode !== 0o600) throw new Error(`${tokenFile} must have mode 0600`);
  const token = readFileSync(tokenPath, 'utf8').trim();
  if (!/^pst_[^\s]{32,}$/u.test(token)) throw new Error(`${tokenFile} is invalid`);
  const sessionDirectory = mkdtempSync(join(tmpdir(), 'ferry-pass-session-'));
  const environment = {
    PATH: process.env.PATH ?? '',
    PROTON_PASS_SESSION_DIR: sessionDirectory,
    PROTON_PASS_PERSONAL_ACCESS_TOKEN: token,
  };
  try {
    try {
      await run('pass-cli', ['login'], {
        env: environment,
        timeoutMs: 30000,
      });
    } catch (error) {
      throw stageFailure('login', error);
    }
    let value;
    try {
      value = await run('pass-cli', [
        'item',
        'view',
        '--vault-name',
        vaultName,
        '--item-title',
        itemTitle,
        '--field',
        field,
      ], {
        env: { ...environment, PROTON_PASS_AGENT_REASON: reason },
        timeoutMs: 30000,
      });
    } catch (error) {
      throw stageFailure('field-read', error);
    }
    if (!value.stdout.trim()) {
      const error = new Error('Proton returned an empty field');
      error.stage = 'field-read';
      throw error;
    }
    return value.stdout.trim();
  } finally {
    rmSync(sessionDirectory, { recursive: true, force: true });
  }
}

export function readReviewerField(options) {
  return readProtonField({
    ...options,
    tokenFile: 'reviewer-agent.pat',
    vaultName: 'PortalPilot',
    field: options.field ?? 'API Key',
  });
}

async function selfTest(basePath) {
  const parent = basePath ?? tmpdir();
  mkdirSync(parent, { recursive: true });
  const root = mkdtempSync(join(parent, 'proton-credential-self-test-'));
  const home = join(root, 'home');
  const tokenDirectory = join(home, '.config', 'discord-ferry');
  const fakePassCli = join(root, 'pass-cli');
  const tokenPath = join(tokenDirectory, 'reviewer-agent.pat');
  const observedSessions = [];
  const observedEnvironments = [];
  try {
    mkdirSync(tokenDirectory, { recursive: true });
    writeFileSync(tokenPath, `pst_${'t'.repeat(40)}\n`, { mode: 0o600 });
    writeFileSync(fakePassCli, `#!/bin/sh
if [ -z "$PROTON_PASS_SESSION_DIR" ] || [ ! -d "$PROTON_PASS_SESSION_DIR" ]; then exit 71; fi
if [ -z "$PROTON_PASS_PERSONAL_ACCESS_TOKEN" ]; then exit 72; fi
if [ "$1" = "login" ]; then exit 0; fi
if [ "$1 $2" = "item view" ] && [ "$PROTON_PASS_AGENT_REASON" = "Review Ferry code" ]; then
  printf 'FIXTURE_FIELD_VALUE\\n'
  exit 0
fi
exit 73
`, { mode: 0o700 });

    const run = (command, args, options) => {
      if (command !== 'pass-cli') throw new Error('unexpected command');
      observedSessions.push(options.env.PROTON_PASS_SESSION_DIR);
      observedEnvironments.push(Object.keys(options.env).sort());
      return runBoundedChild(fakePassCli, args, options);
    };
    const value = await readReviewerField({
      itemTitle: 'Mistral Vibe API Key',
      reason: 'Review Ferry code',
      home,
      run,
    });
    if (value !== 'FIXTURE_FIELD_VALUE') throw new Error('field value changed in transit');
    if (observedSessions.length !== 2 || observedSessions[0] !== observedSessions[1]) {
      throw new Error('commands did not share one isolated session');
    }
    if (existsSync(observedSessions[0])) throw new Error('isolated session was not removed');
    const loginKeys = ['PATH', 'PROTON_PASS_PERSONAL_ACCESS_TOKEN', 'PROTON_PASS_SESSION_DIR'];
    const fieldKeys = [...loginKeys, 'PROTON_PASS_AGENT_REASON'].sort();
    if (JSON.stringify(observedEnvironments[0]) !== JSON.stringify(loginKeys.sort())) {
      throw new Error('login environment was not bounded');
    }
    if (JSON.stringify(observedEnvironments[1]) !== JSON.stringify(fieldKeys)) {
      throw new Error('field environment was not bounded');
    }

    chmodSync(tokenPath, 0o644);
    let rejectedMode = false;
    try {
      await readReviewerField({
        itemTitle: 'Mistral Vibe API Key',
        reason: 'Review Ferry code',
        home,
        run,
      });
    } catch (error) {
      rejectedMode = error.message === 'reviewer-agent.pat must have mode 0600';
    }
    if (!rejectedMode) throw new Error('mode-0644 token fixture was accepted');
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
  process.stderr.write('proton-credential self-test: all checks passed\n');
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
  const args = process.argv.slice(2);
  if (args[0] !== '--self-test' || args.length > 2) {
    process.stderr.write('proton-credential: expected --self-test [temporary-parent]\n');
    process.exitCode = 2;
  } else {
    selfTest(args[1]).catch((error) => {
      process.stderr.write(`proton-credential self-test failed: ${error.message}\n`);
      process.exitCode = 1;
    });
  }
}
