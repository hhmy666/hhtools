/* One user selection, bounded requests, resumable in the current page. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TowerReducerBatchImport = api;
})(typeof window === "object" ? window : globalThis, function () {
  "use strict";
  const positive = (value, fallback) => Number.isInteger(value) && value > 0 ? value : fallback;
  function createSession({items, mode, source, limits = {}}) {
    const selected = mode === "ids" ? [...new Set(items)] : [...items];
    if (!selected.length) throw new Error("请先选择模板");
    const count = positive(limits.file_count, 20);
    const bytes = positive(limits.request_bytes, 128 * 1024 * 1024);
    const fileLimit = positive(limits.file_bytes, 32 * 1024 * 1024);
    const batches = []; let batch = [], size = 65536;
    for (const item of selected) {
      // Keep room for multipart headers (including UTF-8 filenames).
      const rejected = mode === "files" && item.size > fileLimit;
      const cost = mode === "files" && !rejected ? item.size + 1024 + item.name.length * 4 : 0;
      if (cost + 65536 > bytes) throw new Error(`${item.name} 超过当前服务可接收的文件大小`);
      if (batch.length && (Boolean(batch.rejected) !== rejected || batch.length >= count || size + cost > bytes)) {
        batches.push(batch); batch = []; size = 65536;
      }
      if (rejected) batch.rejected = true;
      batch.push(item); size += cost;
    }
    if (batch.length) batches.push(batch);
    return {mode, source, batches, total: selected.length, completed: 0, results: [], sourceBinding: null, result: null};
  }
  function checkAbort(signal) {
    if (signal?.aborted) throw Object.assign(new Error("已取消解析"), {name: "AbortError"});
  }
  async function run(session, {request, onProgress = () => {}, formFactory = () => new FormData(), signal} = {}) {
    const report = (phase, batch) => onProgress({phase, batch,
      completed: session.completed, total: session.total, totalBatches: session.batches.length});
    checkAbort(signal);
    if (session.result) return session.result;
    let index = session.results.length;
    while (index < session.batches.length) {
      checkAbort(signal);
      report("reading", index + 1);
      const items = session.batches[index];
      let options = {method: "POST", signal};
      if (items.rejected) {
        options.json = {files: items.map((file) => ({name: file.name, size: file.size}))};
      } else if (session.mode === "ids") {
        options.json = {template_ids: items, source_repository: session.source};
        if (session.sourceBinding) options.json.expected_binding = session.sourceBinding;
      } else {
        options.formData = formFactory();
        items.forEach((file) => options.formData.append("files", file, file.name));
      }
      let result;
      try {
        result = await request(items.rejected ? "/api/import-rejections"
          : session.mode === "ids" ? "/api/import-ids" : "/api/import", options);
      } catch (error) {
        checkAbort(signal);
        if (session.mode === "ids" && error.code === "import_too_large" && items.length > 1) {
          const middle = Math.ceil(items.length / 2);
          session.batches.splice(index, 1, items.slice(0, middle), items.slice(middle));
          continue;
        }
        throw error;
      }
      checkAbort(signal);
      if (!/^import-[0-9a-f]{32}$/.test(result?.import_id || "")
          || !Array.isArray(result.files) || result.files.length !== items.length) {
        throw new Error("返回的模板数量不完整，请继续解析；已完成批次仍保留");
      }
      if (session.sourceBinding && result.source_binding !== session.sourceBinding) {
        throw new Error("读取来源已变化，请重新导入，避免合并不同模板库的数据");
      }
      session.sourceBinding = result.source_binding || session.sourceBinding;
      session.results.push(result);
      session.completed += items.length;
      report("reading", index + 1);
      index += 1;
    }
    checkAbort(signal);
    if (session.results.length === 1) session.result = session.results[0];
    else {
      report("merging", session.batches.length);
      let result;
      try {
        result = await request("/api/imports/merge", {method: "POST", signal,
          json: {import_ids: session.results.map((item) => item.import_id)}});
      } catch (error) {
        if (error.status === 404 && error.code === "not_found") {
          throw new Error("旧服务不支持自动合并，请关闭旧启动窗口，重新启动工具后导入");
        }
        throw error;
      }
      checkAbort(signal);
      if (!/^import-[0-9a-f]{32}$/.test(result?.import_id || "")
          || !Array.isArray(result.files) || result.files.length !== session.total) {
        throw new Error("合并后的模板数量不完整，请继续解析以重试合并");
      }
      session.result = result;
    }
    report("complete", session.batches.length);
    return session.result;
  }
  return {createSession, run};
});
