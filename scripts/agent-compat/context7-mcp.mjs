#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readProtonField } from './proton-credential.mjs';
import { trustedSearchPath } from './resolve-executable.mjs';

const PASSTHROUGH_ENVIRONMENT = [
  'PATH',
  'HOME',
  'TMPDIR',
  'LANG',
  'LC_ALL',
  'NODE_EXTRA_CA_CERTS',
];

// Reviewed 2026-10-10: 4.1.1, published 2026-09-14, registry integrity
// sha512-fUARTIZGVKzlzQKDhKUg4aIQ3yEU/12STdfxJDDnLMSN7yyHiPvNHI05iFwxkv3vC8G76Wp27MwK6Jucl/C5pA==
// Raising it is a deliberate edit of this line and of config/agent-compat/context7/ together.
// The installer runs `npm ci` against that committed lock, which records an integrity hash for
// this package and for every package it depends on (#995). This launcher never fetches anything:
// it starts the package that `npm ci` verified.
export const CONTEXT7_PACKAGE = '@upstash/context7-mcp@4.1.1';
const PACKAGE_NAME = CONTEXT7_PACKAGE.slice(0, CONTEXT7_PACKAGE.lastIndexOf('@'));
const PACKAGE_VERSION = CONTEXT7_PACKAGE.slice(CONTEXT7_PACKAGE.lastIndexOf('@') + 1);
const REINSTALL = 'run node scripts/agent-compat/codex-bootstrap.mjs to install it';

// Where `npm ci` puts the packages for one lock: beside the releases, keyed by the hash of the
// lock bytes, so an unchanged lock is never installed twice and a changed one never reuses an
// old tree. The installer and this launcher both derive the folder here.
export function context7InstallDirectory(runtimeRoot, lockBytes) {
  const hash = createHash('sha256').update(lockBytes).digest('hex');
  return join(runtimeRoot, 'context7', hash);
}

// The bin file of the installed package, after checking the package is the reviewed version. The
// bin path comes from the installed package.json, and must be a regular file inside the package.
export function context7Entrypoint(installDirectory) {
  const packageDirectory = join(installDirectory, 'node_modules', ...PACKAGE_NAME.split('/'));
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(join(packageDirectory, 'package.json'), 'utf8'));
  } catch {
    throw new Error(`Context7 helper is not installed (${REINSTALL})`);
  }
  if (manifest?.name !== PACKAGE_NAME || manifest.version !== PACKAGE_VERSION) {
    throw new Error(
      `Context7 helper install does not match ${CONTEXT7_PACKAGE} (${REINSTALL})`,
    );
  }
  const bin = typeof manifest.bin === 'string'
    ? manifest.bin
    : manifest.bin?.['context7-mcp'] ?? Object.values(manifest.bin ?? {})[0];
  if (typeof bin !== 'string' || bin === '') {
    throw new Error(`Context7 helper install has no start file (${REINSTALL})`);
  }
  let entry;
  try {
    entry = realpathSync(join(packageDirectory, bin));
    const inside = relative(realpathSync(packageDirectory), entry);
    if (inside === '' || inside === '..' || inside.startsWith(`..${sep}`) || isAbsolute(inside)
        || !statSync(entry).isFile()) {
      throw new Error('outside');
    }
  } catch {
    throw new Error(`Context7 helper install has no start file (${REINSTALL})`);
  }
  return entry;
}

function installedEntrypoint(releaseDirectory) {
  let lockBytes;
  try {
    lockBytes = readFileSync(join(releaseDirectory, 'context7', 'package-lock.json'));
  } catch {
    throw new Error(`Context7 helper is not installed (${REINSTALL})`);
  }
  // releaseDirectory is <runtime root>/releases/<release>.
  return context7Entrypoint(
    context7InstallDirectory(dirname(dirname(releaseDirectory)), lockBytes),
  );
}

export function context7Environment(source, key) {
  const environment = {};
  for (const name of PASSTHROUGH_ENVIRONMENT) {
    if (typeof source[name] === 'string') environment[name] = source[name];
  }
  if (typeof environment.PATH === 'string') environment.PATH = trustedSearchPath(environment.PATH);
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
  releaseDirectory = dirname(fileURLToPath(import.meta.url)),
  environment = process.env,
  parent = process,
}) {
  // Find the install before the key is fetched, so a missing or mismatched install is refused
  // while no credential is in play. The key reaches the child through its environment.
  const entry = installedEntrypoint(releaseDirectory);
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
    // The server runs under the node that started this launcher, from the verified install,
    // so no program is looked up on PATH while the key is in play. It starts from home and not
    // from the checkout, so nothing in a checkout's folder tree is in its way.
    child = spawnChild(process.execPath, [entry], {
      cwd: home,
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
