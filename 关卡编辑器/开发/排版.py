from pathlib import Path
import re
root=Path(__file__).resolve().parents[1]
import sys
if len(sys.argv) != 2: raise SystemExit('请传入 WebEditor/src/level-editor.html 的路径')
src=Path(sys.argv[1]).read_text()
src=re.sub(r'\s*html\s*\{\s*color-scheme:\s*dark\s*\}', '', src)
src=src.replace('width:40px;background:transparent;border:0;color:var(--fg);font:inherit;text-align:center;outline:none',
                'width:40px;border:0;font:inherit;text-align:center')
def svg(name):
    return f'<svg aria-hidden="true"><use href="/assets/icons.svg#{name}"></use></svg>'
prefix = f"""
<div class="app-shell">
<header class="topbar"><span class="brand-button">{svg('layers')}关卡编辑器</span><div class="topbar-actions"><button class="text-button" data-help type="button">{svg('book')}使用说明</button></div></header>
<aside class="sidebar" aria-label="主导航"><nav class="primary-nav"><button class="nav-item is-active" data-workspace type="button" aria-current="page" onclick="document.querySelector('[data-tplid]').focus()">{svg('workbench')}关卡编辑</button><button class="nav-item nav-settings" data-settings type="button">{svg('settings')}模板库路径</button></nav></aside>
<main class="main-content">
<div class="page-heading"><div><h1>关卡编辑</h1><p>从本地模板仓库读取或上传文件，编辑后另存为新模板。</p></div></div>
<section class="card work-start" aria-label="载入与保存模板">
<div class="work-row"><div class="field"><label for="tplid">模板 ID</label><input id="tplid" data-tplid list="tpllist" placeholder="例如 666" spellcheck="false" autocomplete="off"><datalist id="tpllist" data-tpllist></datalist></div>
<button class="primary-button" data-load type="button">读取模板</button><button class="secondary-button" data-open type="button">上传模板</button><button class="secondary-button" data-new type="button">新建空模板</button><input type="file" data-file hidden>
<button class="primary-button" data-export type="button">另存为新模板</button><button class="secondary-button" data-writerepo type="button" disabled>同步到模板仓库</button>
</div><p class="field-help" data-localstatus role="status" aria-live="polite">同步仅新增文件；保存后可一键同步。</p></section>
<div class="work-mode"><div class="segmented-control" role="group" aria-label="画布模式"><button data-mode-edit type="button" aria-pressed="true">编辑</button><button data-mode-play type="button" aria-pressed="false">试玩</button></div><span class="fn" data-fn>未载入模板</span></div>
<div class="leveldata-status" data-leveldatastatus role="status" aria-live="polite" hidden></div>
"""
i=src.index('<div class="bar">'); j=src.index('<div class="edit" data-editgrid>'); src=src[:i]+prefix+src[j:]
i=src.index('  <section class="half">'); j=src.index('    <div class="hbody">',i)
src=src[:i]+'''  <section class="half" data-editpanel aria-label="模板编辑">
    <div class="hh"><span class="ht">编辑画布</span><button class="chip" data-undo type="button">撤销</button><button class="chip" data-redo type="button">重做</button><button class="chip" data-restore type="button">恢复原模板</button><button class="chip" data-clear type="button">清空</button></div>
'''+src[j:]
i=src.index('      <div class="ph">分组'); j=src.index('      <div class="boardwrap">',i); src=src[:i]+src[j:]
src=src.replace('<section class="half playpanel">','<section class="half playpanel" hidden aria-label="试玩">')
src=src.replace('<div class="vdiv"></div>','').replace('data-theme="dark"','data-theme="light"').replace('关卡编辑器 · U1 + D3','Tile Family 关卡编辑器')
src=src.replace('</style>', '''</style><link rel="stylesheet" href="/assets/styles.css"><link rel="stylesheet" href="/assets/workbench.css"><script defer src="/assets/editor.js"></script>''')
src=src.replace('      <div class="boardwrap">','''      <div class="row edit-tools">
        <span class="hsub">落牌层</span><button class="chip" data-select-z="-1" type="button">−</button><button class="chip" data-select-z="1" type="button">＋</button>
        <button class="chip" data-reset-move type="button">平移复位</button><button class="chip" data-rotate="ccw" type="button">逆时针 90°</button><button class="chip" data-rotate="cw" type="button">顺时针 90°</button>
        <button class="chip" data-clear-layers="current" type="button">删本层</button><button class="chip" data-clear-layers="above" type="button">删上方层</button>
      </div>
      <div class="boardwrap">''',1)
