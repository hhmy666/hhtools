(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.CoverView=factory();})(typeof window!=='undefined'?window:this,function(){
 'use strict';
 const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
 function parseIds(text){const ids=[...new Set(text.split(/[\s,，;；]+/).filter(Boolean).map(x=>x.replace(/_m\.bytes$/i,'')))];if(ids.some(x=>!x||x==='.'||x==='..'||/[\\/\x00-\x1f]/.test(x)))throw Error('模板 ID 不能包含路径或控制字符。');return ids;}
 function filtered(rows,query='',status='all'){const q=query.trim().toLowerCase();return rows.filter(r=>(status==='all'||r.status===status)&&(!q||[r.name,r.template_id,r.error].some(v=>String(v||'').toLowerCase().includes(q))));}
 function pageRows(rows,page=1,size=20){const pages=Math.max(1,Math.ceil(rows.length/size)),p=Math.min(pages,Math.max(1,page));return {rows:rows.slice((p-1)*size,p*size),page:p,pages,start:rows.length?(p-1)*size+1:0,end:Math.min(p*size,rows.length)};}
 function progress(task){return task?.status==='succeeded'?100:Math.min(99,Math.max(0,Math.floor((task?.completed||0)/Math.max(1,task?.total||1)*100)));}
 function describe(i){return `${i.kind==='adjacent'?'邻层一压一':'悬空全遮盖'} · (${i.x}, ${i.y}) · 第${i.lower_layer}、${i.upper_layer}层`;}
 return {esc,parseIds,filtered,pageRows,progress,describe};
});
