#!/usr/bin/env node

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

function stopProcessGroup(child, signal) {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch {
    // The child may already have exited.
  }
}

function runChild(command, args, { root, timeoutMs }) {
  return new Promise((finish) => {
    const child = spawn(command, args, {
      cwd: root,
      detached: true,
      env: process.env,
      stdio: ['ignore', 'ignore', 'ignore'],
    });
    let timedOut = false;
    let settled = false;
    let killTimer = null;
    const timeout = setTimeout(() => {
      timedOut = true;
      stopProcessGroup(child, 'SIGTERM');
      killTimer = setTimeout(() => stopProcessGroup(child, 'SIGKILL'), 2000);
      killTimer.unref();
    }, timeoutMs);
    const done = (status) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (killTimer) clearTimeout(killTimer);
      finish({ status: timedOut ? 124 : status });
    };
    child.once('error', () => done(1));
    child.once('close', (status) => done(status ?? 1));
  });
}

function safeFiles(root, files) {
  const checkout = resolve(root);
  return [...new Set(files)].filter((file) => {
    const target = resolve(checkout, file);
    const offset = relative(checkout, target);
    return offset && !offset.startsWith('..') && !isAbsolute(offset) && existsSync(target);
  }).sort();
}

function layerCommands(markdownFiles, helperFiles) {
  const documentation = [
    ['./scripts/assert-doc-refs.sh', []],
  ];
  if (markdownFiles.length) {
    documentation.push(['npx', ['--no-install', 'plain-english', 'lint', ...markdownFiles]]);
  }
  return [
    {
      name: 'compatibility',
      timeoutMs: 180_000,
      commands: [['./scripts/agent-check.sh', ['--ci']]],
    },
    {
      name: 'project',
      timeoutMs: 1_200_000,
      commands: [
        ['uv', ['run', 'ruff', 'check', '.']],
        ['uv', ['run', 'ruff', 'format', '--check', '.']],
        ['uv', ['run', 'mypy', 'src/']],
        ['uv', ['run', 'pytest']],
      ],
    },
    { name: 'documentation', timeoutMs: 180_000, commands: documentation },
    {
      name: 'helper-self-tests',
      timeoutMs: 300_000,
      commands: helperFiles.map((file) => [process.execPath, [file, '--self-test']]),
    },
  ];
}

function checkName(command, args) {
  if (command === process.execPath && args[0]) return args[0];
  return [command, ...args].join(' ');
}

export async function runVerificationLayers({
  run = runChild,
  markdownFiles = [],
  helperFiles = [],
  root = process.cwd(),
  now = () => performance.now(),
}) {
  const checkout = resolve(root);
  const markdown = safeFiles(checkout, markdownFiles);
  const helpers = safeFiles(checkout, helperFiles);
  const completed = [];
  const layers = [];
  for (const layer of layerCommands(markdown, helpers)) {
    const started = now();
    let passed = true;
    let failedCheck = null;
    const checks = [];
    for (const [command, args] of layer.commands) {
      const checkStarted = now();
      let result;
      try {
        result = await run(command, args, {
          root: checkout,
          timeoutMs: layer.timeoutMs,
          layer: layer.name,
        });
      } catch {
        result = { status: 1 };
      }
      const name = checkName(command, args);
      checks.push({
        name,
        status: result?.status === 0 ? 'ok' : 'fail',
        duration_ms: Math.max(0, Math.round(now() - checkStarted)),
      });
      if (result?.status !== 0) {
        passed = false;
        failedCheck = name;
        break;
      }
    }
    layers.push({
      name: layer.name,
      status: passed ? 'ok' : 'fail',
      duration_ms: Math.max(0, Math.round(now() - started)),
      checks,
    });
    if (!passed) {
      return { completed, failed_layer: layer.name, failed_check: failedCheck, layers };
    }
    completed.push(layer.name);
  }
  return { completed, failed_layer: null, failed_check: null, layers };
}

function changedFiles(root) {
  const result = spawnSync('git', ['status', '--porcelain=v1', '--untracked-files=all'], {
    cwd: root,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  if (result.status !== 0) throw new Error('changed-file discovery failed');
  return result.stdout.split(/\r?\n/u).filter(Boolean).map((line) => {
    const value = line.slice(3);
    return value.includes(' -> ') ? value.split(' -> ').at(-1) : value;
  });
}

function productionInputs(root) {
  const changed = safeFiles(root, changedFiles(root));
  const markdownFiles = changed.filter((file) => file.endsWith('.md'));
  const helperFiles = changed.filter((file) =>
    file.startsWith('scripts/agent-compat/') && file.endsWith('.mjs') &&
    advertisesSelfTest(readFileSync(resolve(root, file), 'utf8')));
  return { markdownFiles, helperFiles };
}

export function advertisesSelfTest(source) {
  return typeof source === 'string' &&
    /(?:^|\n)(?:async\s+)?function\s+selfTest\s*\(/u.test(source) &&
    source.includes('--self-test');
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 1 || args[0] !== '--json') {
    throw new Error('verify-all requires --json');
  }
  const root = process.cwd();
  const report = await runVerificationLayers({ root, ...productionInputs(root) });
  process.stdout.write(`${JSON.stringify(report)}\n`);
  if (report.failed_layer) process.exitCode = 1;
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
    process.stderr.write(`verify-all: ${error.message}\n`);
    process.exitCode = 1;
  });
}
