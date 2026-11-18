const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, copyFile, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { encodeLevelSave } from '../前端/模块/level-bytes.js';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root=fileURLToPath(new URL('..',import.meta.url));

async function assertCleanRoot(directory) {
  const launcher = '启动关卡编辑器.command';
  const entries = await readdir(directory, {withFileTypes:true});
  const unexpected = entries
    .filter(entry => !entry.isDirectory() && !(entry.name === launcher && entry.isFile()))
    .map(entry => entry.name).sort();
  assert.deepEqual(unexpected, [], '编辑器根目录只能放启动器和文件夹，请将多余项目移到合适的子目录');
  assert.ok(entries.some(entry => entry.name === launcher && entry.isFile()), '编辑器根目录缺少启动器文件');
}

await assertCleanRoot(root);
const temp=await realpath(await mkdtemp(path.join(os.tmpdir(),'tile-editor-qa-')));
const repo=path.join(temp,'template-repo'), originalOutput=path.join(temp,'output');
const screenshotOutput=path.join(temp,'template-screenshots');
let output=originalOutput;
const directoryConfig=path.join(temp,'目录.json');
await mkdir(repo); await mkdir(output);
// Synthetic fixtures: no private template repository or project configs required.
const game=path.join(temp,'web-editor');
await mkdir(path.join(game,'csv'),{recursive:true});
await mkdir(path.join(game,'icon'));
const pixel=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1cAAAAASUVORK5CYII=','base64');
const catalog=['TileId,Des,IconType,Board,Icon,ColorTag,ThemeTag','int,string,int,string,string,string,string',',,,,,,'];
for(const n of [...Array.from({length:30},(_,i)=>i),300,301,302,304,313,...Array.from({length:32},(_,i)=>1999000+i)]){
  catalog.push(`${1000+n},${n===3?'水滴':'测试图案'+n},0,,${n===3?'':'qa'+n},blue,`);
  await writeFile(path.join(game,'icon',`qa${n}.png`),pixel);
}
await writeFile(path.join(game,'csv','config_client_tile.csv'),catalog.join('\n'));
await writeFile(directoryConfig,JSON.stringify({game_editor_data:game,template_repository:''}));
for(const [id,total] of [['666',60],['autoLevelv1list125',129],['xkd141',60],['mxd0069',60],['mxd0060',60]]) {
  const perLayer=total===129?43:30;
  const tiles=Array.from({length:total},(_,i)=>({
    x:total===129?(i%perLayer)%9*2:4+i%5*2,
    y:total===129?Math.floor((i%perLayer)/9)*2:4+Math.floor((i%perLayer)/5)*2,
    z:Math.floor(i/perLayer),tileId:total===129&&i>=99?-11:-10,debuffIndex:-1,
  }));
  if(id==='mxd0060')for(let i=0;i<3;i++)tiles[i].tileId=1003;
  await writeFile(path.join(repo,id+'_m.bytes'),encodeLevelSave({version:0,templateId:id,duplicateId:'',width:17,height:19,tiles,towers:[],debuffs:[]}));
}
let readyProcess;
const ready=new Promise((resolve,reject)=>{
  const python=spawn('python3',['-u','-c',
    "from server import create_server; import sys; s=create_server(repo=sys.argv[1], output=sys.argv[2], config_path=sys.argv[3], screenshot_output=sys.argv[4]); print(s.server_port,flush=True); s.serve_forever()",repo,output,directoryConfig,screenshotOutput],
    {cwd:root,env:{...process.env,PYTHONPATH:'后端',PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','pipe','inherit']});
  readyProcess=python;
  python.stdout.once('data',data=>resolve('http://127.0.0.1:'+String(data).trim()));
  python.once('exit',code=>reject(new Error('server exited '+code)));
});
let browser;
const screenshots=process.env.TILE_QA_SCREENSHOTS ?? path.join(root,'文档','验收截图');
await mkdir(screenshots,{recursive:true});

async function assertLightFormControls(page, stage) {
  assert.ok(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches),
    '表单颜色验收须在浏览器深色模式下运行');
  const controls = await page.locator('input, textarea, select').evaluateAll(fields => fields
    .filter(field => !(field instanceof HTMLInputElement && [
      'hidden', 'checkbox', 'radio', 'range', 'file', 'button', 'submit', 'reset', 'color', 'image',
    ].includes(field.type)))
    .map(field => {
      const {backgroundColor, color, colorScheme} = getComputedStyle(field);
      const element = field.id ? '#' + field.id : field.outerHTML.split('>')[0] + '>';
      return {element, backgroundColor, color, colorScheme};
    }));
  assert.ok(controls.length > 0, stage + '未找到输入控件');
  const mismatches = controls.filter(({backgroundColor, color, colorScheme}) =>
    backgroundColor !== 'rgb(255, 255, 255)' || color !== 'rgb(39, 56, 74)' ||
    !colorScheme.split(/\s+/).includes('light') || colorScheme.split(/\s+/).includes('dark')
  );
  assert.deepEqual(mismatches, [], stage + '的输入控件必须使用主题白底深字和浅色控件样式');
}

try {
  const endpoint=await ready;
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100},colorScheme:'dark'});
  await page.context().grantPermissions(['clipboard-read','clipboard-write']);
  const errors=[];
  page.on('pageerror',e=>{errors.push(String(e)); console.error(e);});
  await page.goto(endpoint);
  await page.locator('#normal-palette button').first().waitFor({timeout:12000});
  await assertLightFormControls(page, '编辑器启动');
  assert.equal(await page.locator('[data-service]').count(),0);
  const otherRepo = path.join(temp,'另一个仓库');
  await mkdir(otherRepo);
  const repositoryInput = page.getByLabel('模板仓库',{exact:true});
  const outputInput = page.getByLabel('输出模板',{exact:true});
  const savePath = page.getByRole('button',{name:'保存路径',exact:true});
  await page.locator('[data-settings]').click();
  await page.waitForFunction(expected => document.querySelector('#repository-path').value === expected, repo);
  await repositoryInput.fill(path.join(temp,'不存在的目录'));
  await savePath.click();
  await page.getByText('模板仓库目录不存在，请检查路径或重新选取目录。').waitFor();
  await repositoryInput.fill(otherRepo);
  await savePath.click();
  await page.getByText('路径已保存。',{exact:true}).waitFor();
  assert.equal(JSON.parse(await readFile(directoryConfig,'utf8')).template_repository,otherRepo);
  assert.deepEqual(await page.evaluate(() => fetch('/api/templates').then(response => response.json())),{ids:[]});
  await page.route('**/api/repository/pick?target=repository', route => route.fulfill({json:{repository:repo}}));
  await page.getByRole('button',{name:'选取目录',exact:true}).click();
  await page.getByText('点击「保存路径」应用所选目录。').waitFor();
  assert.equal(await repositoryInput.inputValue(),repo);
  output=path.join(temp,'新输出目录');
  await mkdir(output);
  await page.route('**/api/repository/pick?target=output', route => route.fulfill({json:{output}}));
  await page.getByRole('button',{name:'选取输出模板目录',exact:true}).click();
  await page.getByText('点击「保存路径」应用所选目录。').waitFor();
  assert.equal(await outputInput.inputValue(),output);
  for (const input of [repositoryInput,outputInput]) {
    await input.focus();
    assert.equal(await input.evaluate(el => getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
  }
  await page.route('**/api/repository/pick?target=game', route => route.fulfill({json:{game}}));
  await page.getByRole('button',{name:'选取 WebEditor 资源目录',exact:true}).click();
  assert.equal(await page.getByLabel('WebEditor 资源目录（可选）',{exact:true}).inputValue(),game);
  await savePath.click();
  await page.getByText('路径已保存。',{exact:true}).waitFor();
  assert.equal(JSON.parse(await readFile(directoryConfig,'utf8')).game_editor_data,game);
  assert.equal(JSON.parse(await readFile(directoryConfig,'utf8')).template_output,output);
  await page.getByRole('dialog',{name:'模板库路径'}).getByRole('button',{name:'关闭',exact:true}).click();
  await page.screenshot({path:path.join(screenshots,'01-打开工具.png')});
  const id=page.locator('#tplid');
  await id.fill('666'); await id.press('Enter');
  await page.getByText('模板已载入。',{exact:false}).waitFor();
  assert.equal(await page.locator('.cc-board:visible, canvas[data-canvas=play]:visible').count(),1);
  const board=page.getByRole('group',{name:'模板编辑画布'});
  const columns=await page.locator('[data-editpanel] .canvas-workspace').evaluate(el => {
    const [left,right]=[...el.children].map(child=>child.getBoundingClientRect());
    return {left:left.width,right:right.width,top:left.top};
  });
  assert.ok(Math.abs(columns.left/(columns.left+columns.right)-0.7)<0.01);
  assert.ok(columns.top<100);
  const maxZ=page.getByLabel('MaxZ',{exact:true});
  assert.equal(await maxZ.inputValue(),'无限制');
  const allTiles=await board.locator('.cc-tile').count();
  await page.getByRole('button',{name:'+1',exact:true}).click();
  await assertLightFormControls(page, 'MaxZ 启用');
  assert.equal(await maxZ.inputValue(),'1');
  assert.ok(await board.locator('.cc-tile').count()<allTiles);
  await page.getByRole('button',{name:'-1',exact:true}).click();
  assert.equal(await maxZ.inputValue(),'无限制');
  await maxZ.fill('2'); await maxZ.press('Enter');
  assert.equal(await maxZ.inputValue(),'2');
  await page.getByRole('button',{name:'恢复',exact:true}).click();
  assert.equal(await board.locator('.cc-tile').count(),allTiles);
  const normalPalette=page.locator('#normal-palette');
  assert.equal(await normalPalette.locator('button:visible').count(),8);
  const expand=page.getByRole('button',{name:'展开',exact:true});
  await expand.click();
  assert.ok(await normalPalette.locator('button:visible').count()>8);
  await page.getByRole('button',{name:'收起',exact:true}).click();
  assert.equal(await normalPalette.locator('button:visible').count(),8);
  const stats=page.getByRole('region',{name:'模板信息'});
  assert.equal(await stats.locator('section').count(),3);
  assert.equal(await stats.locator('dl').first().locator('dd').first().textContent(),'60');
  assert.ok(await stats.locator('[data-template-layer-counts] li[aria-label]').count()>0);
  const rect=await board.boundingBox();
  const pos={x:rect.x+3/18*rect.width,y:rect.y+3/20*rect.height};
  const before=await board.locator('.cc-tile').count();
  await board.focus(); await page.keyboard.press('q');
  await page.mouse.click(pos.x,pos.y);
  assert.equal(await board.locator('.cc-tile').count(),before+1);
  await page.mouse.click(pos.x,pos.y,{button:'right'});
  assert.equal(await board.locator('.cc-tile').count(),before);
  assert.equal(await board.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(224, 248, 226)');
  assert.equal(await board.locator('svg circle').count(),323);
  const emptyTowerCount=await board.locator('.board-tower').count();
  await page.keyboard.press('ArrowUp');
  await page.mouse.click(pos.x,pos.y);
  assert.equal(await board.locator('.board-tower').count(),emptyTowerCount+1);
  await page.keyboard.press('d');
  assert.equal(await page.getByRole('button',{name:'向右移动塔，快捷键 D'}).getAttribute('aria-pressed'),'true');
  await page.mouse.click(pos.x,pos.y);
  const movedTower=board.locator('.board-tower').filter({hasText:'↑'});
  assert.equal(await movedTower.evaluateAll(nodes=>nodes.some(node=>Math.abs(parseFloat(node.style.left)-4/18*100)<0.001)),true);
  await page.mouse.click(pos.x+rect.width/18,pos.y,{button:'right'});
  assert.equal(await board.locator('.board-tower').count(),emptyTowerCount);
  await page.keyboard.press('q');
  await page.locator('[data-move="r"]').click();
  await page.getByRole('button',{name:'撤销（Z）',exact:true}).click();
  await page.getByRole('button',{name:'重做（Shift+Z）',exact:true}).click();
  await page.screenshot({path:path.join(screenshots,'02-载入并编辑.png')});
  assert.equal(await page.locator('#tplid').inputValue(),'666');
  assert.equal(await page.locator('[data-newname], [data-snap]').count(),0);
  const tools=page.getByRole('complementary',{name:'编辑工具'});
  assert.ok(await tools.getByRole('heading',{name:'基础',exact:true}).isVisible());
  assert.ok(await tools.getByRole('heading',{name:'普通牌',exact:true}).isVisible());
  assert.ok(await tools.locator('img[src="/assets/unity/Editor_Delete.png"]').evaluate(img=>img.complete&&img.naturalWidth>0));
  assert.ok(await page.getByRole('button',{name:'顺时针旋转 90°'}).locator('svg').isVisible());
  const undo=page.getByRole('button',{name:'撤销（Z）',exact:true});
  const undoBefore=await undo.isEnabled();
  const initialBoardMarkup=await board.locator('.cc-tile').allTextContents();
  await page.getByRole('button',{name:'预览',exact:true}).click();
  assert.ok(await board.evaluate(el=>el.classList.contains('is-fill-preview')));
  await page.getByRole('button',{name:'退出预览',exact:true}).click();
  assert.equal(await undo.isEnabled(),undoBefore);
  assert.deepEqual(await board.locator('.cc-tile').allTextContents(),initialBoardMarkup);
  const name=id;
  const save=page.getByRole('button',{name:'另存为新模板'});
  await name.fill('invalid-name'); await save.click();
  await page.getByRole('alert').filter({hasText:'模板 ID 只能'}).waitFor();
  const sync = page.getByRole('button',{name:'同步到模板仓库'});
  const existingTemplate = path.join(repo,'666_m.bytes');
  const originalBytes = await readFile(existingTemplate);
  const duplicateOutput = path.join(output,'666_m.bytes');
  await assert.rejects(readFile(duplicateOutput),{code:'ENOENT'});
  await name.fill('666');
  await save.click();
  await page.getByRole('alert').filter({hasText:'已存在同名'}).waitFor();
  await assert.rejects(readFile(duplicateOutput),{code:'ENOENT'});
  assert.deepEqual(await readFile(existingTemplate),originalBytes);
  assert.equal(await sync.isEnabled(),false);

  await name.fill('LocalQA1'); await save.click();
  await page.getByText('已保存到输出模板：LocalQA1_m.bytes',{exact:false}).waitFor();
  await assert.rejects(readFile(path.join(originalOutput,'LocalQA1_m.bytes')));
  await assert.rejects(readFile(path.join(repo,'LocalQA1_m.bytes')));
  await page.screenshot({path:path.join(screenshots,'03-另存与同步.png')});
  await sync.click();
  await page.getByText('已同步到模板仓库：LocalQA1_m.bytes',{exact:false}).waitFor();
  assert.deepEqual(await readFile(path.join(output,'LocalQA1_m.bytes')),await readFile(path.join(repo,'LocalQA1_m.bytes')));
  await name.fill('LocalQA1'); await save.click();
  await page.getByRole('alert').filter({hasText:'已存在同名'}).waitFor();

  await name.fill('SyncConflictQA');
  await save.click();
  await page.getByText('已保存到输出模板：SyncConflictQA_m.bytes',{exact:false}).waitFor();
  const syncDestination = path.join(repo,'SyncConflictQA_m.bytes');
  const savedOutput = path.join(output,'SyncConflictQA_m.bytes');
  const savedBytes = await readFile(savedOutput);
  await assert.rejects(readFile(syncDestination),{code:'ENOENT'});
  // Another writer claims the name between Save As and Sync.
  await writeFile(syncDestination, originalBytes, {flag:'wx'});
  assert.notDeepEqual(savedBytes,originalBytes);
  await sync.click();
  await page.getByRole('alert').filter({hasText:/已存在同名.*本地输出仍保留/}).waitFor();
  assert.deepEqual(await readFile(syncDestination),originalBytes);
  assert.deepEqual(await readFile(savedOutput),savedBytes);
  await page.getByRole('button',{name:'一键试玩',exact:true}).click();
  assert.equal(await page.locator('.cc-board:visible, canvas[data-canvas=play]:visible').count(),1);
  const play=page.locator('canvas[data-canvas=play]');
  assert.ok(await play.isVisible());
  assert.ok(await page.getByRole('complementary',{name:'背包与道具'}).isVisible());
  const fitBox=await play.boundingBox();
  assert.ok(fitBox.height>850);
  await page.locator('[data-sim-runs]').fill('9');
  await page.getByRole('button',{name:'运行推演'}).click();
  await page.locator('.simulation-results').waitFor();
  assert.equal(await page.locator('.simulation-results tbody tr').count(),5);
  assert.match(await page.locator('.simulation-results tbody tr').first().textContent(),/\/9/);
  await page.screenshot({path:path.join(screenshots,'04-一键试玩.png')});
  await page.getByRole('button',{name:'返回编辑',exact:true}).click();
  await page.getByText('试玩与副本参数',{exact:true}).click();
  await page.getByText('试玩参数 · 实验组与种子').click();
  await assertLightFormControls(page, '试玩参数展开');
  await page.locator('[data-effective-dup]').fill('6|123|100|4');
  await page.getByRole('button',{name:'应用到当前档'}).click();
  await page.getByText('已将实验组生效参数应用到第 1 档。').waitFor();
  await page.locator('input[type=file][data-file]').setInputFiles(path.join(repo,'666_m.bytes'));
  await page.getByText('模板已载入。',{exact:false}).waitFor();
  assert.equal(await page.locator('.cc-board:visible, canvas[data-canvas=play]:visible').count(),1);
  await id.fill('autoLevelv1list125'); await id.press('Enter');
  await page.locator('[data-fn]').filter({hasText:'autoLevelv1list125'}).waitFor();
  const sample=async(seed)=>{
    const edit=page.locator('[data-editpanel]');
    const details=edit.locator('.preview-setup').filter({hasText:'实验组与种子'});
    if(!(await details.evaluate(el=>el.open))) await details.locator('summary').click();
    await page.locator('[data-effective-dup]').fill('6|'+seed+'|100|4');
    await page.getByRole('button',{name:'应用到当前档'}).click();
    await page.getByRole('button',{name:'一键试玩'}).click();
    await page.getByRole('button',{name:'复制牌面与参数'}).click();
    await page.getByText('已复制本次试玩参数和牌面。').waitFor();
    const copied=JSON.parse(await page.evaluate(()=>navigator.clipboard.readText()));
    assert.equal(copied.dupPara.seed,seed);
    assert.equal(copied.tiles.length,129);
    assert.ok(copied.tiles.every(t=>t[3]>0));
    await page.getByRole('button',{name:'返回编辑',exact:true}).click();
    return copied.tiles;
  };
  const previewButton=page.getByRole('button',{name:'预览',exact:true});
  const seedInput=page.getByRole('spinbutton',{name:'随机种子',exact:true});
  const boardIds=()=>board.locator('.cc-tile').evaluateAll(tiles=>tiles.map(tile=>tile.dataset.tileId));
  const originalIds=await boardIds();
  const maxZBefore=await maxZ.inputValue();
  await previewButton.click();
  await seedInput.fill('123');
  const faces123=await boardIds();
  assert.ok(faces123.every(id=>Number(id)>0));
  assert.ok(await board.locator('.preview-tile-icon').count()>0, JSON.stringify(await boardIds()));
  await seedInput.fill('124');
  assert.notDeepEqual(await boardIds(),faces123);
  await seedInput.fill('123');
  assert.deepEqual(await boardIds(),faces123);
  assert.equal(await undo.isVisible(),false);
  assert.equal(await page.locator('[data-export]').evaluate(button=>!!button.closest('[inert]')),true);
  const previewRect=await board.boundingBox();
  await board.click({position:{x:previewRect.width*.19,y:previewRect.height*.19}});
  await board.click({position:{x:previewRect.width*.19,y:previewRect.height*.19},button:'right'});
  await board.press('Delete');
  await board.press('z');
  assert.deepEqual(await boardIds(),faces123);
  await maxZ.fill('2'); await maxZ.press('Enter');
  assert.ok((await boardIds()).length<faces123.length);
  await page.screenshot({path:path.join(screenshots,'08-预览.png')});
  await assertLightFormControls(page, '预览');
  await page.getByRole('button',{name:'退出预览',exact:true}).click();
  assert.equal(await maxZ.inputValue(),maxZBefore);
  assert.deepEqual(await boardIds(),originalIds);
  const first=await sample(123);
  assert.deepEqual(await sample(123),first);
  assert.notDeepEqual(await sample(124),first);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1440);
  await page.setViewportSize({width:1152,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1152);
  await page.screenshot({path:path.join(screenshots,'05-紧凑桌面.png')});
  await page.setViewportSize({width:1440,height:1100});
  await page.getByRole('button',{name:'批量修改模板'}).click();
  await page.getByRole('heading',{name:'导入关卡模板'}).waitFor();
  await assertLightFormControls(page, '批量导入');
  await page.locator('#batch-ids').fill('666\nautoLevelv1list125\nqaNoSuchTemplate0987');
  await page.getByRole('button',{name:'读取模板并编辑'}).click();
  await page.locator('.batch-card').nth(1).waitFor();
  assert.equal(await page.locator('.batch-card .cc-board:visible').count(),2);
  assert.ok((await board.boundingBox()).width>600);
  assert.ok(await page.getByText('1 项读取失败').isVisible());
  const cards=page.locator('.batch-card');
  const right=page.locator('.unity-inspector');
  const rightBefore=await right.boundingBox();
  const beforeCount=await board.locator('.cc-tile').count();
  await page.locator('[data-editpanel] [data-move="r"]').click();
  assert.ok(await page.getByRole('button',{name:'撤销（Z）'}).isEnabled());
  const firstDraft=await board.locator('.cc-tile').first().getAttribute('style');
  await cards.nth(1).getByRole('button',{name:'编辑',exact:true}).click();
  assert.equal(await board.locator('.cc-tile').count(),129);
  assert.equal(await page.getByRole('button',{name:'撤销（Z）'}).isEnabled(),false);
  assert.equal((await right.boundingBox()).y,rightBefore.y);
  await page.screenshot({path:path.join(screenshots,'06-批量编辑.png')});
  await cards.nth(1).getByRole('button',{name:'试玩',exact:true}).click();
  await page.locator('[data-batch-play-id]').filter({hasText:'autoLevelv1list125'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'下一关'}).isEnabled(),false);
  await page.getByRole('button',{name:'上一关'}).click();
  await page.locator('[data-batch-play-id]').filter({hasText:'666'}).waitFor();
  await page.getByRole('button',{name:'下一关'}).click();
  await page.locator('[data-batch-play-id]').filter({hasText:'autoLevelv1list125'}).waitFor();
  await page.getByRole('button',{name:'返回编辑'}).click();
  await cards.first().getByRole('button',{name:'编辑',exact:true}).click();
  assert.equal(await board.locator('.cc-tile').count(),beforeCount);
  assert.equal(await board.locator('.cc-tile').first().getAttribute('style'),firstDraft);
  await id.fill('BatchQA1');
  await save.click();
  await page.getByText('已保存到输出模板：BatchQA1_m.bytes',{exact:false}).waitFor();
  await page.getByRole('button',{name:'返回单模板'}).click();
  assert.equal(await id.inputValue(),'autoLevelv1list125');
  await page.getByRole('button',{name:'使用说明'}).click();
  await page.getByRole('heading',{name:'使用说明',exact:true}).waitFor();
  await page.getByRole('searchbox',{name:'搜索使用说明'}).fill('贪心');
  assert.ok(await page.getByRole('heading',{name:'3. 试玩与通关推演'}).isVisible());
  assert.equal(await page.getByRole('heading',{name:'1. 载入关卡模板'}).isVisible(),false);
  await page.screenshot({path:path.join(screenshots,'07-使用说明.png')});
  await page.getByRole('button',{name:'关闭使用说明'}).click();
  await page.getByRole('button',{name:'批量修改模板'}).click();
  await page.getByRole('button',{name:'上传模板文件',exact:true}).click();
  const markerLabels = [...'ABCDEFGHIJKLMN'];
  const markerTemplate = path.join(temp,'MarkerQA_m.bytes');
  await writeFile(markerTemplate, encodeLevelSave({
    version: 0, templateId: 'MarkerQA', duplicateId: '', width: 17, height: 19,
    tiles: markerLabels.map((_, i) => ({x: (i % 7) * 2, y: Math.floor(i / 7) * 2, z: 0, tileId: -10 - i, debuffIndex: -1})),
    towers: [], debuffs: [],
  }));
  await page.locator('#batch-files').setInputFiles([path.join(repo,'666_m.bytes'),path.join(repo,'autoLevelv1list125_m.bytes'),markerTemplate]);
  await page.getByRole('button',{name:'读取模板并编辑'}).click();
  await page.locator('.batch-card.is-active').filter({hasText:'666'}).waitFor();
  assert.equal(await page.locator('.batch-card:visible').count(),3);
  const markerCard = page.locator('.batch-card').filter({has: page.getByRole('heading',{name:'MarkerQA',exact:true})});
  const checkMarkers = async container => {
    const faces = await container.locator('.cc-tile').evaluateAll(tiles => Object.fromEntries(
      tiles.map(tile => [tile.dataset.tileId, tile.querySelector('.cc-tile-label').textContent])
    ));
    assert.deepEqual(faces, Object.fromEntries(markerLabels.map((label, i) => [-10 - i, label])));
  };
  await checkMarkers(markerCard.locator('.batch-board-placeholder'));
  await markerCard.getByRole('button',{name:'编辑',exact:true}).click();
  await checkMarkers(board);
  await page.getByRole('button',{name:'返回单模板'}).click();
  await page.locator('[data-file]').setInputFiles(markerTemplate);
  await page.locator('[data-fn]').filter({hasText:'MarkerQA_m.bytes'}).waitFor();
  await checkMarkers(board);
  await page.getByRole('button',{name:'批量换牌',exact:true}).click();
  await board.locator('[data-tile-id="-10"]').click({force:true});
  await page.getByRole('button',{name:'随机标记 G，快捷键 5',exact:true}).click();
  assert.match(await page.locator('[data-toast]').textContent(),/换成「G」/);
  await page.getByRole('button',{name:'撤销（Z）',exact:true}).click();

  // Simulate a missing icon before loading a fixed tile that uses it.
  await page.route('**/icon/1003_blue_1-v2.png', route => route.abort());
  await page.reload();
  await page.locator('#normal-palette button').first().waitFor();
  const namedTemplate = path.join(temp,'NamedQA_m.bytes');
  await writeFile(namedTemplate, encodeLevelSave({
    version: 0, templateId: 'NamedQA', duplicateId: '', width: 17, height: 19,
    tiles: [{x: 8, y: 8, z: 0, tileId: 1003, debuffIndex: -1}], towers: [], debuffs: [],
  }));
  await page.locator('[data-file]').setInputFiles(namedTemplate);
  await page.locator('[data-fn]').filter({hasText:'NamedQA_m.bytes'}).waitFor();
  await previewButton.click();
  await board.getByText('水滴',{exact:true}).waitFor();
  await page.getByRole('button',{name:'退出预览',exact:true}).click();
  await page.evaluate(() => {
    window.drawnTileLabels = [];
    const fillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
      if (this.canvas.matches('[data-canvas="play"]')) window.drawnTileLabels.push(text);
      return fillText.call(this, text, ...args);
    };
  });
  await page.getByRole('button',{name:'一键试玩',exact:true}).click();
  await page.locator('[data-issues]').getByText(/水滴 共 1 个/).waitFor();
  await page.waitForFunction(() => window.drawnTileLabels.includes('水滴'));
  assert.ok(!await page.evaluate(() => window.drawnTileLabels.includes('1003')));
  const playBounds = await play.boundingBox();
  await play.click({position:{x:playBounds.width / 2,y:playBounds.height / 2 + 9}});
  await page.locator('[data-trayui]').getByText('水滴',{exact:true}).waitFor();
  await page.getByRole('button',{name:'返回编辑',exact:true}).click();
  await page.getByRole('button',{name:'新建空模板',exact:true}).click();
  await page.getByRole('button',{name:'随机标记 A，快捷键 Q',exact:true}).click();
  const grid = await board.boundingBox();
  const gridPoint = (x, y) => ({
    x: grid.x + (x + 1) * grid.width / 18,
    y: grid.y + (y + 1) * grid.height / 20,
  });
  const clickGrid = async (x, y) => {
    const target = gridPoint(x, y);
    await page.mouse.click(target.x, target.y);
  };
  await clickGrid(8,8);
  const diagonal = gridPoint(8.8,8.8);
  assert.match(await page.evaluate(({x,y}) => document.elementFromPoint(x,y)
    ?.closest('.cc-tile')?.getAttribute('title') ?? '', diagonal), /\(8, 8, 0\)$/);
  await page.mouse.move(diagonal.x, diagonal.y);
  const cursor = board.locator('.cc-tile-cursor');
  assert.equal(await cursor.getAttribute('data-label'),'A · 第 2 层');
  const cursorPosition = await cursor.evaluate(el => [el.style.left, el.style.top]);
  await clickGrid(8.8,8.8);
  const added = board.locator('.cc-tile[title$="(9, 9, 1)"]');
  assert.equal(await added.count(),1,'点击旧牌边缘后，新牌应叠在光标指向的相邻坐标');
  assert.deepEqual(await added.evaluate(el => [el.style.left, el.style.top]),cursorPosition);
  for (const [x, y] of [[0,0],[16,0],[0,18],[16,18]]) {
    await clickGrid(x,y);
    assert.equal(await board.locator(`.cc-tile[title$="(${x}, ${y}, 0)"]`).count(),1);
  }
  assert.equal(await board.evaluate(el => getComputedStyle(el).outlineStyle),'none');
  await page.getByRole('button',{name:'批量截图',exact:true}).click();
  const utility = page.locator('[data-template-batch]');
  await utility.getByLabel('模板 ID 或关卡 ID').fill('| 666 | 15\\|3\\|100\\|4 |\n| 666 | 16\\|3\\|100\\|4 |');
  await utility.getByRole('button',{name:'生成 PNG'}).click();
  await utility.getByRole('status').filter({hasText:'已保存 2 张 PNG'}).waitFor();
  const patternedFiles = await readdir(screenshotOutput);
  assert.deepEqual(patternedFiles.sort(),['666_15|3|100|4.png','666_16|3|100|4.png']);
  for (const name of patternedFiles) {
    const png = await readFile(path.join(screenshotOutput,name));
    assert.equal(png.subarray(1,4).toString(),'PNG');
    assert.equal(png.readUInt32BE(16),1200);
  }
  await utility.getByRole('button',{name:'关闭'}).click();
  await page.getByRole('button',{name:'批量截图',exact:true}).click();
  await utility.getByLabel('不带图案',{exact:true}).check();
  assert.equal(await utility.locator('[data-tool-config]').isVisible(),false);
  const duplicateIds = 'xkd141\nxkd141\nmxd0069\nmxd0069\nmxd0060\nmxd0060';
  await utility.getByLabel('模板 ID',{exact:true}).fill(duplicateIds);
  await page.evaluate(() => {
    window.templateLabels = [];
    const fillText = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
      if (this.canvas.width === 1200 && this.canvas.height === 1350) window.templateLabels.push(text);
      return fillText.call(this, text, ...args);
    };
  });
  await utility.getByRole('button',{name:'生成 PNG'}).click();
  await utility.getByRole('status').filter({hasText:'已保存 3 张 PNG'}).waitFor();
  assert.deepEqual((await readdir(screenshotOutput)).sort(),[
    ...patternedFiles, 'mxd0060.png', 'mxd0069.png', 'xkd141.png',
  ]);
  const templateLabels = await page.evaluate(() => window.templateLabels);
  assert.ok(templateLabels.includes('A?'));
  assert.ok(templateLabels.some(label => /^\d+$/.test(label)));
  await page.route('**/api/template-screenshot/open-folder', route => route.fulfill({json:{ok:true}}));
  const openFolderRequest = page.waitForRequest('**/api/template-screenshot/open-folder');
  await utility.getByRole('button',{name:'打开截图文件夹'}).click();
  assert.equal((await openFolderRequest).method(),'POST');
  await utility.getByRole('button',{name:'关闭'}).click();
  await page.getByRole('button',{name:'导出模板数据',exact:true}).click();
  assert.equal(await utility.locator('[data-tool-config]').isVisible(),false);
  await utility.getByLabel('模板 ID',{exact:true}).fill(duplicateIds);
  await utility.getByLabel('上传模板文件').setInputFiles(path.join(repo,'xkd141_m.bytes'));
  await utility.getByRole('button',{name:'查看模板数据'}).click();
  await utility.getByRole('status').filter({hasText:'已生成 3 行'}).waitFor();
  assert.deepEqual(await utility.locator('tbody tr td:first-child').allTextContents(),['xkd141','mxd0069','mxd0060']);
  const downloadPending = page.waitForEvent('download');
  await utility.getByRole('button',{name:'导出 CSV'}).click();
  const download = await downloadPending;
  const csv = await readFile(await download.path(),'utf8');
  assert.ok(csv.includes('template_id,Tile数量') && !csv.includes('随机种子'));
  assert.equal(csv.split('\r\n').length,4);
  await utility.getByRole('button',{name:'关闭'}).click();
  await assertLightFormControls(page, '验收结束');
  assert.deepEqual(errors,[]);
} finally {
  await browser?.close(); readyProcess?.kill(); await rm(temp,{recursive:true,force:true});
}
await assertCleanRoot(root);
console.log('交付验收通过：根目录、深色模式表单、光标落牌、重名另存与同步保护、预览、试玩和批量编辑。');