src=src.replace('<canvas data-canvas="edit"></canvas>','<canvas data-canvas="edit" aria-label="模板编辑画布"></canvas>')
marker='      <div class="ph phrow">笔刷'
src=src.replace(marker,'''      <div class="row edit-tools"><button class="chip" data-locate-cover type="button">定位全遮盖</button><button class="chip" data-locate-float type="button">定位悬空</button><span class="field-help">红框：全遮盖 · 蓝框：悬空</span>
      <details class="coordinate"><summary>按坐标增删</summary><div class="row"><label>X <input class="in" data-coord-x type="number" value="2"></label><label>Y <input class="in" data-coord-y type="number" value="2"></label><label>Z <input class="in" data-coord-z type="number" value="0"></label><button class="chip" data-coordinate="add">加牌</button><button class="chip" data-coordinate="remove">删除</button></div></details></div>
'''+marker)
src=src.replace('      <p class="hint">左键摆','''      <details class="preview-setup"><summary>试玩参数 · 实验组与种子</summary>
      <div class="row rule-row"><label>规则 <select data-seed-mode><option value="unity">当前 Unity 分支 · DupPara 种子</option><option value="group">种子组规则 · 模板仓编辑器</option></select></label><label>付费保护减少 A 种类 <input class="in" data-iap-cut type="number" min="0" max="100" value="0"></label></div>
      <div class="row rule-row"><label>实验组生效 DupPara <input class="in" data-effective-dup placeholder="例如 6|123|100|4" spellcheck="false"></label><button class="chip" data-apply-ab type="button">应用到当前档</button></div>
      <p class="field-help">使用目标实验组实际生效的关卡、档位、类型池和参数。策略组、种子组只在对应规则下参与生成；本工具不自动获取玩家分组。</p>
      <div class="row"><button class="chip" data-importcsv type="button">导入 5 张试玩配置 CSV</button><input type="file" data-csvfiles accept=".csv,text/csv" multiple hidden>
      <input class="in" data-templatename hidden><input class="in" data-templateauthor hidden><button data-templatemetasave hidden>保存信息</button></div>
      </details>
      <p class="hint">左键摆''')
