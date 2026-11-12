'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..'),name='塔占比修改及增加Btile工具';
test('product name is consistent in visible header, browser title, runtime title and help',()=>{
 const html=fs.readFileSync(path.join(root,'frontend/index.html'),'utf8');
 assert.ok(html.includes(`<title>${name}</title>`));assert.ok(html.includes(`<span>${name}</span>`));
 assert.ok(fs.readFileSync(path.join(root,'frontend/assets/app.js'),'utf8').includes(' · '+name+'`'));
 assert.ok(fs.readFileSync(path.join(root,'scripts/app.py'),'utf8').includes(name+'本地服务'));
 assert.ok(fs.readFileSync(path.join(root,'docs/README.md'),'utf8').startsWith('# '+name+'\n'));
});
