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

test('fallbackTab: disabled module goes to portfolio', () => {
  assert.equal(F.fallbackTab('shadow', { ...allOff, shadow: false }), 'portfolio');
  assert.equal(F.fallbackTab('shadow', { ...allOff, shadow: true }), 'shadow');
});

test('fallbackTab: core tabs pass through', () => {
  for (const t of ['portfolio', 'triggers', 'advice', 'history', 'settings']) {
    assert.equal(F.fallbackTab(t, allOff), t);
  }
});

test('fallbackTab: missing state counts as default (off)', () => {
  assert.equal(F.fallbackTab('memory', {}), 'portfolio');
});

test('isEnabled works without a DOM: non-modules true, modules default off', () => {
  assert.equal(F.isEnabled('settings'), true);
  assert.equal(F.isEnabled('screener'), false);
});