src=src.replace('<button class="chip ic" data-playmin title="最小化试玩">⊟</button>','<button class="chip" data-copy-board type="button">复制牌面与参数</button>')
src=src.replace('四个参数','四个参数').replace('右边试玩即时套这档重算','切到试玩后使用这一档参数')
src=src.replace('<canvas data-canvas="play"></canvas>','<canvas data-canvas="play" aria-label="关卡试玩画布"></canvas>')
src=src.replace('      <div class="pstate"','      <p class="field-help play-limit">此处生成开局牌面。未捕获的无种子随机、动态塔牌、道具与活动状态不承诺逐步复现真机。</p>\n      <div class="pstate"')
src=src.replace('整份 tile+塔 一起挪','整份 Tile 与塔一起移动')
src=re.sub(r'<span class="chip([^"]*)" (data-(?:raise|lower|move|snap|selmode|slotdec|slotinc|reroll|sim)\b[^>]*)>([^<]*)</span>',r'<button type="button" class="chip\1" \2>\3</button>',src)
src=src.replace('<div class="toast" data-toast></div>','''</main></div>
<div class="toast" data-toast role="status"></div>
<dialog data-settings-dialog aria-labelledby="settings-title">
  <div class="dialog-header"><h2 id="settings-title">模板库路径</h2></div>
  <form class="repository-form" data-repository-form>
    <label for="repository-path">模板仓库</label>
    <div class="repository-row">
      <input id="repository-path" type="text" placeholder="例如 ~/tileleveltemplate/Template" spellcheck="false" autocomplete="off" required>
      <button class="secondary-button" data-repository-pick type="button">选取目录</button>
    </div>
    <label for="output-path">输出模板</label>
    <div class="repository-row">
      <input id="output-path" type="text" placeholder="填写输出目录的完整路径" spellcheck="false" autocomplete="off" required>
      <button class="secondary-button" data-output-pick type="button" aria-label="选取输出模板目录">选取目录</button>
    </div>
    <p class="field-help" data-repository-status role="status"></p>
    <div class="repository-actions">
      <button class="secondary-button" data-dismiss type="button">关闭</button>
      <button class="primary-button" data-repository-save type="submit">保存路径</button>
    </div>
  </form>
</dialog>
<dialog data-help-dialog aria-labelledby="help-title"><div class="dialog-header"><h2 id="help-title">使用说明</h2></div><p>输入模板 ID 按 Enter，或上传 Unity 模板。左键摆牌，右键删牌，拖动连续涂；滚轮缩放，空格拖动画布。</p><p>编辑后直接在「模板 ID」栏改成新 ID，再点「另存为新模板」，然后点击「同步到模板仓库」。有重名时另选名称，本地输出会保留。牌数及 A/B 各自须为 3 的倍数。</p><p>试玩请填目标关卡和实验组生效参数。在「复制牌面与参数」中可核对每个 Tile 的坐标和图案。</p><button class="secondary-button" data-dismiss type="button" autofocus>关闭</button></dialog>''')
src=src.replace('<!--SCRIPT-->','<script type="module" src="/modules/level-editor.js"></script>')
(root/'前端/index.html').write_text(src)
# One editing canvas with the brush and DupPara controls within reach.
file=root/'前端/index.html'
src=file.read_text()
start=src.index('      <div class="boardwrap">')
brush=src.index('      <div class="ph phrow">笔刷',start)
end=src.index('      <p class="hint">左键摆',brush)
hint_end=src.index('</p>',end)+4
src=src[:start]+'<div class="canvas-workspace"><div class="canvas-column">'+src[start:brush]+'</div><aside class="editor-inspector" aria-label="笔刷与副本参数">'+src[brush:end]+'</aside></div>'+src[end:hint_end]+src[hint_end:]
src=src.replace('<input class="in" data-templatename hidden><input class="in" data-templateauthor hidden><button data-templatemetasave hidden>保存信息</button>','')
i=src.index('      <div class="row template-meta">'); j=src.index('      <div class="row showbar">',i)
meta=src[i:j]
src=src[:i]+src[j:]
i=src.index('      <details class="preview-setup">')
src=src[:i]+'<details class="preview-setup"><summary>模板备注</summary>'+meta+'</details>'+src[i:]
src=src.replace('<button data-mode-play type="button" aria-pressed="false">试玩</button>','</div><button class="primary-button play-action" data-mode-play type="button" aria-pressed="false">一键试玩</button><div class="mode-divider" hidden>')
src=src.replace('      <div class="ph">本关流程（整关共用，保存到关卡数据）</div>','      <div class="ph">关卡参数</div>')
src=src.replace('使用目标实验组实际生效的关卡、档位、类型池和参数。','使用目标实验组实际生效的关卡、档位、类型池和参数。')
src=src.replace('<div class="row rule-row"><label>实验组生效', '<div class="row rule-row"><label>导入实验组类型池 <input type="file" accept=".csv,text/csv" data-pool-file></label></div><div class="row rule-row"><label>实验组生效')
src=src.replace('顺时针 90°</button>\n        <button','顺时针 90°</button><button class="chip" type="button" data-fill-preview>预览随机配牌</button>\n        <button')
src=src.replace('>z 0</button>','>第 1 层</button>')
file.write_text(src)
src=file.read_text()
p=src.index('<section class="half playpanel"')
a=src.index('<div class="boardwrap">',p)
b=src.index('<p class="field-help play-limit">',a)
c=src.index('<div class="pstate"',b)
d=src.index('    </div>\n  </section>',c)
src=src[:a]+'<div class="canvas-workspace"><div class="canvas-column">'+src[a:b]+src[b:c]+'</div><aside class="editor-inspector play-inspector" aria-label="背包与道具">'+src[c:d]+'</aside></div>\n'+src[d:]
src=src.replace('<div class="mode-divider" hidden></div>','')
src=src.replace('仅英文字母和数字','仅英文字母和数字')
src=src.replace('>选择改 tile<','>批量换牌<').replace('>落牌层<',' data-drop-label>落牌层<')
src=src.replace('      <div class="cats" data-cats></div>','      <p class="field-help brush-counts" data-counts></p><div class="cats" data-cats></div>')
src=src.replace('>导入 5 张试玩配置 CSV<','>导入 5 张试玩配置 CSV<')
src=src.replace('牌数及 A/B 各自须为 3 的倍数。','每种 Tile 牌数须为 3 的倍数。')
file.write_text(src)
src=file.read_text()
src=src.replace('<link rel="stylesheet" href="/assets/workbench.css">','<link rel="stylesheet" href="/assets/editor.css"><link rel="stylesheet" href="/assets/workbench.css">')
i=src.index('<div class="boardwrap">')
j=src.index('</canvas>',i)+len('</canvas>')
src=src[:i]+'''<div class="canonical-board-frame"><div data-cover-board class="cc-board" tabindex="0" role="group" aria-label="模板编辑画布"></div>
<canvas data-canvas="edit" hidden aria-hidden="true"></canvas><div hidden><button data-zoom="out"></button><span data-zl></span><button data-zoom="in"></button><button data-fit></button></div>'''+src[j:]
src=src.replace('>撤销</button>',' title="Undo (Z)">撤销（Z）</button>',1).replace('>重做</button>',' title="Redo (Shift + Z)">重做（Shift+Z）</button>',1)
src=src.replace('>恢复原模板<','>恢复母版<').replace('>清空<','>清除所有<').replace('>预览随机配牌<','>预览配牌<').replace('>删本层<','>清除当前层<').replace('>删上方层<','>清除上方层<')
src=src.replace('>整体升层<','>升<').replace('>整体降层<','>降<').replace('>←<','>左<').replace('>→<','>右<').replace('>↑<','>上<').replace('>↓<','>下<')
src=src.replace('>全部</button>','>无限制</button>',1)
src=src.replace('<button class="on" data-showzlabel>第 1 层</button>','<input class="in layer-number" data-showzlabel inputmode="numeric" aria-label="显示到第几层" placeholder="无限制">')
src=src.replace('滚轮缩放 · 空格拖动平移','Alt + 方向键移光标，Enter 摆牌')
src=src.replace('拖动连续涂；滚轮缩放，空格拖动画布。','拖动连续涂；Alt + 方向键移光标，Enter 按当前笔刷操作。')
file.write_text(src)
src=file.read_text().replace('<dialog data-help-dialog','<dialog data-clear-layer-dialog aria-labelledby="clear-layer-title"><h2 id="clear-layer-title">清除当前层</h2><p data-clear-count></p><div class="row"><button class="secondary-button" type="button" data-dismiss>取消</button><button class="secondary-button" type="button" data-delete-choice="tiles">仅删除 Tile</button><button class="primary-button" type="button" data-delete-choice="layer">删除整层</button></div></dialog><dialog data-help-dialog')
file.write_text(src)

