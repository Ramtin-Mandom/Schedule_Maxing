#!/usr/bin/env node
/**
 * Strategic compact reminder (adapted from everything-claude-code's strategic-compact skill).
 *
 * PreToolUse: counts tool calls per session and, from COMPACT_THRESHOLD calls on (default 40, then every 20),
 * shows a reminder that a phase boundary is a good moment for /compact.
 * PreCompact: resets the session's counter.
 *
 * The counter is keyed by the session_id Claude Code passes on stdin. Never blocks a tool: always exits 0.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const THRESHOLD = parseInt(process.env.COMPACT_THRESHOLD || '40', 10);
const INTERVAL = parseInt(process.env.COMPACT_INTERVAL || '20', 10);

function readStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  } catch {
    return {};
  }
}

function main() {
  const input = readStdin();
  const session = String(input.session_id || 'default').replace(/[^\w-]/g, '');
  const counterFile = path.join(os.tmpdir(), `schedule-maxing-tool-count-${session}`);

  if (input.hook_event_name === 'PreCompact') {
    try { fs.unlinkSync(counterFile); } catch { /* nothing counted yet */ }
    return;
  }

  let count = 1;
  try {
    count = parseInt(fs.readFileSync(counterFile, 'utf8'), 10) + 1 || 1;
  } catch { /* first call of the session */ }
  fs.writeFileSync(counterFile, String(count));

  if (count === THRESHOLD || (count > THRESHOLD && (count - THRESHOLD) % INTERVAL === 0)) {
    process.stdout.write(JSON.stringify({
      systemMessage: `[strategic-compact] ${count} tool calls this session. If a phase just ended ` +
        '(exploration done, a fix landed, a test run finished), /compact now keeps the plan and drops the noise.',
    }));
  }
}

try {
  main();
} catch (err) {
  process.stderr.write(`[strategic-compact] ${err.message}\n`);
}
process.exit(0);
