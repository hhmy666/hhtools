'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const V=require('../frontend/assets/view.js');
test('IDs accept suffixes and separators, deduplicate without changing case',()=>{
 assert.deepEqual(V.parseIds('foo_m.bytes\nbar, foo_m.bytes；Baz'),['foo','bar','Baz']);
 assert.throws(()=>V.parseIds('../secret'),/模板 ID/);
});
test('search, result filter and paging keep errors visible and clamp pages',()=>{
 const rows=[{name:'abc',status:'issue',adjacent_count:1},{name:'bad',status:'error',error:'文件缺失'},{name:'ok',status:'clean'}];
 assert.equal(V.filtered(rows,'','all').length,3);
 assert.equal(V.filtered(rows,'缺失','all')[0].name,'bad');
 assert.equal(V.filtered(rows,'','error').length,1);
 assert.equal(V.pageRows(rows,8,20).page,1);
 assert.equal(V.pageRows([],1,20).pages,1);
});
test('progress only fills after the whole task succeeds',()=>{
 assert.equal(V.progress({status:'running',completed:10,total:10}),99);
 assert.equal(V.progress({status:'succeeded',completed:10,total:10}),100);
 assert.equal(V.progress({status:'running',completed:0,total:5}),0);
});
test('coordinate description distinguishes adjacent and floating without modifying layers',()=>{
 assert.equal(V.describe({kind:'floating',x:8,y:7,lower_layer:16,upper_layer:19}),'悬空全遮盖 · (8, 7) · 第16、19层');
});
