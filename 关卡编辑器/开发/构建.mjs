import {stripTypeScriptTypes} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const source = process.argv[2];
if (!source) throw new Error('请指定 WebEditor/src 的目录：node 开发/构建.mjs /path/to/WebEditor/src');
const out = new URL('../前端/模块/', import.meta.url);
const built = new Set;
function build(name) {
  if (built.has(name)) return;
  built.add(name);
  let s = fs.readFileSync(path.join(source, name), 'utf8');
  const custom = new URL(name, import.meta.url);
  if (fs.existsSync(custom)) s = fs.readFileSync(custom, 'utf8');
  let js = stripTypeScriptTypes(s, {mode:'strip'});
  // Upstream bundler erases implicit type-only imports too.
  js = js.replace(/(import\s*\{)([^}]*)(\}\s*from\s*['"]\.\/([^'"]+)\.ts['"])/g, (_,a,b,c,dep) => {
    build(dep+'.ts');
    return a+b.split(',').map(x=>x.trim()).filter(x=>x&&!['Master','EdTile'].includes(x)).join(', ')+c;
  });
  js = js.replace(/(from\s*['"]\.\/[^'"]+)\.ts(['"])/g, '$1.js$2');
  fs.writeFileSync(new URL(name.replace(/\.ts$/,'.js'),out),js);
}
build('level-editor.ts');
console.log('生成浏览器原生模块：'+built.size);
