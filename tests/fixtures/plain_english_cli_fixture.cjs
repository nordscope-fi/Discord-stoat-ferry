#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const args = process.argv.slice(2);
if (args[0] === '--version') {
  process.stdout.write(`${process.env.FERRY_PLAIN_ENGLISH_VERSION || '0.24.1'}\n`);
  process.exit(0);
}

if (args[0] === 'hook') {
  const child = spawn('sleep', ['600'], { stdio: 'ignore' });
  fs.writeFileSync(process.env.FERRY_PLAIN_ENGLISH_CLI_PID, String(process.pid));
  fs.writeFileSync(process.env.FERRY_PLAIN_ENGLISH_CHILD_PID, String(child.pid));
  child.once('exit', () => process.exit(0));
  return;
}

if (args[0] !== 'init') process.exit(2);
if (process.env.FERRY_PLAIN_ENGLISH_INIT_MARKER) {
  fs.appendFileSync(
    process.env.FERRY_PLAIN_ENGLISH_INIT_MARKER,
    `${JSON.stringify(args)}\n`,
  );
}
const agent = args[args.indexOf('--agent') + 1];
const root = args[args.indexOf('--root') + 1];
const runner = [
  '#!/usr/bin/env node',
  'import { spawn, spawnSync } from "node:child_process";',
  'const child = spawn("plain-english", process.argv.slice(2), {',
  '  stdio: "inherit", detached: process.platform !== "win32",',
  '});',
  'const relay = (signal) => {',
  '  if (child.pid !== undefined) {',
  '    if (process.platform === "win32") {',
  '      spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"]);',
  '    } else {',
  '      try { process.kill(-child.pid, signal); } catch {}',
  '    }',
  '  }',
  '  process.kill(process.pid, signal);',
  '};',
  'for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {',
  '  process.once(signal, () => relay(signal));',
  '}',
  'child.once("exit", (status, signal) => {',
  '  if (signal) process.kill(process.pid, signal);',
  '  else process.exit(status ?? 0);',
  '});',
].join('\n') + '\n';

if (agent === 'codex') {
  const directory = path.join(root, '.codex', 'hooks');
  fs.mkdirSync(directory, { recursive: true });
  const hooksPath = path.join(root, '.codex', 'hooks.json');
  const document = JSON.parse(fs.readFileSync(hooksPath, 'utf8'));
  document.hooks.Issue = [{ matcher: 'mcp__linear__save_issue', hooks: [] }];
  const command = 'node "$(git rev-parse --show-toplevel)/.codex/hooks/' +
    'plain-english.mjs" hook chat --agent codex';
  for (const event of ['Stop', 'SubagentStop']) {
    document.hooks[event] = [{
      matcher: '*', hooks: [{ type: 'command', command, timeout: 10 }],
    }];
  }
  fs.writeFileSync(hooksPath, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o600 });
  fs.writeFileSync(path.join(directory, 'plain-english.mjs'), runner, { mode: 0o755 });
} else if (agent === 'vibe') {
  const directory = path.join(root, '.vibe', 'hooks');
  fs.mkdirSync(directory, { recursive: true });
  const hooksPath = path.join(root, '.vibe', 'hooks.toml');
  const blocks = [
    ['plain-english-docs', 'pre_tool', 'write_file|edit', 'docs'],
    ['plain-english-issue', 'pre_tool', '.*_save_(issue|comment)', 'issue'],
    ['plain-english-chat', 'post_agent', '', 'chat'],
  ].map(([name, type, match, channel]) => [
    '[[hooks]]', `name = "${name}"`, `type = "${type}"`,
    ...(match ? [`match = "re:${match}"`] : []),
    `command = "node .vibe/hooks/plain-english.mjs hook ${channel} --agent vibe"`,
    'timeout = 10', '',
  ].join('\n')).join('\n');
  const existing = fs.readFileSync(hooksPath, 'utf8');
  const ferry = existing.split(/(?=\[\[hooks\]\])/u)
    .filter((block) => !block.includes('plain-english-')).join('').trimEnd();
  fs.writeFileSync(hooksPath, `${ferry}\n\n${blocks}`);
  fs.writeFileSync(path.join(directory, 'plain-english.mjs'), runner, { mode: 0o755 });
  fs.writeFileSync(path.join(directory, 'plain-english-judge.mjs'), '#!/usr/bin/env node\n', {
    mode: 0o755,
  });
  for (const channel of ['docs', 'github', 'issue']) {
    fs.writeFileSync(path.join(directory, `plain-english-${channel}.prompt.md`), `${channel}\n`);
  }
}
