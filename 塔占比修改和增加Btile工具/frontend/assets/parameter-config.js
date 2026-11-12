(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TowerReducerParameters = api;
})(typeof window === "object" ? window : globalThis, function () {
  "use strict";
  const MAX_FILE_BYTES = 32 * 1024 * 1024;
  const writable = row => row.status === "ready" || row.status === "unchanged";
  const rowIdentity = row => JSON.stringify([row.original_level_id, row.original_template_id, row.new_template_id, row.original_parameters, row.level_indexes]);
  const esc = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const parameters = values => Array.from({length: 10}, (_, i) => values?.[i] ?? "—");
  const statusLabels = {ready: "已调整", unchanged: "参数不变", no_tower: "无塔", failed: "失败"};
  function parseTemplateInput(text) {
    const template_ids = [], source_map = {};
    const normalize = value => value.trim().replace(/_m\.bytes$/i, "");
    for (const part of String(text || "").split(/[\s,，;；]+/).filter(Boolean)) {
      const pair = part.split("=");
      if (pair.length > 2 || (pair.length === 2 && pair.some(value => !value.trim()))) throw new Error("关联格式应为 原ID=新ID；每行一项。");
      const id = normalize(pair.at(-1));
      if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error(`模板 ID 格式不正确：${id}`);
      if (!template_ids.includes(id)) template_ids.push(id);
      if (pair.length === 2) {
        const original = normalize(pair[0]);
        if (!/^[A-Za-z0-9_-]+$/.test(original)) throw new Error(`原模板 ID 格式不正确：${original}`);
        const previous = source_map[id];
        if (!previous) source_map[id] = original;
        else if (previous !== original) source_map[id] = [...new Set([...(Array.isArray(previous) ? previous : [previous]), original])];
      }
    }
    return {template_ids, source_map};
  }
  function parseFileAssociations(text) {
    const parsed = parseTemplateInput(text);
    if (parsed.template_ids.some(id => !parsed.source_map[id])) throw new Error("原、新模板关联请每行填写 原ID=新ID；新ID使用文件内部模板 ID。");
    return parsed.source_map;
  }
  function filterRows(rows, query) {
    const needle = String(query || "").trim().toLocaleLowerCase("zh-CN");
    if (!needle) return rows || [];
    return (rows || []).filter(row => [row.level_indexes?.join(" "), row.original_level_id, row.original_template_id, row.new_template_id,
      row.new_level_id, row.description, statusLabels[row.status], row.message, ...(row.original_parameters || []), ...(row.new_parameters || [])]
      .join(" ").toLocaleLowerCase("zh-CN").includes(needle));
  }
  function pageRows(rows, page = 1, size = 20) {
    const total = rows.length, pages = Math.max(1, Math.ceil(total / size));
    page = Math.min(pages, Math.max(1, page));
    return {rows: rows.slice((page - 1) * size, page * size), page, pages, total, start: total ? (page - 1) * size + 1 : 0, end: Math.min(page * size, total)};
  }
  function changedRows(rows) {
    return (rows || []).filter(row => writable(row) && row.original_parameters?.length === 10 && row.new_parameters?.length === 10 && parameters(row.original_parameters).some((value, i) => String(value) !== String(parameters(row.new_parameters)[i])));
  }
  function parameterCells(values, changedAgainst) {
    return parameters(values).map((value, i) => `<td class="parameter-value${changedAgainst && String(value) !== String(parameters(changedAgainst)[i]) ? " is-changed" : ""}">${esc(value)}</td>`).join("");
  }
  function renderMappingRows(rows, selected) {
    return rows.map(row => {
      const detail = row.message || "";
      const delta = row.delta && (Number(row.delta.A) || Number(row.delta.B)) ? `A ${Number(row.delta.A) >= 0 ? "+" : ""}${row.delta.A || 0} · B ${Number(row.delta.B) >= 0 ? "+" : ""}${row.delta.B || 0}` : "";
      const tone = row.status === "failed" ? "danger" : row.status === "no_tower" ? "neutral" : "success";
      return `<tr><td class="parameter-select-cell"><input type="checkbox" data-parameter-row="${esc(row.row_id)}" aria-label="选择原关卡 ${esc(row.original_level_id)}" ${selected.has(row.row_id) ? "checked" : ""} ${writable(row) ? "" : "disabled"}></td><td>${esc(row.level_indexes?.join("、") || "—")}</td><td class="parameter-id">${esc(row.original_level_id)}</td><td class="parameter-id">${esc(row.original_template_id)}</td><td class="parameter-id">${esc(row.new_template_id)}</td>${parameterCells(row.new_parameters)}<td class="parameter-status-cell"><span class="status-pill ${tone}"><svg aria-hidden="true"><use href="/assets/icons.svg#${tone === "danger" ? "warning" : "status"}"></use></svg>${esc(statusLabels[row.status] || "失败")}</span>${delta ? `<small>${esc(delta)}</small>` : ""}${detail ? `<details><summary>查看明细</summary><p>${esc(detail)}</p></details>` : ""}</td></tr>`;
    }).join("");
  }
  function renderComparisonRows(rows) {
    return changedRows(rows).map(row => `<tr><td rowspan="2">${esc(row.level_indexes?.join("、") || "—")}</td><td rowspan="2" class="parameter-id">${esc(row.original_level_id)}</td><td rowspan="2" class="parameter-id">${esc(row.original_template_id)}</td><td rowspan="2" class="parameter-id">${esc(row.new_template_id)}</td><th scope="row">旧参数</th>${parameterCells(row.original_parameters)}</tr><tr class="parameter-new-row"><th scope="row">新参数</th>${parameterCells(row.new_parameters, row.original_parameters)}</tr>`).join("");
  }
  function renderSourceCandidates(entries, choices = {}, pending = false) {
    return (entries || []).map((entry, index) => `<fieldset class="parameter-match-choice"><legend>${esc(entry.new_template_id)}：选择原模板（可多选）</legend>${(entry.source_candidates || []).map((candidate, i) => {
      const id = candidate.original_template_id;
      const selected = choices[entry.new_template_id];
      return `<label class="checkbox-label"><input type="checkbox" id="parameter-match-${index}-${i}" data-parameter-source-choice="${esc(entry.new_template_id)}" value="${esc(id)}" aria-label="将 ${esc(entry.new_template_id)} 关联到 ${esc(id)}" ${[].concat(selected || []).includes(id) ? 'checked' : ''} ${pending ? 'disabled' : ''}>${esc(candidate.label || id)}</label>`;
    }).join('')}</fieldset>`).join('');
  }
  function overallProgress(state) {
    const task = state.task;
    if (task?.status === 'succeeded') return 1;
    if (!task) {
      const upload = state.uploadProgress;
      return upload?.total > 0 ? Math.min(.99, Math.max(0, upload.completed / upload.total)) / 5 : 0;
    }
    const overall = task.overall_progress;
    let fraction;
    if (overall?.total > 0 && Number.isFinite(overall.completed)) fraction = overall.completed / overall.total;
    else {
      // Old durable tasks have only per-stage counts. Never show a completed
      // sub-stage as a completed batch while recovering those records.
      const action = state.taskTicket?.action || state.pending;
      const phases = action === 'preview' ? ['verifying','previewing'] : action === 'commit' ? ['verifying','committing'] : ['indexing','resolving','verifying','calculating'];
      const position = Math.max(0, phases.indexOf(task.phase));
      fraction = (position + (task.total > 0 ? Math.min(1, task.completed / task.total) : 0)) / phases.length;
    }
    if (state.taskTicket?.request?.payload?.source?.mode === 'files') {
      const stages = overall?.stage_count || 4;
      fraction = (1 + Math.max(0, fraction) * stages) / (stages + 1);
    }
    return Math.max(0, Math.min(.99, fraction));
  }
  function createWorkflow({request, upload, onChange = () => {}, background = false, pollDelay = 400, storage = null} = {}) {
    const initial = () => ({mode: "ids", ids: "", source: "output", files: [], fileAssociations: "", jobId: "", context: null, selection: null, result: null,
      selected: new Set(), query: "", page: 1, comparisonPage: 1, size: 20, levelStart: "", levelEnd: "", rule: "lessT{原关卡ID}", rulePrefix: "lessT", ruleEdited: false,
      unifyDescription: false, description: "", plan: null, committed: null, commitFailure: null, pending: "", error: "", progress: "",
      pendingContext: null, contextChangePending: false, contextNotice: "", task: null, taskTicket: null,
      stripPrefix: '', stripSuffix: '', sourceChoices: {}, matchCandidates: [], preserveLevelPrefix: false, uploadProgress: null});
    const state = initial(); let epoch = 0, controller = null, contextEpoch = 0, contextController = null;
    const storageKey = 'tower-parameter-task-v1';
    const persist = () => { try { if (state.taskTicket) storage?.setItem(storageKey, JSON.stringify({ticket: state.taskTicket,
      selection: state.selection, result: state.result, plan: state.plan,
      draft: {mode: state.mode, ids: state.ids, source: state.source, jobId: state.jobId, fileAssociations: state.fileAssociations,
        stripPrefix: state.stripPrefix, stripSuffix: state.stripSuffix, sourceChoices: state.sourceChoices, preserveLevelPrefix: state.preserveLevelPrefix,
        levelStart: state.levelStart, levelEnd: state.levelEnd, rule: state.rule, rulePrefix: state.rulePrefix, ruleEdited: state.ruleEdited, unifyDescription: state.unifyDescription, description: state.description}}));
      else storage?.removeItem(storageKey); } catch (_) {} };
    try { const saved = JSON.parse(storage?.getItem(storageKey) || 'null'); if (saved?.ticket?.request?.request_id) {
      Object.assign(state, saved.draft); state.taskTicket = saved.ticket; state.selection = saved.selection || null;
      state.result = saved.result || null; state.plan = saved.plan || null;
      state.matchCandidates = (state.selection?.entries || []).filter(entry => entry.source_candidates?.length);
      state.selected = new Set((state.result?.rows || []).filter(writable).map(row => row.row_id));
    } } catch (_) {}
    const forgetTask = () => { state.taskTicket = null; state.task = null; persist(); };
    const writeLocked = () => state.pending === 'commit' || (background && state.taskTicket?.action === 'commit' && state.task?.status !== 'succeeded');
    const notify = () => onChange(state);
    const stop = () => { epoch++; controller?.abort(); controller = null; state.pending = ""; state.progress = ""; };
    const invalidatePlan = () => { state.plan = null; state.committed = null; state.commitFailure = null; };
    const invalidateResult = () => { state.result = null; state.selected.clear(); state.page = 1; state.comparisonPage = 1; invalidatePlan(); };
    function settleDeferredContext() {
      if (!state.pendingContext && !state.contextChangePending) return;
      if (state.contextChangePending) {
        state.selection = null; state.plan = null;
        state.contextNotice = "配置或输出路径已变更，本次写入结果已保留。新的操作请重新确认模板。";
      }
      state.context = state.pendingContext;
      state.pendingContext = null; state.contextChangePending = false;
    }
    async function operation(name, work, accept) {
      if (state.pending) return null;
      const token = ++epoch; controller = new AbortController(); const current = controller;
      state.pending = name; state.error = ""; notify();
      let timedOut = false;
      const timer = background ? null : setTimeout(() => { timedOut = true; current.abort(); }, 60000);
      try {
        const result = await work(current.signal);
        if (token !== epoch || current.signal.aborted) return null;
        accept(result); return result;
      } catch (error) {
        if (token === epoch) state.error = timedOut ? "本地服务响应超时。请重试；写入操作先核对配置结果。" : error.name === "AbortError" ? "已停止等待；后台处理记录保留，可恢复查看。" : error.message;
        return null;
      } finally {
        clearTimeout(timer);
        if (token === epoch) {
          controller = null; state.pending = ""; state.progress = "";
          if (name === "commit") settleDeferredContext();
          persist(); notify();
        }
      }
    }
    async function taskRequest(url, options) {
      const local = new AbortController(); const abort = () => local.abort();
      options.signal?.addEventListener('abort', abort, {once: true});
      if (options.signal?.aborted) local.abort();
      const timer = setTimeout(abort, 15000);
      try { return await request(url, {...options, signal: local.signal}); }
      finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
    }
    async function runTask(action, payload, signal) {
      let ticket = state.taskTicket;
      if (!ticket || ticket.action !== action) {
        ticket = {action, request: {request_id: globalThis.crypto?.randomUUID?.() || `parameter-${Date.now()}-${Math.random().toString(36).slice(2)}`, action, payload}, taskId: null};
        state.taskTicket = ticket; persist();
      }
      let failures = 0;
      for (;;) {
        if (signal.aborted) throw Object.assign(new Error('已停止等待'), {name: 'AbortError'});
        let task;
        try {
          task = await taskRequest(ticket.taskId ? `/api/parameters/tasks/${encodeURIComponent(ticket.taskId)}` : '/api/parameters/start',
            ticket.taskId ? {signal} : {method: 'POST', json: ticket.request, signal});
          if (signal.aborted) throw Object.assign(new Error('已停止等待'), {name: 'AbortError'});
          failures = 0;
        } catch (error) {
          if (signal.aborted) throw error;
          if ((error.status && error.status < 500 && error.status !== 429) || ++failures > 3) throw new Error(`${error.message}；处理记录已保留，请点击“恢复处理结果”。`);
          state.progress = '连接暂时中断，正在恢复处理状态…'; notify();
          await new Promise(resolve => setTimeout(resolve, Math.min(2000, pollDelay * failures)));
          continue;
        }
        if (!task?.task_id || !['running', 'succeeded', 'failed'].includes(task.status)) throw new Error('服务未返回有效处理记录，请重新启动工具服务后重试。');
        ticket.taskId = task.task_id; persist(); state.task = task;
        const labels = {preparing: '准备处理', indexing: '读取生成记录', resolving: '解析与关联模板', verifying: '核验模板', calculating: '计算关卡参数', previewing: '生成配置差异', committing: '追加配置', completed: '处理完成'};
        state.progress = `${labels[task.phase] || '正在处理'}${task.total > 0 ? ` · ${task.completed} / ${task.total}` : ''}${task.current_filename ? ` · ${task.current_filename}` : ''}`;
        if (task.selection) {
          state.selection = task.selection;
          state.matchCandidates = (task.selection.entries || []).filter(entry => entry.source_candidates?.length);
          const entries = (task.selection.entries || []).filter(row => row.status !== 'failed');
          if (!state.ruleEdited && entries.length) state.rule = entries.every(row => row.template_mode === 'ordinary_add') ? 'moreB{原关卡ID}'
            : entries.every(row => row.operation === 'increase') ? 'moreT{原关卡ID}' : 'lessT{原关卡ID}';
        }
        notify();
        if (task.status === 'failed') { forgetTask(); throw new Error(task.error?.message || '处理失败，请重新计算。'); }
        if (task.status === 'succeeded') return task.result;
        await new Promise(resolve => setTimeout(resolve, pollDelay));
      }
    }
    async function sourcePayload(signal) {
      const affixes = {...(state.stripPrefix.trim() ? {strip_prefix: state.stripPrefix.trim()} : {}), ...(state.stripSuffix.trim() ? {strip_suffix: state.stripSuffix.trim()} : {})};
      if (state.mode === 'ids') {
        const parsed = parseTemplateInput(state.ids);
        if (!parsed.template_ids.length) throw new Error('请先输入新模板 ID。');
        return {mode: 'ids', source: state.source, ...parsed, ...affixes, source_map: {...state.sourceChoices, ...parsed.source_map}};
      }
      if (state.mode === 'job') {
        if (!state.jobId) throw new Error('请选择一个已生成任务。');
        return {mode: 'job', job_id: state.jobId};
      }
      if (!state.files.length) throw new Error('请先选择新模板文件。');
      const sourceMap = {...state.sourceChoices, ...parseFileAssociations(state.fileAssociations)};
      state.uploadProgress = null;
      const imported = await api.upload({files: state.files, signal, onProgress: (message, counts) => { state.progress = message; state.uploadProgress = counts || null; notify(); }});
      if (signal.aborted) throw Object.assign(new Error('已取消'), {name: 'AbortError'});
      return {mode: 'files', import_id: imported.import_id, ...affixes, ...(Object.keys(sourceMap).length ? {source_map: sourceMap} : {})};
    }
    const api = {
      state, upload,
      input(patch) { if (writeLocked()) return; stop(); forgetTask(); Object.assign(state, patch); state.uploadProgress = null; state.sourceChoices = {}; state.matchCandidates = []; state.selection = null; state.error = ""; state.contextNotice = ""; invalidateResult(); notify(); },
      chooseSource(newId, originalIds) {
        if (writeLocked()) return false;
        const entry = state.matchCandidates.find(entry => entry.new_template_id === newId);
        const ids = [].concat(originalIds || []).filter(Boolean);
        if (!entry || ids.some(id => !entry.source_candidates.some(candidate => candidate.original_template_id === id))) return false;
        stop(); forgetTask();
        if (ids.length) state.sourceChoices[newId] = ids.length === 1 ? ids[0] : [...new Set(ids)];
        else delete state.sourceChoices[newId];
        state.selection = null; state.error = ''; invalidateResult(); notify(); return true;
      },
      range(patch) { if (writeLocked()) return; stop(); forgetTask(); Object.assign(state, patch); state.error = ""; invalidateResult(); notify(); },
      delivery(patch) { if (writeLocked()) return; stop(); forgetTask(); Object.assign(state, patch); if (Object.prototype.hasOwnProperty.call(patch, "rule")) state.ruleEdited = true; state.error = ""; invalidatePlan(); notify(); },
      search(query) { state.query = query; state.page = 1; state.comparisonPage = 1; notify(); },
      page(value, comparison = false) { state[comparison ? "comparisonPage" : "page"] = value; notify(); },
      size(value) { state.size = [20, 50, 100].includes(Number(value)) ? Number(value) : 20; state.page = 1; state.comparisonPage = 1; notify(); },
      toggle(id, checked) {
        if (writeLocked() || !(state.result?.rows || []).some(row => row.row_id === id && writable(row))) return;
        checked ? state.selected.add(id) : state.selected.delete(id); stop(); forgetTask(); invalidatePlan(); notify();
      },
      selectPage(rows, checked) {
        if (writeLocked()) return;
        rows.filter(writable).forEach(row => checked ? state.selected.add(row.row_id) : state.selected.delete(row.row_id)); stop(); forgetTask(); invalidatePlan(); notify();
      },
      selectAll(checked) { api.selectPage(filterRows(state.result?.rows || [], state.query), checked); },
      cancel() { if (writeLocked()) return; stop(); state.error = background ? '已停止等待，后台继续处理；可恢复查看。' : "操作已取消，可重新执行。"; notify(); },
      reset() { if (writeLocked()) return; stop(); forgetTask(); contextEpoch++; contextController?.abort(); Object.assign(state, initial()); notify(); },
      writeLocked,
      async resume() {
        if (!background || !state.taskTicket || state.pending) return null;
        const action = state.taskTicket.action;
        return operation(action, signal => runTask(action, state.taskTicket.request.payload, signal), result => {
          if (action === 'calculate') { state.result = result; state.selected = new Set((result.rows || []).filter(writable).map(row => row.row_id)); invalidatePlan(); }
          else if (action === 'preview') state.plan = result;
          else if (action === 'commit') {
            if (result.status !== 'committed') {
              state.commitFailure = result;
              throw new Error(`${result.message || '写入未完成，请核对差异后重试。'}${result.written ? ' 配置已发生写入，请先复查结果与备份。' : ''}${result.backup_path ? ` 备份：${result.backup_path}` : ''}`);
            }
            state.committed = result; state.plan = null;
          }
        });
      },
      contextChanged() {
        contextEpoch++; contextController?.abort(); contextController = null;
        if (writeLocked()) {
          state.pendingContext = null; state.contextChangePending = true; notify(); return;
        }
        stop(); forgetTask(); state.context = null; state.selection = null; invalidateResult(); notify();
      },
      async loadContext(preferredJob = "") {
        const token = ++contextEpoch; contextController?.abort(); contextController = new AbortController();
        try {
          const context = await request("/api/parameters/context", {signal: contextController.signal});
          if (token !== contextEpoch) return;
          const changed = !state.context || state.context.config_path !== context.config_path || state.context.config_root !== context.config_root || state.context.output_root !== context.output_root;
          if (writeLocked()) {
            state.pendingContext = context; state.contextChangePending = state.contextChangePending || changed; notify(); return;
          }
          if (state.context && changed) {
            stop(); forgetTask(); state.selection = null; invalidateResult();
          }
          state.context = context;
          if (!state.jobId && preferredJob && (context.jobs || []).some(job => job.job_id === preferredJob)) state.jobId = preferredJob;
          notify();
        } catch (error) { if (token === contextEpoch && error.name !== "AbortError") { state.error = `读取配置路径失败：${error.message}`; notify(); } }
      },
      async resolve() {
        if (background) return operation('resolve', async signal => runTask('resolve', await sourcePayload(signal), signal), result => { state.selection = result; invalidateResult(); });
        return operation("resolve", async signal => {
          let body;
          const affixes = {...(state.stripPrefix.trim() ? {strip_prefix: state.stripPrefix.trim()} : {}), ...(state.stripSuffix.trim() ? {strip_suffix: state.stripSuffix.trim()} : {})};
          if (state.mode === 'ids') {
            const parsed = parseTemplateInput(state.ids);
            if (!parsed.template_ids.length) throw new Error('请先输入新模板 ID。');
            body = {mode:'ids', source:state.source, ...parsed, ...affixes, source_map:{...state.sourceChoices,...parsed.source_map}};
          } else if (state.mode === 'job') {
            if (!state.jobId) throw new Error('请选择一个已生成任务。');
            body = {mode:'job',job_id:state.jobId};
          } else {
            if (!state.files.length) throw new Error('请先选择新模板文件。');
            const sourceMap = {...state.sourceChoices,...parseFileAssociations(state.fileAssociations)};
            const imported = await api.upload({files:state.files,signal,onProgress:message=>{state.progress=message;notify();}});
            if (signal.aborted) throw Object.assign(new Error('已取消'),{name:'AbortError'});
            body = {mode:'files',import_id:imported.import_id,...affixes,...(Object.keys(sourceMap).length?{source_map:sourceMap}:{})};
          }
          return request("/api/parameters/resolve", {method: "POST", json: body, signal});
        }, result => {
          state.selection = result; state.contextNotice = "";
          state.matchCandidates = (result.entries || []).filter(entry => entry.source_candidates?.length);
          const entries = (result.entries || []).filter(row => row.status !== "failed");
          if (!state.ruleEdited && entries.length) state.rule = entries.every(row => row.template_mode === "ordinary_add") ? "moreB{原关卡ID}"
            : entries.every(row => row.operation === "increase") ? "moreT{原关卡ID}" : "lessT{原关卡ID}";
          invalidateResult();
        });
      },
      async calculate() {
        if (writeLocked()) return api.resume();
        if (background) {
          if (state.taskTicket?.action === 'calculate' && !state.result) return api.resume();
          return operation('calculate', async signal => {
            const range = [state.levelStart, state.levelEnd].map(value => value === '' ? null : Number(value));
            if (range.some(value => value !== null && (!Number.isInteger(value) || value < 1)) || (range.every(value => value !== null) && range[0] > range[1])) throw new Error('关卡范围应为正整数，起始关卡不能大于结束关卡。');
            forgetTask();
            const source = state.selection?.selection_id ? {selection_id: state.selection.selection_id} : {source: await sourcePayload(signal)};
            return runTask('calculate', {...source, level_start: range[0], level_end: range[1],
              ...(state.ruleEdited || state.selection?.selection_id ? {level_id_rule: state.rule} : {}), ...(state.preserveLevelPrefix ? {preserve_level_prefix: true} : {}), description: state.unifyDescription ? state.description : null}, signal);
          }, result => { state.result = result; state.selected = new Set((result.rows || []).filter(writable).map(row => row.row_id)); state.page = 1; state.comparisonPage = 1; invalidatePlan(); });
        }
        if (!state.selection?.selection_id) { state.error = "请先确认新模板。"; notify(); return null; }
        return operation("calculate", signal => {
          const range = [state.levelStart, state.levelEnd].map(value => value === "" ? null : Number(value));
          if (range.some(value => value !== null && (!Number.isInteger(value) || value < 1)) || (range.every(value => value !== null) && range[0] > range[1])) throw new Error("关卡范围应为正整数，起始关卡不能大于结束关卡。");
          return request("/api/parameters/calculate", {method: "POST", signal, json: {selection_id: state.selection.selection_id,
            level_start: range[0], level_end: range[1], level_id_rule: state.rule, ...(state.preserveLevelPrefix ? {preserve_level_prefix: true} : {}), description: state.unifyDescription ? state.description : null}});
        }, result => { state.result = result; state.selected = new Set((result.rows || []).filter(writable).map(row => row.row_id)); state.page = 1; state.comparisonPage = 1; invalidatePlan(); });
      },
      async exportResult() {
        if (writeLocked()) return null;
        if (!state.result?.calculation_id || !state.selection?.selection_id) return null;
        const selected = new Set((state.result.rows || []).filter(row => state.selected.has(row.row_id)).map(rowIdentity));
        const payload = {
          selection_id: state.selection.selection_id, level_start: state.levelStart === "" ? null : Number(state.levelStart),
          level_end: state.levelEnd === "" ? null : Number(state.levelEnd), level_id_rule: state.rule, ...(state.preserveLevelPrefix ? {preserve_level_prefix: true} : {}),
          description: state.unifyDescription ? state.description : null};
        if (background) forgetTask();
        return operation("download", signal => background ? runTask('calculate', payload, signal) : request("/api/parameters/calculate", {method: "POST", signal, json: payload}), result => {
            state.result = result; state.selected = new Set((result.rows || []).filter(row => writable(row) && selected.has(rowIdentity(row))).map(row => row.row_id)); invalidatePlan();
          });
      },
      async preview() {
        if (writeLocked()) return null;
        if (!state.selection?.selection_id || !state.result?.calculation_id || !state.selected.size) { state.error = "请先确认模板、计算并选择可写入的关卡。"; notify(); return null; }
        const payload = {calculation_id: state.result.calculation_id, selected_row_ids: [...state.selected], level_id_rule: state.rule, preserve_level_prefix: state.preserveLevelPrefix, description: state.unifyDescription ? state.description : null};
        if (background && state.taskTicket?.action !== 'preview') forgetTask();
        return operation("preview", signal => background ? runTask('preview', payload, signal) : request("/api/parameters/preview", {method: "POST", signal, json: payload}), result => { state.plan = result; });
      },
      async commit() {
        if (!state.plan?.plan_id) { state.error = "预览已失效，请重新预览写入。"; notify(); return null; }
        if (state.plan.status === "blocked" || state.plan.status === "error") { state.error = "预览存在冲突，修正后请重新预览。"; notify(); return null; }
        if (background && state.taskTicket?.action !== 'commit') forgetTask();
        return operation("commit", signal => background ? runTask('commit', {plan_id: state.plan.plan_id}, signal) : request("/api/parameters/commit", {method: "POST", json: {plan_id: state.plan.plan_id}, signal}), result => {
          if (result.status !== "committed") {
            state.commitFailure = result;
            throw new Error(`${result.message || "写入未完成，请核对差异后重试。"}${result.written ? " 配置已发生写入，请先复查结果与备份。" : ""}${result.backup_path ? ` 备份：${result.backup_path}` : ""}`);
          }
          state.committed = result; state.plan = null;
        });
      },
    };
    return api;
  }
  function mount({document, request, batchImport, openDialog, closeDialog, setDialogPending, showDeliveryCompletion,
    setInvalid = () => {}, clearInvalid = () => {}, focusFirstInvalid = () => false, currentJob = () => ""} = {}) {
    const el = id => document.getElementById(id), all = selector => [...document.querySelectorAll(selector)];
    if (!el("parameters-view")) return null;
    let uploadSession = null, uploadedFiles = null;
    let sessionStorage = null; try { sessionStorage = globalThis.sessionStorage; } catch (_) {}
    const workflow = createWorkflow({request, background: true, storage: sessionStorage, onChange: render, upload: async ({files, signal, onProgress}) => {
      if (uploadedFiles !== files) { uploadSession = batchImport.createSession({items: files, mode: "files", limits: {file_count: 20, request_bytes: 128 * 1024 * 1024, file_bytes: MAX_FILE_BYTES}}); uploadedFiles = files; }
      return batchImport.run(uploadSession, {request: (url, options) => request(url === '/api/import' ? '/api/parameters/upload' : url, options), signal, onProgress: progress => onProgress(progress.phase === "merging" ? "正在合并已上传模板…" : `上传模板 · ${progress.completed} / ${progress.total} 个`, progress)});
    }});
    function feedback(message, error = false) {
      const box = el("parameter-feedback"); box.hidden = !message; box.classList.toggle("is-error", error); box.querySelector("span").textContent = message || "";
    }
    function render() {
      const s = workflow.state, pending = Boolean(s.pending), writePending = workflow.writeLocked();
      for (const [id, key] of [['parameter-template-ids','ids'], ['parameter-file-associations','fileAssociations'], ['parameter-level-start','levelStart'], ['parameter-level-end','levelEnd'], ['parameter-source','source'], ['parameter-description','description']]) {
        if (el(id).value !== String(s[key])) el(id).value = s[key];
      }
      el('parameter-unify-description').checked = s.unifyDescription;
      const templateRule = s.rule.endsWith('{新模板ID}');
      el('parameter-level-id-rule').value = templateRule ? 'template' : 'prefix';
      const suffix = templateRule ? '{新模板ID}' : '{原关卡ID}';
      const prefix = s.rule.endsWith(suffix) ? s.rule.slice(0, -suffix.length) : s.rulePrefix;
      if (el('parameter-level-id-prefix').value !== prefix) el('parameter-level-id-prefix').value = prefix;
      el('parameter-level-id-prefix-field').hidden = false;
      el("parameters-view").setAttribute("aria-busy", String(pending));
      all("[data-parameter-mode]").forEach(button => { const selected = button.dataset.parameterMode === s.mode; button.classList.toggle("is-selected", selected); button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1; button.disabled = writePending; });
      all("[data-parameter-panel]").forEach(panel => { panel.hidden = panel.dataset.parameterPanel !== s.mode; });
      const jobs = s.context?.jobs || [];
      const jobSelect = el("parameter-job");
      const jobOptions = '<option value="">请选择已生成任务</option>' + jobs.map(job => `<option value="${esc(job.job_id)}">${esc(job.label || job.job_id)}</option>`).join("");
      if (jobSelect.innerHTML !== jobOptions) jobSelect.innerHTML = jobOptions;
      jobSelect.value = s.jobId;
      if (el('parameter-source-status')) el('parameter-source-status').hidden = s.mode === 'files';
      el("parameter-source-label").textContent = s.mode === "job" ? "源模板与生成产物只读" : "模板数据源只读";
      const showSourcePath = s.mode === "ids" && s.source === "output";
      el("parameter-source-path").hidden = !showSourcePath;
      el("parameter-source-path").textContent = showSourcePath ? s.context?.output_root || "等待读取输出目录" : "";
      el("parameter-config-path").textContent = s.context?.config_path || s.context?.config_root || "请在设置中填写配置仓库 / CSV 目录";
      el("parameter-config-status").textContent = s.context?.config_ready ? "配置已就绪" : "配置未就绪";
      el("parameter-config-status").className = `status-pill ${s.context?.config_ready ? "success" : "warning"}`;
      el("parameter-files-list").innerHTML = s.files.length ? s.files.map((file, i) => `<li><span class="file-name">${esc(file.name)}</span><span class="file-meta">${(file.size / 1024 / 1024).toFixed(2)} MB</span><button class="remove-file" type="button" data-remove-parameter-file="${i}" aria-label="移除 ${esc(file.name)}" ${pending ? "disabled" : ""}>移除</button></li>`).join("") : '<li class="empty-row">尚未选择模板。</li>';
      el("parameter-resolve-button").disabled = pending || (s.mode === "ids" ? !s.ids.trim() : s.mode === "files" ? !s.files.length : !s.jobId);
      el("parameter-resolve-button").setAttribute("aria-busy", String(s.pending === "resolve"));
      el("parameter-resolve-button").textContent = s.pending === "resolve" ? "正在确认模板…" : "确认新模板";
      const hasSource = s.mode === 'ids' ? Boolean(s.ids.trim()) : s.mode === 'files' ? Boolean(s.files.length) : Boolean(s.jobId);
      for (const id of ['parameter-calculate-button','parameter-calculate-side-button']) {
        if (!el(id)) continue;
        el(id).disabled = pending || writePending || (!hasSource && !s.taskTicket) || !s.context?.config_ready;
        el(id).setAttribute('aria-busy', String(s.pending === 'calculate'));
        el(id).textContent = s.pending === 'calculate' ? '正在计算参数…' : s.taskTicket && !s.result ? '恢复处理结果' : '计算新参数';
      }
      el("parameter-resolve-button").hidden = true;
      el("parameter-cancel-button").textContent = '停止等待';
      el("parameter-cancel-button").hidden = !pending || s.pending === "commit";
      if (el('parameter-recover-button')) el('parameter-recover-button').hidden = pending || !s.taskTicket || s.task?.status === 'succeeded';
      if (el('parameter-task-progress')) {
        el('parameter-task-progress').hidden = !pending && !s.taskTicket;
        const overall = overallProgress(s);
        el('parameter-task-stage').textContent = `${s.progress || (s.task?.status === 'succeeded' ? '处理完成' : '处理记录已保留，可恢复查看。')} · 总进度 ${Math.floor(overall * 100)}%`;
        const meter = el('parameter-task-meter');
        if (s.task?.status === 'succeeded') { meter.max = 1; meter.value = 1; }
        else { meter.max = 10000; meter.value = Math.floor(overall * 10000); }
      }
      const entries = s.selection?.entries || [];
      el("parameter-selection-summary").hidden = !s.selection;
      el("parameter-selection-summary").textContent = s.selection ? `已确认 ${entries.length} 个模板${entries.filter(entry => entry.status === "failed").length ? ` · ${entries.filter(entry => entry.status === "failed").length} 个读取失败` : ""}` : "";
      el("parameter-source-errors").hidden = !entries.some(entry => entry.status === "failed");
      el("parameter-source-errors").innerHTML = entries.filter(entry => entry.status === "failed").map(entry => `<li>${esc(entry.new_template_id || entry.original_template_id)}：${esc(entry.message || "读取失败")}</li>`).join("");
      if (el('parameter-matching-options')) {
        el('parameter-matching-options').hidden = s.mode === 'job';
        for (const [id, key] of [['parameter-strip-prefix', 'stripPrefix'], ['parameter-strip-suffix', 'stripSuffix']]) { el(id).disabled = pending || writePending; if (el(id).value !== s[key]) el(id).value = s[key]; }
      }
      if (el('parameter-match-panel')) {
        el('parameter-match-panel').hidden = !s.matchCandidates.length;
        const container = el('parameter-match-choices'), html = renderSourceCandidates(s.matchCandidates, s.sourceChoices, pending || writePending);
        if (container.innerHTML !== html) container.innerHTML = html;
        el('parameter-match-recalculate').disabled = pending || writePending || !Object.keys(s.sourceChoices).length;
      }
      if (el('parameter-preserve-level-prefix')) {
        el('parameter-preserve-level-prefix').checked = s.preserveLevelPrefix;
        el('parameter-preserve-level-prefix').disabled = pending || writePending;
        el('parameter-level-id-rule').disabled = pending || writePending || s.preserveLevelPrefix;
        el('parameter-level-id-prefix').disabled = pending || writePending || s.preserveLevelPrefix;
      }
      if (s.mode === "files" && entries.some(entry => entry.status === "failed" && /关联|原ID|原模板/.test(entry.message || ""))) el("parameter-file-associations-details").open = true;
      feedback(s.error || (s.pending ? '' : s.contextNotice), Boolean(s.error));
      el("parameter-results").hidden = !s.result;
      if (!s.result) return;
      const rows = s.result.rows || [], filtered = filterRows(rows, s.query), page = pageRows(filtered, s.page, s.size);
      s.page = page.page;
      el("parameter-result-summary").textContent = `共 ${rows.length} 行 · ${rows.filter(row => row.status === "ready").length} 行已调整 · ${rows.filter(row => row.status === "unchanged").length} 行参数不变 · ${rows.filter(row => row.status === "failed").length} 行失败`;
      el("parameter-table-body").innerHTML = renderMappingRows(page.rows, s.selected) || `<tr><td colspan="16" class="table-empty">${s.query ? "没有匹配结果，请更换关键词或清空搜索。" : "所选模板没有匹配的原关卡配置。"}</td></tr>`;
      const selectablePage = page.rows.filter(writable), selectedPage = selectablePage.filter(row => s.selected.has(row.row_id));
      el("parameter-select-page").checked = selectablePage.length > 0 && selectedPage.length === selectablePage.length;
      el("parameter-select-page").indeterminate = selectedPage.length > 0 && selectedPage.length < selectablePage.length;
      el("parameter-select-page").disabled = !selectablePage.length || s.pending === "commit";
      el("parameter-selection-count").textContent = `已选择 ${s.selected.size} / ${rows.filter(writable).length} 行（跨页）`;
      el("parameter-select-all").textContent = s.query ? `选择全部 ${filtered.filter(writable).length} 行搜索结果` : `选择全部 ${rows.filter(writable).length} 行可用结果`;
      el("parameter-page-status").textContent = `${page.start}–${page.end} / ${page.total} 行 · 第 ${page.page} / ${page.pages} 页`;
      el("parameter-prev-page").disabled = page.page <= 1; el("parameter-next-page").disabled = page.page >= page.pages;
      el("parameter-clear-search").hidden = !s.query;
      el("parameter-description").disabled = !s.unifyDescription || s.pending === "commit";
      const comparison = pageRows(changedRows(filtered), s.comparisonPage, s.size); s.comparisonPage = comparison.page;
      el("parameter-comparison-body").innerHTML = renderComparisonRows(comparison.rows) || '<tr><td colspan="15" class="table-empty">当前结果没有副本参数变化。</td></tr>';
      el("parameter-comparison-count").textContent = `${comparison.total} 行参数有变化`;
      el("parameter-comparison-page-status").textContent = `${comparison.start}–${comparison.end} / ${comparison.total} 行`;
      el("parameter-comparison-prev").disabled = comparison.page <= 1; el("parameter-comparison-next").disabled = comparison.page >= comparison.pages;
      el("parameter-download-button").disabled = pending || writePending || !rows.length || !s.selection?.selection_id;
      el("parameter-preview-button").disabled = pending || writePending || !s.selected.size || !s.context?.config_ready || !s.selection?.selection_id;
      el("parameter-preview-button").setAttribute("aria-busy", String(s.pending === "preview"));
      el("parameter-preview-button").textContent = s.pending === "preview" ? "正在预览…" : "一键添加至 config_level.csv";
      el("parameter-delivery-status").textContent = s.committed ? s.committed.written ? "已追加配置" : "无需追加" : s.plan ? "已预览" : "尚未写入";
      el("parameter-write-result").hidden = !s.committed;
      if (s.committed) el("parameter-write-result").textContent = `${s.committed.written ? "配置已追加到" : "配置已核对，无需追加："} ${s.committed.config_path || s.result.config_path || "config_level.csv"}${s.committed.backup_path ? `；备份：${s.committed.backup_path}` : ""}`;
    }
    const input = (name, key, event = "input") => el(name).addEventListener(event, () => workflow.input({[key]: el(name).value}));
    input("parameter-template-ids", "ids"); input("parameter-file-associations", "fileAssociations"); input("parameter-source", "source", "change"); input("parameter-job", "jobId", "change");
    for (const [id, key] of [['parameter-strip-prefix','stripPrefix'],['parameter-strip-suffix','stripSuffix']]) if (el(id)) input(id, key);
    el('parameter-preserve-level-prefix')?.addEventListener('change', () => workflow.delivery({preserveLevelPrefix: el('parameter-preserve-level-prefix').checked}));
    el('parameter-match-choices')?.addEventListener('change', event => {
      const target = event.target, newId = target.dataset?.parameterSourceChoice;
      if (!newId) return;
      const values = Array.from(el('parameter-match-choices').querySelectorAll('input[data-parameter-source-choice]')).filter(input => input.dataset.parameterSourceChoice === newId && input.checked).map(input => input.value);
      workflow.chooseSource(newId, values); el(target.id)?.focus({preventScroll:true});
    });
    el('parameter-match-recalculate')?.addEventListener('click', () => workflow.calculate());
    ["parameter-level-start", "parameter-level-end"].forEach((id, i) => el(id).addEventListener("input", () => workflow.range({[i ? "levelEnd" : "levelStart"]: el(id).value})));
    function submitCalculate(event) {
      event.preventDefault();
      if (workflow.state.pending || workflow.writeLocked()) return;
      const form = el('parameter-source-form'); clearInvalid(form);
      if (workflow.state.mode === "ids") {
        try { parseTemplateInput(workflow.state.ids); }
        catch (error) { setInvalid(el("parameter-template-ids"), error.message); focusFirstInvalid(form); return; }
      }
      if (workflow.state.mode === "files") {
        try { parseFileAssociations(workflow.state.fileAssociations); }
        catch (error) { el("parameter-file-associations-details").open = true; setInvalid(el("parameter-file-associations"), error.message); focusFirstInvalid(form); return; }
      }
      const start = workflow.state.levelStart, end = workflow.state.levelEnd;
      [start, end].forEach((value, i) => { if (value !== "" && (!Number.isInteger(Number(value)) || Number(value) < 1)) setInvalid(el(i ? "parameter-level-end" : "parameter-level-start"), "请填写正整数关卡号，或留空表示不限。"); });
      if (start !== "" && end !== "" && Number(start) > Number(end)) setInvalid(el("parameter-level-end"), "结束关卡不能小于起始关卡。");
      if (!focusFirstInvalid(form)) workflow.calculate();
    }
    el('parameter-source-form').addEventListener('submit', submitCalculate);
    for (const id of ['parameter-calculate-button','parameter-calculate-side-button']) el(id)?.addEventListener('click', submitCalculate);
    el("parameter-cancel-button").addEventListener("click", () => workflow.cancel());
    el('parameter-recover-button')?.addEventListener('click', () => workflow.resume());
    el("parameter-refresh-context").addEventListener("click", () => workflow.loadContext(currentJob()));
    all("[data-parameter-mode]").forEach(button => {
      button.addEventListener("click", () => { if (button.dataset.parameterMode !== workflow.state.mode) workflow.input({mode: button.dataset.parameterMode}); });
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); const buttons = all("[data-parameter-mode]"); let next = buttons.indexOf(button);
        next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (next + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next].click(); buttons[next].focus();
      });
    });
    function addFiles(files) {
      const selected = [...workflow.state.files];
      for (const file of files) if (!selected.some(existing => existing.name === file.name && existing.size === file.size && existing.lastModified === file.lastModified)) selected.push(file);
      workflow.input({files: selected});
    }
    el("parameter-template-files").addEventListener("change", event => { addFiles([...event.target.files]); event.target.value = ""; });
    el("parameter-files-list").addEventListener("click", event => { const button = event.target.closest("[data-remove-parameter-file]"); if (button) workflow.input({files: workflow.state.files.filter((_, i) => i !== Number(button.dataset.removeParameterFile))}); });
    const drop = el("parameter-drop-zone");
    ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove("is-dragging"); if (name === "drop") addFiles([...event.dataTransfer.files]); }));
    el("parameter-search").addEventListener("input", event => { if (!event.isComposing) workflow.search(event.target.value); });
    el("parameter-search").addEventListener("compositionend", event => workflow.search(event.target.value));
    el("parameter-clear-search").addEventListener("click", () => { el("parameter-search").value = ""; workflow.search(""); el("parameter-search").focus(); });
    el("parameter-page-size").addEventListener("change", event => { workflow.size(event.target.value); el("parameter-table-body").closest(".table-scroll").scrollTop = 0; el("parameter-comparison-body").closest(".table-scroll").scrollTop = 0; });
    el("parameter-table-body").addEventListener("change", event => { const checkbox = event.target.closest("[data-parameter-row]"); if (checkbox) workflow.toggle(checkbox.dataset.parameterRow, checkbox.checked); });
    el("parameter-select-page").addEventListener("change", event => workflow.selectPage(pageRows(filterRows(workflow.state.result?.rows || [], workflow.state.query), workflow.state.page, workflow.state.size).rows, event.target.checked));
    el("parameter-select-all").addEventListener("click", () => workflow.selectAll(true));
    el("parameter-clear-selection").addEventListener("click", () => { workflow.selectPage(workflow.state.result?.rows || [], false); });
    function changePage(delta, comparison = false) {
      workflow.page(workflow.state[comparison ? "comparisonPage" : "page"] + delta, comparison);
      el(comparison ? "parameter-comparison-body" : "parameter-table-body").closest(".table-scroll").scrollTop = 0;
    }
    el("parameter-prev-page").addEventListener("click", () => changePage(-1));
    el("parameter-next-page").addEventListener("click", () => changePage(1));
    el("parameter-comparison-prev").addEventListener("click", () => changePage(-1, true));
    el("parameter-comparison-next").addEventListener("click", () => changePage(1, true));
    el('parameter-level-id-rule').addEventListener('change', event => {
      const rulePrefix = el('parameter-level-id-prefix').value;
      workflow.delivery({rulePrefix, rule: rulePrefix + (event.target.value === 'template' ? '{新模板ID}' : '{原关卡ID}')});
    });
    el('parameter-level-id-prefix').addEventListener('input', event => workflow.delivery({rulePrefix: event.target.value, rule: event.target.value + (el('parameter-level-id-rule').value === 'template' ? '{新模板ID}' : '{原关卡ID}')}));
    el("parameter-unify-description").addEventListener("change", event => workflow.delivery({unifyDescription: event.target.checked}));
    el("parameter-description").addEventListener("input", event => workflow.delivery({description: event.target.value}));
    el("parameter-download-button").addEventListener("click", async () => {
      const result = await workflow.exportResult(); if (!result?.calculation_id) return;
      window.location.assign(`/api/parameters/${encodeURIComponent(result.calculation_id)}/download?format=xlsx`);
    });
    el("parameter-preview-button").addEventListener("click", async () => {
      const view = el("parameters-view"); clearInvalid(view);
      if (!workflow.state.preserveLevelPrefix && !workflow.state.rule.trim()) { setInvalid(el("parameter-level-id-rule"), "请填写新关卡 ID 规则。"); focusFirstInvalid(view); return; }
      const plan = await workflow.preview(); if (!plan?.plan_id) return;
      el("parameter-preview-path").textContent = plan.config_path || workflow.state.result.config_path || "config_level.csv";
      const planRows = plan.rows || [], appendCount = plan.summary?.append_count ?? planRows.filter(row => row.status === "append").length;
      const skipped = plan.summary?.skip_count ?? planRows.filter(row => row.status === "skip").length, blocked = plan.summary?.blocked_count ?? planRows.filter(row => row.status === "blocked").length;
      el("parameter-preview-summary").textContent = `将追加 ${appendCount} 行 · 跳过 ${skipped} 行 · 冲突 ${blocked} 行。原配置保留，config_level_list.csv 不变。`;
      el("parameter-preview-body").innerHTML = planRows.map(row => `<tr><td>${esc(row.original_level_id)}</td><td>${esc(row.new_level_id)}</td><td>${esc(row.new_template_id)}</td><td>${row.description === "" ? '<span class="file-meta">（空备注）</span>' : esc(row.description)}</td><td class="parameter-preview-state ${row.status === "blocked" ? "is-error" : ""}">${esc({append: "将追加", skip: "跳过", blocked: "冲突", excluded: "不写入"}[row.status] || row.status)}<small>${esc(row.message || "")}</small></td></tr>`).join("");
      el("parameter-preview-diff").textContent = typeof plan.diff === "string" ? plan.diff : JSON.stringify(plan.diff, null, 2);
      el("parameter-preview-feedback").hidden = true;
      el("parameter-confirm-write").disabled = plan.status === "error" || plan.status === "blocked";
      el("parameter-confirm-write").textContent = plan.status === "empty" ? "确认无需追加" : "确认追加";
      openDialog(el("parameter-preview-dialog")); el("parameter-preview-cancel").focus();
    });
    el("parameter-confirm-write").addEventListener("click", async () => {
      if (workflow.state.pending) return;
      const dialog = el("parameter-preview-dialog"); setDialogPending(dialog, true);
      const result = await workflow.commit(); setDialogPending(dialog, false);
      if (!result || result.status !== "committed") {
        const box = el("parameter-preview-feedback"); box.hidden = false; box.querySelector("span").textContent = workflow.state.error || "写入未完成，请重新预览。";
        return;
      }
      closeDialog(dialog);
      const appendCount = result.summary?.append_count ?? (result.rows || []).filter(row => row.status === "append").length;
      showDeliveryCompletion(result.written ? "配置写入完成" : "配置核对完成", result.written ? `已追加 ${appendCount} 行关卡配置。` : "所选配置内容已相同，无需追加。", result.directories || [{label: "配置", path: workflow.state.result?.config_path || result.config_path || ""}], "parameter-preview-button");
    });
    render();
    return {workflow, enter: async () => { await workflow.loadContext(currentJob()); if (workflow.state.taskTicket && workflow.state.task?.status !== 'succeeded') workflow.resume(); }, contextChanged: () => workflow.contextChanged(), reset: () => {
      if (workflow.writeLocked()) return;
      workflow.reset(); uploadSession = null; uploadedFiles = null;
      ["parameter-template-ids", "parameter-file-associations", "parameter-level-start", "parameter-level-end", "parameter-search", "parameter-description", "parameter-template-files"].forEach(id => { el(id).value = ""; });
      el("parameter-file-associations-details").open = false;
      el("parameter-level-id-rule").value = "prefix"; el("parameter-level-id-prefix").value = "lessT"; el("parameter-unify-description").checked = false; el("parameter-source").value = "output"; el("parameter-page-size").value = "20";
    }};
  }
  return {MAX_FILE_BYTES, parseTemplateInput, parseFileAssociations, filterRows, pageRows, changedRows, createWorkflow, renderMappingRows, renderComparisonRows, renderSourceCandidates, mount};
});
