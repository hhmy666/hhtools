import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../assets/app.js', import.meta.url), 'utf8');
const helpers = source.slice(source.indexOf('  function budgetAttemptRange('), source.indexOf('  function createSearchBudgetPanel('));
const ctx = vm.createContext({});
vm.runInContext('const compactInteger = n => String(n);' + helpers, ctx);

test('shared budget changes show only the remaining work-item attempts', () => {
  ctx.entries = [{scope:'per_work_item', base_attempt_budget:2000, current_attempt_budget:2000,
    attempts_completed:2000, unfinished_candidate_slots:4}];
  assert.equal(vm.runInContext('selectedAdditionalAttemptsRange(entries, [], 2, 4, 1)', ctx), '2000');
  assert.equal(vm.runInContext('selectedAdditionalAttemptsRange(entries, [], 1, 4, 1)', ctx), '0');
  ctx.entries[0].attempts_completed = 900;
  assert.equal(vm.runInContext('selectedAdditionalAttemptsRange(entries, [], 1, 4, 1)', ctx), '1100');
});

test('recommended shared increments do not depend on missing candidate count', () => {
  ctx.recommended = [{scope:'per_work_item', additional_attempts:2000, resumable:true}];
  assert.equal(vm.runInContext('selectedAdditionalAttemptsRange([], recommended, 2, 2, 1)', ctx), '2000');
});