src = file.read_text()
for direction, label in [('ccw', '逆时针旋转 90°'), ('cw', '顺时针旋转 90°')]:
    src = re.sub(
        rf'<button class="chip" data-rotate="{direction}" type="button">[^<]*</button>',
        f'<button class="chip rotation-button" data-rotate="{direction}" type="button" aria-label="{label}" title="{label}">{svg("rotate-"+direction)}</button>',
        src
    )

start = src.index('<aside class="editor-inspector" aria-label="笔刷与副本参数">')
params = src.index('      <div class="ph">副本档位', start)
end = src.index('</aside></div>', params)
settings = src[params:end]
caption = '      <div class="ph">副本档位 · DupPara1–10（选哪次玩）</div>'
settings = settings.replace(caption, '', 1)
inspector = """
<aside class="unity-inspector" aria-label="编辑工具">
  <div class="unity-tool-heading">
    <h3>编辑</h3>
    <button type="button" class="chip" data-selmode aria-pressed="false">批量换牌</button>
  </div>
  <div class="unity-tool-stats">
    <p>Tile 总数 <strong data-tile-count>0</strong></p>
    <p>A 牌 <strong data-a-count>0</strong></p>
    <p>B 牌 <strong data-b-count>0</strong></p>
  </div>
  <p class="unity-selection" data-selhint hidden></p>
  <div data-brush class="unity-palette"></div>
</aside></div>
"""
controls = '<details class="editor-play-settings" data-play-settings><summary>试玩与副本参数</summary><div class="editor-play-parameters">'+settings+'</div></details>'
src = src[:start] + inspector + controls + src[end+len('</aside></div>'):]
src = re.sub(r'\s*<b>选择改 tile</b>[^。]+。', '', src)
file.write_text(src)
src = file.read_text()
src = re.sub(r'（快捷键 [A-Z]）', '', src)
file.write_text(src)

