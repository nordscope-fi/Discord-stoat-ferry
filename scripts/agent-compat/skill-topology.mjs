// Discord Ferry — Skill topology bridger.
// Creates .agents/skills/<name> symlinks pointing to ../../.claude/skills/<name>.
// Simplified one-directional bridge: all Ferry skills are Claude-owned.

import { existsSync, mkdirSync, readdirSync, readlinkSync, symlinkSync, unlinkSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

export function buildSkillPlan(projectRoot) {
  const claudeRoot = join(projectRoot, '.claude', 'skills');
  const agentRoot = join(projectRoot, '.agents', 'skills');
  const operations = [];
  const errors = [];
  const records = [];

  if (!existsSync(claudeRoot)) {
    errors.push(`Claude skills directory not found: ${claudeRoot}`);
    return { records, operations, errors };
  }

  const claudeSkills = readdirSync(claudeRoot).filter(name => {
    const skillPath = join(claudeRoot, name, 'SKILL.md');
    return existsSync(skillPath);
  });

  for (const name of claudeSkills) {
    const agentLink = join(agentRoot, name);
    const claudeDir = join(claudeRoot, name);
    const relTarget = relative(join(agentRoot), claudeDir);

    if (existsSync(agentLink)) {
      try {
        const current = readlinkSync(agentLink);
        if (current === relTarget) {
          records.push({ name, status: 'ok', type: 'claude-owned' });
          continue;
        }
        operations.push({ action: 'remove-link', path: agentLink, reason: 'stale target' });
      } catch {
        const stat = statSync(agentLink, { throwIfNoEntry: false });
        if (stat?.isDirectory()) {
          errors.push(`${name}: .agents/skills/${name} is a real directory, not a symlink. Remove it manually.`);
          continue;
        }
        operations.push({ action: 'remove-link', path: agentLink, reason: 'not a symlink' });
      }
    }

    operations.push({ action: 'create-link', path: agentLink, target: relTarget });
    records.push({ name, status: 'pending', type: 'claude-owned' });
  }

  if (existsSync(agentRoot)) {
    const agentEntries = readdirSync(agentRoot);
    for (const name of agentEntries) {
      if (claudeSkills.includes(name)) continue;
      const agentLink = join(agentRoot, name);
      try {
        readlinkSync(agentLink);
        operations.push({ action: 'remove-link', path: agentLink, reason: 'source removed' });
        records.push({ name, status: 'stale', type: 'obsolete' });
      } catch {
        // Real directory belonging to another tool; leave it alone.
      }
    }
  }

  return { records, operations, errors };
}

export function applySkillPlan(projectRoot, { operations, errors }) {
  if (errors.length > 0) {
    throw new Error(`Skill topology errors:\n${errors.join('\n')}`);
  }

  const agentRoot = join(projectRoot, '.agents', 'skills');
  mkdirSync(agentRoot, { recursive: true });

  for (const op of operations) {
    if (op.action === 'remove-link') {
      unlinkSync(op.path);
    } else if (op.action === 'create-link') {
      symlinkSync(op.target, op.path);
    }
  }

  return operations.length;
}
