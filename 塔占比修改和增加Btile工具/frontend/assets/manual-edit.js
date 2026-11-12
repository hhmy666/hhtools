(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory();
  else root.TowerManualEdit=factory();
})(typeof window!=='undefined'?window:this,function(){
  'use strict';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const overlap=(a,b)=>Math.max(0,2-Math.abs(a.x-b.x))*Math.max(0,2-Math.abs(a.y-b.y));
  function create(board){return {board:clone(board),translationOrigin:clone(board),deleted:[],additions:[],translation:{dx:0,dy:0},quarterTurns:0,history:[]};}
  function snapshot(draft){return {board:draft.board,deleted:draft.deleted,additions:draft.additions,translation:draft.translation,quarterTurns:draft.quarterTurns||0};}
  function translationBounds(board,origin=board){
    const b={min_dx:-32,max_dx:32,min_dy:-32,max_dy:32},w=board.width||17,h=board.height||19;
    const originals=new Map(origin.tiles.map(t=>[t.index,t]));
    const originalTowers=new Map((origin.towers||[]).map(t=>[t.index,t]));
    for(const t of [...board.tiles,...(board.towers||[])]){
      const initial=((board.towers||[]).includes(t)?originalTowers:originals).get(t.index)||{x:2,y:2};
      b.min_dx=Math.max(b.min_dx,Math.min(2,initial.x)-t.x,-t.x);
      b.max_dx=Math.min(b.max_dx,Math.max(14,initial.x)-t.x,w-2-t.x);
      b.min_dy=Math.max(b.min_dy,Math.min(2,initial.y)-t.y,-t.y);
      b.max_dy=Math.min(b.max_dy,Math.max(16,initial.y)-t.y,h-2-t.y);
    }
    for(const s of board.towers||[]){
      const n=board.tiles.filter(t=>t.x===s.x&&t.y===s.y).length,v=[[0,-1],[-1,0],[0,1],[1,0]][s.direction];
      if(!v)return {min_dx:0,max_dx:0,min_dy:0,max_dy:0};
      for(const [shift,height,pivot] of [[0,2,.5],[16/85,2*203.81146/181.77777,.554],[.2,2*1.8/1.48,.5]]){
        const ex=s.x+v[0]*shift*(n-1),ey=s.y+v[1]*shift*(n-1);
        b.min_dx=Math.max(b.min_dx,Math.ceil(-Math.min(s.x,ex)-1e-9));
        b.max_dx=Math.min(b.max_dx,Math.floor(w-Math.max(s.x,ex)-2+1e-9));
        b.min_dy=Math.max(b.min_dy,Math.ceil(-Math.min(s.y,ey)-1+height*(1-pivot)-1e-9));
        b.max_dy=Math.min(b.max_dy,Math.floor(h-Math.max(s.y,ey)-1-height*pivot+1e-9));
      }
    }
    return b;
  }
  function translate(draft,dx,dy){
    const offset=draft.translation||{dx:0,dy:0};
    if(dx===0&&dy===0)return {draft};
    if(![dx,dy,offset.dx+dx,offset.dy+dy].every(Number.isSafeInteger))return {error:'平移坐标需为有效整数。'};
    const next=clone(draft);
    for(const t of [...next.board.tiles,...(next.board.towers||[]),...next.additions]){t.x+=dx;t.y+=dy;}
    next.translation={dx:offset.dx+dx,dy:offset.dy+dy};
    next.validationRequested=false;
    next.history.push(snapshot(draft));
    return {draft:next};
  }
  function rotate(draft,direction){
    if(!['cw','ccw'].includes(direction))return {error:'请选择顺时针或逆时针旋转。'};
    const q=direction==='cw'?1:3,next=clone(draft);
    const turn=t=>{for(let i=0;i<q;i++){const x=t.x;t.x=17-t.y;t.y=x+1;}};
    for(const t of [...next.board.tiles,...(next.board.towers||[]),...next.additions])turn(t);
    for(const tower of next.board.towers||[])tower.direction=(tower.direction-q+4)%4;
    let {dx,dy}=draft.translation||{dx:0,dy:0};
    for(let i=0;i<q;i++){const x=dx;dx=-dy;dy=x;}
    next.translation={dx,dy};next.quarterTurns=((draft.quarterTurns||0)+q)%4;
    next.validationRequested=false;next.history.push(snapshot(draft));
    return {draft:next};
  }
  function clearLayer(draft,layer,mode){
    if(!Number.isSafeInteger(layer)||layer<1||!['layer','above'].includes(mode))return {error:'请先选择具体层，再删除本层或以上牌。'};
    const indices=draft.board.tiles.filter(t=>mode==='layer'?t.z===layer-1:t.z>layer-1).map(t=>t.index);
    if(!indices.length)return {draft,deletedCount:0};
    const next=clone(draft),removed=new Set(indices);
    next.board.tiles=next.board.tiles.filter(t=>!removed.has(t.index));
    next.additions=next.additions.filter(t=>!removed.has(t.index));
    next.deleted.push(...indices.filter(index=>index>=0));
    next.board.towers=next.board.towers.filter(s=>next.board.tiles.some(t=>t.x===s.x&&t.y===s.y));
    for(const s of next.board.towers)s.count=next.board.tiles.filter(t=>t.x===s.x&&t.y===s.y).length;
    for(const t of next.board.tiles){const supports=next.board.tiles.filter(p=>p.z===t.z-1&&overlap(t,p));t.support_indices=supports.map(p=>p.index);t.support_areas=supports.map(p=>overlap(t,p));}
    next.validationRequested=false;next.history.push(snapshot(draft));
    return {draft:next,deletedCount:indices.length};
  }
  function point(x,y,rect){return {x:Math.round((x-rect.left)/rect.width*18-1),y:Math.round((y-rect.top)/rect.height*20-1)};}
  function hit(board,x,y,z){return board.tiles.filter(t=>t.z===z&&x>=t.x&&x<t.x+2&&y>=t.y&&y<t.y+2).at(-1);}
  function visibleHit(board,x,y,ceiling){return board.tiles.filter(t=>t.z<=ceiling&&x>=t.x&&x<t.x+2&&y>=t.y&&y<t.y+2).sort((a,b)=>a.z-b.z||a.index-b.index).at(-1);}
  function placement(board,x,y,ceiling=Infinity,index=null){
    const target=board.tiles.find(t=>t.index===index&&t.z<=ceiling);
    if(target?.tower_index!=null){x=target.x;y=target.y;}
    // Unity Helper.GetNextPositionZToAddForPositionXY: highest occupied
    // cell in the complete 2×2 footprint + 1. Geometry is checked on save.
    const z=board.tiles.filter(t=>t.z<=ceiling&&overlap(t,{x,y})>0).reduce((z,t)=>Math.max(z,t.z+1),0);
    return {x,y,z};
  }
  function target(board,action,x,y,layer=0,index=null){
    const clicked=board.tiles.find(t=>t.index===index);
    if(action==='add')return layer>0?{x,y,z:layer-1}:placement(board,x,y,Infinity,index);
    if(layer>0)return clicked?.z===layer-1?clicked:hit(board,x,y,layer-1);
    return clicked||visibleHit(board,x,y,Infinity);
  }
  function inheritedPair(board,origin,lower,upper){
    if(lower.index<0||upper.index<0)return false;
    const a=origin.tiles.find(t=>t.index===lower.index),b=origin.tiles.find(t=>t.index===upper.index);
    return Boolean(a&&b&&(origin.geometry_warnings||[]).some(w=>w.code==='inherited_full_cover'&&w.lower_index===a.index&&w.upper_index===b.index&&w.lower_z===a.z&&w.upper_z===b.z&&w.x===a.x&&w.y===a.y)
      &&a.x===b.x&&a.y===b.y&&b.z===a.z+1&&lower.z===a.z&&upper.z===b.z&&lower.x-a.x===upper.x-b.x&&lower.y-a.y===upper.y-b.y);
  }
  function issues(board,origin=board,translation={dx:0,dy:0}){
    const tower=t=>(board.towers||[]).some(s=>s.x===t.x&&s.y===t.y);
    const result=[];
    for(const t of board.tiles){
      const add=(code,message)=>result.push({index:t.index,z:t.z,code,message:`第${t.z+1}层(${t.x}, ${t.y})${message}`});
      if(t.x<0||t.y<0||t.x+2>(board.width||17)||t.y+2>(board.height||19)||t.z<0||t.z>255||t.index<0&&(t.x<2||t.x>14||t.y<2||t.y>16))add('bounds','超出保存时允许的坐标范围。');
      if(board.tiles.some(p=>p.index!==t.index&&p.z===t.z&&overlap(p,t)))add('overlap','同层牌重叠。');
      if(tower(t))continue;
      if(t.z>0&&!board.tiles.some(p=>!tower(p)&&p.z===t.z-1&&overlap(t,p)))add('unsupported','的牌下方没有承接，不能悬空。');
      for(const p of board.tiles.filter(p=>p.z===t.z-1&&p.x===t.x&&p.y===t.y)){
        if(inheritedPair(board,origin,p,t)){
          const message=`原模板一压一：第${p.z+1}、${t.z+1}层(${t.x}, ${t.y})，已原样保留，可编辑修正；不影响保存导出。`;
          for(const tile of [p,t])result.push({index:tile.index,z:tile.z,code:'inherited_full_cover',blocking:false,message});
        }else add('full_cover','相邻两层一压一全遮盖，请错开半格。');
      }
    }
    return result;
  }
  function problem(board,{allowFloating=false,origin=board,translation={dx:0,dy:0}}={}){
    const tower=t=>board.towers.some(s=>s.x===t.x&&s.y===t.y);
    for(const t of board.tiles){
      if(t.x<0||t.y<0||t.x+2>(board.width||17)||t.y+2>(board.height||19)||t.z<0||t.z>255||t.index<0&&(t.x<2||t.x>14||t.y<2||t.y>16))return `第${t.z+1}层(${t.x}, ${t.y})超出保存时允许的坐标范围。`;
      if(board.tiles.some(p=>p.index!==t.index&&p.z===t.z&&overlap(p,t)))return '同层不能重叠，请换一个位置。';
      if(tower(t))continue;
      if(!allowFloating&&t.z>0&&!board.tiles.some(p=>!tower(p)&&p.z===t.z-1&&overlap(t,p)))return `第${t.z+1}层(${t.x}, ${t.y})的牌下方没有承接，不能悬空。`;
      if(board.tiles.some(p=>p.z===t.z-1&&p.x===t.x&&p.y===t.y&&!inheritedPair(board,origin,p,t)))return '相邻两层不能一压一全遮盖，请错开半格。';
    }
    for(const s of board.towers){
      const levels=board.tiles.filter(t=>t.x===s.x&&t.y===s.y).map(t=>t.z).sort((a,b)=>a-b);
      if(levels.some((z,i)=>z!==i))return '只能增删塔顶，不能让塔的中间断层。';
      const vector=[[0,-1],[-1,0],[0,1],[1,0]][s.direction];
      if(!vector)return '塔方向不合法。';
      for(const [shift,height,pivot] of [[0,2,.5],[16/85,2*203.81146/181.77777,.554],[.2,2*1.8/1.48,.5]]){
        const ex=s.x+vector[0]*shift*(levels.length-1),ey=s.y+vector[1]*shift*(levels.length-1);
        if(Math.min(s.x,ex)<-1e-9||Math.max(s.x,ex)+2>(board.width||17)+1e-9||Math.min(s.y,ey)+1-height*(1-pivot)<-1e-9||Math.max(s.y,ey)+1+height*pivot>(board.height||19)+1e-9)return '这摞塔继续展开会越过画板边界，不能新增。';
      }
    }
    return '';
  }
  function apply(draft,op){
    const next=clone(draft);next.history=draft.history.slice();
    if(op.action==='add'){
      if(![op.x,op.y,op.z].every(Number.isSafeInteger)||![-10,-11].includes(op.tile_id))return {error:'请输入有效整数坐标，并选择A/B笔刷。'};
      const tile={index:-1-Math.max(0,...draft.additions.map(t=>-t.index)),x:op.x,y:op.y,z:op.z,tile_id:op.tile_id,added:true,changed:true,tower_index:next.board.towers.find(t=>t.x===op.x&&t.y===op.y)?.index??null};
      next.additions.push(tile);next.board.tiles.push(tile);
    }else if(op.action==='delete'){
      const tile=next.board.tiles.find(t=>t.index===op.index);
      if(!tile)return {error:'点击处没有可见牌。'};
      next.board.tiles=next.board.tiles.filter(t=>t.index!==tile.index);
      if(tile.index<0)next.additions=next.additions.filter(t=>t.index!==tile.index);else next.deleted.push(tile.index);
      next.board.towers=next.board.towers.filter(s=>next.board.tiles.some(t=>t.x===s.x&&t.y===s.y));
    }else return {error:'未知微调操作。'};
    for(const s of next.board.towers)s.count=next.board.tiles.filter(t=>t.x===s.x&&t.y===s.y).length;
    next.validationRequested=false;
    // Refresh actual support details so hover never reuses stale evidence.
    for(const t of next.board.tiles){const supports=next.board.tiles.filter(p=>p.z===t.z-1&&overlap(t,p));t.support_indices=supports.map(p=>p.index);t.support_areas=supports.map(p=>overlap(t,p));}
    next.history.push(snapshot(draft));
    return {draft:next};
  }
  function undo(draft){if(!draft.history.length)return draft;return {...draft,...draft.history.at(-1),history:draft.history.slice(0,-1),validationRequested:false};}
  function payload(draft){return {deleted_indices:draft.deleted,additions:draft.additions.map(({x,y,z,tile_id})=>({x,y,z,tile_id})),...(draft.quarterTurns?{transform:{quarter_turns:draft.quarterTurns,...draft.translation}}:draft.translation&&(draft.translation.dx||draft.translation.dy)?{translation:draft.translation}:{})};}
  function counts(draft){const tiles=draft.board.tiles;return {total:tiles.length,A:tiles.filter(t=>t.tile_id===-10).length,B:tiles.filter(t=>t.tile_id===-11).length};}
  function saveProblem(draft){const c=counts(draft),offset=draft.translation||{dx:0,dy:0};return problem(draft.board,{origin:draft.translationOrigin,translation:offset})||(Math.abs(offset.dx)>32||Math.abs(offset.dy)>32?'保存时整体偏移需在-32至32格范围内。':'')||(!c.total||c.total%3||c.A%3||c.B%3?'保存时总牌量及A/B各自需为3的倍数，请继续增删或撤销。':'');}
  return {create,point,hit,visibleHit,placement,target,apply,undo,payload,counts,saveProblem,issues,translationBounds,translate,rotate,clearLayer};
});