# Full-window workspace: file actions in the title bar, every editor control in the inspector.
src = file.read_text()
header = re.search(r'<header class="topbar">.*?</header>', src, re.S)
work_row = re.search(r'<div class="work-row">.*?</div>(?=<p class="field-help" data-localstatus)', src, re.S)
status = re.search(r'<p class="field-help" data-localstatus.*?</p>', src, re.S)
mode = re.search(r'<div class="work-mode">.*?</div>\s*(?=<div class="leveldata-status")', src, re.S)
play = re.search(r'<button class="primary-button play-action"[^>]*>一键试玩</button>', mode[0])[0]
file_label = re.search(r'<span class="fn" data-fn>[^<]*</span>', mode[0])[0]
actions = header[0].split('<div class="topbar-actions">', 1)[1].removesuffix('</div></header>')
title_bar = (
    f'<header class="topbar workspace-topbar"><h1 class="brand-button">{svg("layers")}关卡编辑器</h1>'
    + work_row[0] + play
    + '<div class="topbar-actions"><button class="icon-button" type="button" data-settings title="模板库路径" aria-label="模板库路径">'
    + svg('settings') + '</button>' + actions + '</div></header>'
)
src = src[:header.start()] + title_bar + '\n<main class="main-content workspace-main">\n' + src[mode.end():]
src = src.replace('<span class="ht">▶ 试玩</span><span class="hsub">点 tile 进背包 · 同类凑 3 消除</span>', '')
src = src.replace('<aside class="editor-inspector play-inspector" aria-label="背包与道具">', '<aside class="editor-inspector play-inspector" aria-label="背包与道具"><div data-status-home="play"></div>')
start = src.index('    <div class="hh"><span class="ht">编辑画布')
board_start = src.index('<div class="canvas-workspace">', start)
header_controls = re.search(r'<div class="hh"><span class="ht">编辑画布</span>(.*?)</div>', src[start:board_start], re.S)[1]
move_controls = re.search(r'<button type="button" class="chip" data-raise.*?</div>', src[start:board_start], re.S)[0].removesuffix('</div>').strip()
fine_controls = re.search(r'<button class="chip" data-reset-move.*?</div>', src[start:board_start], re.S)[0].removesuffix('</div>').strip()
src = src[:start] + '    <div class="hbody">\n' + src[board_start:]
diagnostics = re.search(r'      <div class="row edit-tools"><button class="chip" data-locate-cover.*?</details></div>', src, re.S)[0]
src = src.replace(diagnostics, '', 1)
side_head = f'''
  <div class="inspector-context">{file_label}<div data-status-home="edit">{status[0]}</div></div>
  <div class="inspector-controls">
    <fieldset class="maxz-control">
      <legend>层级显示</legend>
      <div class="maxz-field"><label for="maxz">MaxZ</label><input id="maxz" class="in layer-number" data-showzlabel inputmode="numeric" value="无限制" aria-describedby="maxz-help"></div>
      <div class="maxz-actions">
        <button type="button" class="chip" data-showz="up">+1</button>
        <button type="button" class="chip" data-showz="dn">-1</button>
        <button type="button" class="chip" data-maxz-reset>恢复</button>
      </div>
      <p id="maxz-help" class="visually-hidden">0 或无限制显示所有层。正整数显示到该层。</p>
    </fieldset>
    <div class="row inspector-action-row">{header_controls}</div>
    <div class="row inspector-action-row">{move_controls}</div>
    <div class="row inspector-action-row">{fine_controls}</div>
    {diagnostics}
  </div>
  <section class="inspector-info" aria-labelledby="template-stats-title">
    <h3 id="template-stats-title">模板信息</h3>
    <div class="template-stat-groups" data-template-stat-groups></div>
    <h4>每层 Tile 数 <span>不含塔</span></h4>
    <ul class="template-layer-counts" data-template-layer-counts></ul>
  </section>
'''
src = src.replace('<aside class="unity-inspector" aria-label="编辑工具">', '<aside class="unity-inspector" aria-label="编辑工具">' + side_head, 1)
src = re.sub(r'<div class="unity-tool-stats">.*?</div>', '', src, count=1, flags=re.S)
src = re.sub(r'</aside></div>\s*(<details class="editor-play-settings".*?</div></details>)\s*<p class="hint">.*?</p>',
             r'\1</aside></div>', src, count=1, flags=re.S)
