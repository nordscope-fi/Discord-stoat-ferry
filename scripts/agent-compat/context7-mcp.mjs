#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { readProtonField } from './proton-credential.mjs';

const PASSTHROUGH_ENVIRONMENT = [
  'PATH',
  'HOME',
  'TMPDIR',
  'LANG',
  'LC_ALL',
  'NODE_EXTRA_CA_CERTS',
];

export function context7Environment(source, key) {
  const environment = {};
  for (const name of PASSTHROUGH_ENVIRONMENT) {
    if (typeof source[name] === 'string') environment[name] = source[name];
  }
  environment.CONTEXT7_API_KEY = key;
  return environment;
}

export function readContext7Access(home) {
  const path = join(home, '.config', 'discord-ferry', 'context7-agent.json');
  const info = lstatSync(path);
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o777) !== 0o600) {
    throw new Error('Context7 ownership record is unsafe');
  }
  const record = JSON.parse(readFileSync(path, 'utf8'));
  if (record?.version !== 2
      || record.state !== 'ready'
      || typeof record.share_id !== 'string'
      || record.share_id.length === 0
      || typeof record.item_id !== 'string'
      || record.item_id.length === 0) {
    throw new Error('Context7 ownership record is invalid');
  }
  return { shareId: record.share_id, itemId: record.item_id };
}

export async function runContext7({
  home,
  check = false,
  fieldReader = readProtonField,
  accessReader = readContext7Access,
  spawnChild = spawn,
  environment = process.env,
  parent = process,
}) {
  let key;
  try {
    const access = accessReader(home);
    key = await fieldReader({
      tokenFile: 'context7-agent.pat',
      shareId: access.shareId,
      itemId: access.itemId,
      field: 'API Key',
      reason: 'Start Discord Ferry Context7',
      home,
    });
  } catch {
    throw new Error('Context7 credential unavailable');
  }
  if (check) return { status: 0, signal: null, ready: true };

  let child;
  try {
    child = spawnChild('npx', ['-y', '@upstash/context7-mcp'], {
      env: context7Environment(environment, key),
      stdio: 'inherit',
    });
  } catch {
    throw new Error('Context7 process could not start');
  }

  const forward = (signal) => {
    try {
      child.kill(signal);
    } catch {
      // The child may already have exited.
    }
  };
  const interrupt = () => forward('SIGINT');
  const terminate = () => forward('SIGTERM');
  parent.on('SIGINT', interrupt);
  parent.on('SIGTERM', terminate);
  const cleanup = () => {
    parent.removeListener('SIGINT', interrupt);
    parent.removeListener('SIGTERM', terminate);
  };

  return new Promise((resolve, reject) => {
    child.once('error', () => {
      cleanup();
      reject(new Error('Context7 process could not start'));
    });
    child.once('close', (status, signal) => {
      cleanup();
      resolve({ status, signal, ready: false });
    });
  });
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length > 1 || (args.length === 1 && args[0] !== '--check')) {
    process.stderr.write('context7-mcp: expected no arguments or --check\n');
    process.exitCode = 2;
    return;
  }
  try {
    const result = await runContext7({
      home: process.env.HOME,
      check: args[0] === '--check',
    });
    if (result.ready) {
      process.stdout.write('context7 credential ready\n');
    } else if (result.signal) {
      process.kill(process.pid, result.signal);
    } else {
      process.exitCode = result.status ?? 1;
    }
  } catch (error) {
    process.stderr.write(`context7-mcp: ${error.message}\n`);
    process.exitCode = 1;
  }
}

let invokedAsMain = false;
if (process.argv[1]) {
  try {
    invokedAsMain = import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    // An invalid entrypoint cannot be the current module.
  }
}

if (invokedAsMain) await main();
