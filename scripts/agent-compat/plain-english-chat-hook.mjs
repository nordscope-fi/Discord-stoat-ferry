#!/usr/bin/env node

import { execFileSync, spawn } from 'node:child_process';
import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

export const DIRECT_CHAT_ARGV = [
  '--no-install', 'plain-english', 'hook', 'chat', '--agent', 'codex',
];
export const DIRECT_CHAT_COMMAND = ['npx', ...DIRECT_CHAT_ARGV].join(' ');

export function canonicalCheckoutRoot(root) {
  const common = execFileSync(
    'git', ['rev-parse', '--path-format=absolute', '--git-common-dir'],
    { cwd: root, encoding: 'utf8' },
  ).trim();
  return dirname(realpathSync(common));
}

export function removeUnusedIssueChannel(document) {
  document.hooks = document.hooks ?? {};
  delete document.hooks.Issue;
  return document;
}

export function transformCodexChatHooks(document, projectRoot) {
  const wrapper = `node "${projectRoot}/.codex/bin/plain-english-chat-hook.mjs"`;
  for (const event of ['Stop', 'SubagentStop']) {
    const matches = [];
    for (const group of document.hooks?.[event] ?? []) {
      for (const hook of group.hooks ?? []) {
        if (hook.command === DIRECT_CHAT_COMMAND) matches.push(hook);
      }
    }
    if (matches.length !== 1) {
      throw new Error(`expected one direct chat hook for ${event}; found ${matches.length}`);
    }
    matches[0].command = wrapper;
    matches[0].timeout = 60;
  }
  return document;
}

function passThrough() {
  const child = spawn(
    'npx',
    DIRECT_CHAT_ARGV,
    { stdio: ['inherit', 'inherit', 'inherit'], detached: true },
  );
  let killTimer;
  const stopGroup = () => {
    try { process.kill(-child.pid, 'SIGTERM'); } catch {}
    killTimer = setTimeout(() => {
      try { process.kill(-child.pid, 'SIGKILL'); } catch {}
    }, 2000);
  };
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, stopGroup);
  }
  child.on('error', () => process.exit(1));
  child.on('exit', (code, signal) => {
    if (killTimer) clearTimeout(killTimer);
    if (signal) process.kill(process.pid, signal);
    process.exit(code ?? 1);
  });
}

function selfTest() {
  const expected = ['npx', ...DIRECT_CHAT_ARGV].join(' ');
  if (expected !== DIRECT_CHAT_COMMAND) {
    process.stderr.write('plain-english-chat-hook self-test: command drift\n');
    process.exit(1);
  }
  process.stderr.write('plain-english-chat-hook self-test: all checks passed\n');
}

function main() {
  if (process.argv[2] === '--self-test') {
    selfTest();
    return;
  }
  if (process.argv[2] === '--transform') {
    const [path, root] = process.argv.slice(3);
    if (!path || !root) throw new Error('--transform requires a hooks path and project root');
    const document = JSON.parse(readFileSync(path, 'utf8'));
    transformCodexChatHooks(document, root);
    writeFileSync(path, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o600 });
    return;
  }
  passThrough();
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  try {
    main();
  } catch (err) {
    process.stderr.write(`${err.message}\n`);
    process.exit(1);
  }
}
