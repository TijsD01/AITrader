import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const F = require('../../src/web/static/features.js');

const allOff = { screener: false, debate: false, shadow: false, memory: false };

test('registry has the four modules, all off by default', () => {
  assert.deepEqual(F.MODULES.map(m => m.id), ['screener', 'debate', 'shadow', 'memory']);
  for (const m of F.MODULES) {
    assert.equal(m.defaultOn, false);
    assert.ok(m.label && m.icon && m.description, m.id);
  }
});

test('resolveModules: garbage input gives defaults', () => {
  for (const raw of [null, undefined, 'x', 'null', [], [1], 42, true]) {
    assert.deepEqual(F.resolveModules(raw), allOff, JSON.stringify(raw));
  }
});

test('resolveModules: only booleans count, unknown ids dropped', () => {
  assert.deepEqual(
    F.resolveModules({ screener: true, debate: 'yes', bogus: true }),
    { screener: true, debate: false, shadow: false, memory: false },
  );
});

test('isEnabled works without a DOM: non-modules true, modules default off', () => {
  for (const t of ['portfolio', 'triggers', 'advice', 'history', 'settings']) assert.equal(F.isEnabled(t), true, t);
  for (const m of ['screener', 'debate', 'shadow', 'memory']) assert.equal(F.isEnabled(m), false, m);
});
