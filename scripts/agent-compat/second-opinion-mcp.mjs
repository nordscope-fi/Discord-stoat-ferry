#!/usr/bin/env node
// Discord Ferry — protected launcher for the second-opinion MCP server.
// The server registers its providers from its own environment. On the Qwen
// host the Mistral key does not sit in any config file, so this launcher
// reads it from the item-limited Proton grant (the same "Mistral Vibe API
// Key" item the reviewer runtime uses) and injects it at server startup.
// Mirrors context7-mcp.mjs. Fails loudly when the server or the credential
// is unavailable; never prints the key. See ADR-035.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { realpathSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { readReviewerField } from './proton-credential.mjs';

const PASSTHROUGH_ENVIRONMENT = [
  'PATH',
  'HOME',
  'USER',
  'LOGNAME',
  'SHELL',
  'TMPDIR',
  'LANG',
  'LC_ALL',
  'NODE_EXTRA_CA_CERTS',
  'SSL_CERT_FILE',
  'SSL_CERT_DIR',
];

// Provider keys the host environment may already carry. These pass through
// when present; they are never fetched by this launcher.
const PROVIDER_PASSTHROUGH = ['GEMINI_API_KEY', 'OPENAI_API_KEY'];

export function secondOpinionPaths(home) {
  const base = join(home, 'Documents', 'GitHub', 'portalpilot', 'second-opinion');
  return {
    python: join(base, '.venv', 'bin', 'python3'),
    script: join(base, 'main.py'),
  };
}

export function secondOpinionEnvironment(source, mistralKey) {
  const environment = {};
  for (const name of [...PASSTHROUGH_ENVIRONMENT, ...PROVIDER_PASSTHROUGH]) {
    if (typeof source[name] === 'string') environment[name] = source[name];
  }
  environment.MISTRAL_API_KEY = mistralKey;
  return environment;
}

export async function runSecondOpinion({
  home,
  check = false,
  fieldReader = readReviewerField,
  environment = process.env,
  spawnChild = spawn,
  parent = process,
} = {}) {
  const { python, script } = secondOpinionPaths(home);
  if (!existsSync(python) || !existsSync(script)) {
    throw new Error('second-opinion server not found');
  }
  let key;
  try {
    key = await fieldReader({
      provider: 'vibe',
      reason: 'Start Discord Ferry second opinion',
      home,
    });
  } catch (error) {
    // Every message on this path is a bounded constant from the credential
    // reader; the field value only ever exists on the success return.
    const detail = error instanceof Error && error.message ? `: ${error.message}` : '';
    throw new Error(`second-opinion credential unavailable${detail}`);
  }
  if (check) return { status: 0, signal: null, ready: true };

  let child;
  try {
    child = spawnChild(python, [script], {
      env: secondOpinionEnvironment(environment, key),
      stdio: 'inherit',
    });
  } catch {
    throw new Error('second-opinion process could not start');
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
      reject(new Error('second-opinion process could not start'));
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
    process.stderr.write('second-opinion-mcp: expected no arguments or --check\n');
    process.exitCode = 2;
    return;
  }
  try {
    const result = await runSecondOpinion({
      home: process.env.HOME,
      check: args[0] === '--check',
    });
    if (result.ready) {
      process.stdout.write('second-opinion credential ready\n');
    } else if (result.signal) {
      process.kill(process.pid, result.signal);
    } else {
      process.exitCode = result.status ?? 1;
    }
  } catch (error) {
    process.stderr.write(`second-opinion-mcp: ${error.message}\n`);
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
