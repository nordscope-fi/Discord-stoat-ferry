#!/usr/bin/env node
// Test helper: reads a JSON array of command strings on stdin and prints a JSON array of
// booleans, one per command, from the shared destructive git detector. It only classifies
// text. It never runs any of the commands.

import { readFileSync } from 'node:fs';
import { isDestructiveGitCommand } from '../../scripts/agent-compat/destructive-git.mjs';

const commands = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(commands.map(isDestructiveGitCommand)));
