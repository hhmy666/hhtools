(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TowerOrdinaryAdd = api;
})(typeof window !== 'undefined' ? window : globalThis, function() {
  'use strict';
  const clone = value => structuredClone(value);
  const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
  function defaults() {
    return {tile_type:'B', added_tiles:18, placement_strategy:'bottom_first',
      layer_mode:'auto', min_added_layers:0, max_added_layers:3,
      target_mode:'count',min_total_tiles:90,max_total_tiles:99,split_mode:'auto',top_tiles:9,bottom_tiles:9};
  }
  function validate(options) {
    const o = {...defaults(), ...options};
    if (!['A','B'].includes(o.tile_type)) return {field:'tile_type',message:'请选择新增 A 或 B。'};
    if (o.target_mode==='count' && (!Number.isInteger(o.added_tiles) || o.added_tiles <= 0 || o.added_tiles > 2147483647 || o.added_tiles % 3))
      return {field:'added_tiles',message:'新增张数须为正整数，且为 3 的倍数，例如 18、21。'};
    if (!['count','total_range'].includes(o.target_mode)) return {field:'target_mode',message:'请选择数量目标。'};
    if (o.target_mode==='total_range' && (!Number.isInteger(o.min_total_tiles) || o.min_total_tiles<=0 || o.min_total_tiles>2147483647))
      return {field:'min_total_tiles',message:'目标总数下限须为正整数。'};
    if (o.target_mode==='total_range' && (!Number.isInteger(o.max_total_tiles) || o.max_total_tiles<o.min_total_tiles || o.max_total_tiles>2147483647))
      return {field:'max_total_tiles',message:'目标总数上限须为整数且不小于下限。'};
    if (!['top_first','bottom_first','both'].includes(o.placement_strategy)) return {field:'placement_strategy',message:'请选择最上层、最下层或上下同时补入。'};
    if (!['auto','custom'].includes(o.split_mode)) return {field:'split_mode',message:'请选择上下分配方式。'};
    if (o.placement_strategy==='both' && o.split_mode==='custom') {
      for (const field of ['top_tiles','bottom_tiles']) if (!Number.isInteger(o[field]) || o[field]<=0 || o[field]>2147483647)
        return {field,message:'上下各自张数须为正整数。'};
      const total=o.top_tiles+o.bottom_tiles;
      if (total%3 || total>2147483647 || o.target_mode==='count' && total!==o.added_tiles)
        return {field:'bottom_tiles',message:o.target_mode==='count'?'上下合计须等于新增张数，且为 3 的倍数。':'上下合计须为 3 的倍数，最终总数还需落在目标范围内。'};
    }
    if (!['auto','exact','range'].includes(o.layer_mode)) return {field:'layer_mode',message:'请选择自动、固定值或范围。'};
    for (const field of ['min_added_layers','max_added_layers'])
      if (!Number.isInteger(o[field]) || o[field] < 0 || o[field] > 2147483647)
        return {field,message:'新增层数须为非负整数。'};
    if (o.layer_mode !== 'auto' && o.min_added_layers > o.max_added_layers)
      return {field:'min_added_layers',message:'最少新增层数不能大于最多新增层数。'};
    if (o.layer_mode === 'exact' && o.min_added_layers !== o.max_added_layers)
      return {field:'max_added_layers',message:'固定层数的上下限须相同。'};
    return null;
  }
  function summary(options) {
    const o = {...defaults(),...options};
    const layers = o.layer_mode === 'auto' ? '层数自动' : o.layer_mode === 'exact' ? `新增 ${o.max_added_layers} 层` : `新增 ${o.min_added_layers}–${o.max_added_layers} 层`;
    const quantity=o.target_mode==='total_range'?`总 Tile ${o.min_total_tiles}–${o.max_total_tiles} 张（补 ${o.tile_type}）`:`新增 ${o.tile_type} ${o.added_tiles} 张`;
    const side=o.placement_strategy==='both'?(o.split_mode==='custom'?`上 ${o.top_tiles} / 下 ${o.bottom_tiles} 张`:'上下自动分配'):o.placement_strategy==='top_first'?'最上层':'最下层';
    return `非塔区域 · ${quantity} · ${side} · ${layers}`;
  }
  function fieldsMarkup(prefix, options) {
    const o = {...defaults(),...options};
    const select = (key,label,items) => `<div class="field"><label for="${prefix}-${key}">${label}</label><select id="${prefix}-${key}" data-ordinary-field="${key}">${items.map(([value,text])=>`<option value="${value}"${o[key]===value?' selected':''}>${text}</option>`).join('')}</select></div>`;
    const number = (key,label,extra='') => `<div class="field${key==='added_tiles'?' span-two':''}" ${extra}><label for="${prefix}-${key}">${label}</label><input id="${prefix}-${key}" data-ordinary-field="${key}" type="number" value="${esc(o[key])}" min="${key==='added_tiles'?3:0}" max="2147483647" step="${key==='added_tiles'?3:1}"></div>`;
    return `<div class="form-grid ordinary-add-fields">
      ${select('tile_type','新增 Tile 类型',[['B','新增 B'],['A','新增 A']])}
      ${select('target_mode','数量目标',[['count','新增张数'],['total_range','目标总 Tile 数范围']])}
      ${number('added_tiles','新增张数','data-ordinary-target="count"')}
      ${number('min_total_tiles','目标总数下限','data-ordinary-target="total_range"')}
      ${number('max_total_tiles','目标总数上限','data-ordinary-target="total_range"')}
      ${select('placement_strategy','补入位置',[['bottom_first','非塔区域最下层'],['top_first','非塔区域最上层'],['both','非塔区域上下同时补入']])}
      ${select('layer_mode','新增层数',[['auto','自动'],['exact','固定值'],['range','范围']])}
      <div class="span-two" data-ordinary-split="both">${select('split_mode','上下分配',[['auto','自动分配'],['custom','自定义上下张数']])}</div>
      ${number('top_tiles','上方新增张数','data-ordinary-split="custom"')}
      ${number('bottom_tiles','下方新增张数','data-ordinary-split="custom"')}
      ${number('min_added_layers','最少新增层数','data-ordinary-layer="range"')}
      ${number('max_added_layers',o.layer_mode==='range'?'最多新增层数':'固定新增层数','data-ordinary-layer="bounded"')}
      <p class="field-help span-two">总数范围包含两端；上下张数合计为 3 的倍数。新增层数按上下合计，可在下方连续加多层。</p>
    </div>`;
  }
  function mountEditor(element, {prefix='ordinary',options,onChange=()=>{},onPreview=null}={}) {
    let current = {...defaults(),...clone(options || {})};
    element.innerHTML = fieldsMarkup(prefix,current) + (onPreview ? '<button class="secondary-button increase-preview-button" type="button" data-ordinary-preview>预览新增效果</button>' : '');
    const conditional = () => {
      element.querySelectorAll('[data-ordinary-target]').forEach(node=>{node.hidden=node.dataset.ordinaryTarget!==current.target_mode;});
      element.querySelectorAll('[data-ordinary-split]').forEach(node=>{node.hidden=current.placement_strategy!=='both' || node.dataset.ordinarySplit==='custom' && current.split_mode!=='custom';});
      element.querySelectorAll('[data-ordinary-layer]').forEach(node => {
        node.hidden = node.dataset.ordinaryLayer === 'range' ? current.layer_mode !== 'range' : current.layer_mode === 'auto';
      });
      const label = element.querySelector(`label[for="${prefix}-max_added_layers"]`);
      if (label) label.textContent = current.layer_mode === 'range' ? '最多新增层数' : '固定新增层数';
    };
    conditional();
    element.oninput = element.onchange = event => {
      const input = event.target, key = input.dataset.ordinaryField;
      if (!key) return;
      current[key] = ['tile_type','placement_strategy','layer_mode','target_mode','split_mode'].includes(key) ? input.value
        : input.validity?.badInput || input.value === '' ? NaN : Number(input.value);
      if (current.layer_mode === 'exact') current.min_added_layers = current.max_added_layers;
      if (key === 'layer_mode' && current.layer_mode === 'auto') {
        current.min_added_layers = 0;current.max_added_layers = 3;
      }
      for (const field of ['min_added_layers','max_added_layers']) {
        const control = element.querySelector(`[data-ordinary-field="${field}"]`);
        if (control) control.value = Number.isFinite(current[field]) ? String(current[field]) : '';
      }
      conditional();onChange(clone(current));
    };
    element.onclick = event => {if (event.target.closest('[data-ordinary-preview]')) onPreview?.(clone(current));};
    return {value:()=>clone(current)};
  }
  return {defaults,validate,summary,fieldsMarkup,mountEditor};
});