# Keep secondary lists available at the bottom of the right pane.
dock = re.search(r'<div class="drawerdock" data-drawerdock>.*?(?=\n</main>)', src, re.S)[0].rstrip()
src = src.replace(dock, '', 1)
src = src.replace('</div></details></aside></div>', '</div></details>' + dock + '</aside></div>', 1)
file.write_text(src)

src = file.read_text()
src = src.replace('<button class="primary-button play-action" data-mode-play', '<div class="batch-toolbar"><button type="button" class="secondary-button" data-template-tool="data">导出模板数据</button><button type="button" class="secondary-button" data-template-tool="capture">批量截图</button><button type="button" class="secondary-button batch-entry" data-batch-open>批量修改模板</button></div><button class="primary-button play-action" data-mode-play', 1)
src = src.replace('<dialog data-settings-dialog', (root/'开发/模板批量工具.html').read_text() + '<dialog data-settings-dialog', 1)
src = src.replace('<div class="edit" data-editgrid>', (root/'开发/批量导入.html').read_text() + '<div class="edit" data-editgrid>', 1)
src = src.replace('<div class="canvas-column">      <div class="canonical-board-frame">', '<div class="canvas-column" data-editor-column><div class="batch-cards" data-batch-cards hidden></div><div class="canonical-board-frame" data-editor-frame>', 1)
src = src.replace('<div class="hh">\n      \n      <span class="sp">', '<div class="hh">\n      <div class="batch-play-navigation" data-batch-navigation hidden><button class="chip" type="button" data-batch-prev>上一关</button><strong data-batch-play-id></strong><button class="chip" type="button" data-batch-next>下一关</button></div>\n      <span class="sp">', 1)
if 'data-batch-navigation' not in src:
    src = src.replace('<section class="half playpanel" hidden aria-label="试玩">\n    <div class="hh">',
                      '<section class="half playpanel" hidden aria-label="试玩">\n    <div class="hh"><div class="batch-play-navigation" data-batch-navigation hidden><button class="chip" type="button" data-batch-prev>上一关</button><strong data-batch-play-id></strong><button class="chip" type="button" data-batch-next>下一关</button></div>', 1)
src = src.replace('通关推演 · 随机点击','通关推演').replace('<button type="button" class="chip" data-sim>跑 500 局</button>',
    '<label class="sim-trials">随机局数 <input class="in" type="number" min="1" max="500" value="100" data-sim-runs></label><button type="button" class="chip act" data-sim>运行推演</button>')
src = re.sub(r'<span class="hsub" data-simout[^>]*>—</span>\s*</div>', '</div><div data-simout class="sim-output" role="status" aria-live="polite"></div>', src)
src = re.sub(r'<dialog data-help-dialog.*?</dialog>', lambda _: (root/'开发/使用说明.html').read_text(), src, flags=re.S)
file.write_text(src)

src = file.read_text()
preview = re.search(r'<button[^>]+data-fill-preview[^>]*>[^<]*</button>', src)[0]
src = src.replace(preview, '', 1)
preview = preview.replace('预览配牌', '预览').replace('data-fill-preview', 'data-fill-preview aria-pressed="false"')
src = src.replace(f'<div class="inspector-context">{file_label}',
                  f'<div class="inspector-context"><div class="inspector-file-row">{file_label}{preview}</div>', 1)
preview_panel = """
      <div class="preview-seed-control" data-preview-options hidden>
        <label for="preview-seed">随机种子</label>
        <input class="in" id="preview-seed" data-preview-seed type="number" min="-2147483648" max="2147483647" step="1" aria-describedby="preview-seed-status">
        <p id="preview-seed-status" data-preview-seed-status aria-live="polite"></p>
      </div>
"""
src = src.replace('    <fieldset class="maxz-control">', preview_panel + '    <fieldset class="maxz-control">', 1)
file.write_text(src)
