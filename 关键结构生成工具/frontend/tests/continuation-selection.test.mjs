import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
const app=fs.readFileSync(new URL("../assets/app.js",import.meta.url),"utf8");
const start=app.indexOf("  function continuationItemsForMode(");
const end=app.indexOf("  function continuationSelectedItems(",start);
const select=start<0 ? null : Function(`${app.slice(start,end)};return continuationItemsForMode;`)();
const items=[{template_key:"first",plan_id:"s5"},{template_key:"second",plan_id:"s5"},{template_key:"third",plan_id:"s5"}];
test("individual mode submits only the current dropdown template",()=>{
  assert.equal(typeof select,"function");
  assert.deepEqual(select(items,"individual","second::s5",["first::s5","third::s5"]),[items[1]]);
});
test("uniform mode submits exactly the checked templates",()=>{
  assert.deepEqual(select(items,"uniform","second::s5",["first::s5","third::s5"]),[items[0],items[2]]);
});

const slice=(from,to)=>app.slice(app.indexOf(from),app.indexOf(to,app.indexOf(from)));
const labelHelpers=Function(`
  const STRUCTURE_TYPES="CDEFGHIJKLMN".split("");
  const MAJORITY_A_ON_TIE="majority_a_on_tie";
  const clone=value=>JSON.parse(JSON.stringify(value));
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
  ${slice("  function newPlanModel(","  function planSignature(")}
  ${slice("  function parseStructureGroupVector", "  function unitRoles")}
  ${slice("  function generationSpec(","  function toPlanSpec(")}
  ${slice("  function continuationPlanParameters(","  function continuationSelectedItems(")}
  return {newPlanModel,label:typeof continuationTargetLabel==='function'?continuationTargetLabel:null};
`)();
test("individual dropdown marks actual edits and clears the marker when reverted",()=>{
  assert.equal(typeof labelHelpers.label,"function");
  const baseline=labelHelpers.newPlanModel();
  const draft=structuredClone(baseline);
  const item={template_key:"first",plan_id:"s5",candidates:[{},{}]};
  assert.equal(labelHelpers.label(item,draft,baseline),"first · s5 · 已有 2 个候选");
  draft.candidateCount=7;
  assert.equal(labelHelpers.label(item,draft,baseline),"first · s5 · 已有 2 个候选（已修改参数）");
  draft.candidateCount=6;
  assert.doesNotMatch(labelHelpers.label(item,draft,baseline),/已修改参数/);
});
test("inactive form values do not mark an unchanged template",()=>{
  assert.equal(typeof labelHelpers.label,"function");
  const baseline=labelHelpers.newPlanModel();
  baseline.maxLayerKind="none";
  const draft=structuredClone(baseline);
  draft.steps[1]=4;
  draft.maxLayerValue=70;
  draft.firstRecycleLeadTiles=8;
  draft.customGroupsPerType="2；2；1";
  draft.layout="y";
  assert.doesNotMatch(labelHelpers.label(items[0],draft,baseline),/已修改参数/);
  draft.maxLayerKind="percent";
  assert.match(labelHelpers.label(items[0],draft,baseline),/已修改参数/);
});
