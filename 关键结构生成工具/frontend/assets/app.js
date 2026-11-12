(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const STRUCTURE_TYPES = "CDEFGHIJKLMN".split("");
  const MAJORITY_A_ON_TIE = "majority_a_on_tie";
  const LEGACY_PREFER_A_ELSE_B = "prefer_a_else_b";
  const TYPE_COLORS = {
    A: "#e0e4e1", B: "#b8c1bd", C: "#f37b73", D: "#efb94f",
    E: "#e5ce47", F: "#6fcf97", G: "#42bea8", H: "#55a7e7",
    I: "#4e8ed8", J: "#8d72d9", K: "#a98ae5", L: "#62c7ba",
    M: "#d88cc6", N: "#ef9b86",
  };
  const PLAN_MODE_COPY = {
    use_batch: "使用统一方案 · 当前模板无需单独维护参数。",
    replace: "仅本模板 · 下方方案会替代统一方案。",
    append: "追加 · 保留统一方案，同时新增一份独立成品。",
    skip: "跳过 · 本批次不为当前模板生成成品。",
  };

  const state = {
    csrf: "",
    profile: null,
    strategies: {},
    importMode: "template_ids",
    importFiles: [],
    imported: null,
    currentFlow: 1,
    unlockedFlow: 1,
    completedSteps: new Set(),
    batchPlan: newPlanModel(),
    precisionPlanSnapshot: null,
    templateRules: new Map(),
    editorScope: "batch",
    activeTemplate: null,
    syncingForm: false,
    seed: "",
    seedWasAutoResolved: false,
    jobId: null,
    jobResult: null,
    jobSnapshot: null,
    jobStartedAt: null,
    jobPollTimer: null,
    jobPollFailures: 0,
    jobRecommendationPollAttempts: 0,
    jobLoadToken: 0,
    taskControlsPending: new Set(),
    sourceLayerViews: new Map(),
    taskSourceItem: null,
    sourcePreviewToken: 0,
    isRecoveredJob: false,
    selectionsByJob: new Map(),
    selectedCandidates: new Map(),
    activeWorkIndex: 0,
    candidatePage: 0,
    compareIds: new Set(),
    focusCandidate: null,
    focusReturnId: null,
    focusReturnPage: null,
    focusReturnWorkKey: null,
    player: { unitIndex: 0, routeKey: "threshold", step: 0, timer: null },
    recentExport: null,
    exportId: null,
    exportSnapshot: null,
    exportStartedAt: null,
    exportPollTimer: null,
    exportPollFailures: 0,
    exportLoadToken: 0,
    confirmAction: null,
    confirmTrigger: null,
    candidateOverviewOpened: false,
    pendingUsePartialReview: false,
    preflightSnapshot: null,
    parameterSource: "output_ids",
    parameterMode: "online",
    parameterFiles: [],
    parameterSelectionId: null,
    parameterSelectionContext: null,
    parameterResult: null,
    parameterResolveToken: 0,
    parameterCalculationToken: 0,
    consumptionHistoryFile: null,
    consumptionConfigLevelFile: null,
    consumptionFirstDupOverrides: {},
    consumptionCalibrationResult: null,
    consumptionCalibrationFingerprint: null,
    consumptionCalibrationToken: 0,
    unlaunchedLookupToken: 0,
    unlaunchedParameterResult: null,
    lastParameterSuggestionUndo: null,
    resumeBudgetMultiplier: null,
    parameterEntryPreferRecent: false,
    defaultConfigSetId: "",
    calibrationSource: "output_ids",
    calibrationFiles: [],
    calibrationSelectionId: null,
    calibrationResolvedItems: [],
    calibrationNameMatchToken: 0,
    calibrationNameMatchTimer: null,
    calibrationNameMatchReady: false,
    calibrationResolveToken: 0,
    adjustmentContext: null,
    stagedTemplateAdjustments: new Map(),
    runningAdjustmentKeys: new Set(),
    adjustmentDialog: null,
    adjustmentRunToken: 0,
    adjustmentRunPending: false,
  };

  function $(selector, root = document) { return root.querySelector(selector); }
  function $$(selector, root = document) { return [...root.querySelectorAll(selector)]; }
  function announce(selector, copy) {
    const node = $(selector);
    if (node && node.textContent !== copy) node.textContent = copy;
  }
  function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }
  function numberValue(selector, fallback) {
    const value = Number($(selector)?.value);
    return Number.isFinite(value) ? value : fallback;
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }
  function icon(name) { return `<svg aria-hidden="true"><use href="#i-${name}"></use></svg>`; }
  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }
  function formatDuration(milliseconds) {
    const totalSeconds = Math.max(0, Math.floor(Number(milliseconds || 0) / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return hours > 0
      ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  function formatApproxDuration(milliseconds) {
    const seconds = Math.max(0, Math.ceil(Number(milliseconds || 0) / 1000));
    if (seconds < 60) return "1 分钟内";
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `约 ${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return remainder ? `约 ${hours} 小时 ${remainder} 分钟` : `约 ${hours} 小时`;
  }
  function effectiveTaskElapsed(task, now = Date.now()) {
    const startedAt = new Date(task?.started_at || task?.created_at || now).getTime();
    const endedAt = task?.completed_at ? new Date(task.completed_at).getTime() : now;
    if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt)) return 0;
    const controlEvents = [...(task?.events || [])]
      .filter((event) => ["pause", "resume"].includes(event?.type) && event?.at)
      .sort((left, right) => new Date(left.at).getTime() - new Date(right.at).getTime());
    let pauseStartedAt = null; let pausedDuration = 0;
    controlEvents.forEach((event) => {
      const at = new Date(event.at).getTime();
      if (!Number.isFinite(at)) return;
      if (event.type === "pause" && pauseStartedAt === null) pauseStartedAt = at;
      if (event.type === "resume" && pauseStartedAt !== null) {
        pausedDuration += Math.max(0, at - pauseStartedAt); pauseStartedAt = null;
      }
    });
    if (pauseStartedAt !== null) pausedDuration += Math.max(0, endedAt - pauseStartedAt);
    return Math.max(0, endedAt - startedAt - pausedDuration);
  }
  function formatClock(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
  }
  function statusLabel(value) {
    return ({
      queued: "等待开始", running: "运行中", pause_requested: "正在暂停",
      paused: "已暂停", cancel_requested: "正在终止", cancelled: "已终止",
      completed: "已完成", partial: "部分完成", failed: "失败", committing: "安全写入中",
    })[value] || String(value || "未知状态");
  }
  function eventCopy(event) {
    const details = event?.details || {};
    if (event?.type === "work_item_progress") {
      const scope = [details.template_key, details.plan_signature || details.plan_id].filter(Boolean).join(" · ");
      const result = details.error
        ? itemErrorCopy({ error: details.error })
        : `${Number(details.candidate_count || 0)} 个候选已通过硬校验`;
      return `${scope || "当前成品项"}：${result}`;
    }
    if (event?.type === "rebuild_progress") {
      const scope = [details.template_key, details.plan_id].filter(Boolean).join(" · ") || "当前成品项";
      const completed = Math.max(0, Number(details.completed_work_items || 0));
      const total = Math.max(0, Number(details.work_item_count || 0));
      return `${scope}：候选重建 ${completed}/${total}`;
    }
    if (event?.type === "candidate_export_progress") {
      const scope = details.output_template_id || details.template_key || details.current_item || details.candidate_id || "当前候选";
      const completed = Math.max(0, Number(details.completed || 0));
      const total = Math.max(0, Number(details.total || 0));
      return completed === 0
        ? `${scope} 开始独立复验${total ? `（0/${total}）` : ""}`
        : `${scope} 已完成复验${total ? `（${Math.min(completed, total)}/${total}）` : ""}`;
    }
    return (event?.message || statusLabel(event?.status)).replace(/；已跳过会同时出现五种单张的分配/g, "");
  }
  function visibleSearchEvents(events) {
    const seen = new Map();
    return [...(events || [])].sort((a,b)=>Number(a.sequence ?? a.seq ?? 0)-Number(b.sequence ?? b.seq ?? 0)).filter(event=>{
      if (event.type !== "candidate_search_progress") return true;
      const d=event.details || {}, p=d.search_progress || {};
      const key=JSON.stringify([d.template_key,d.plan_id,d.plan_signature]);
      const attempts=Number(p.attempts_searched || 0), count=Number(d.candidate_count || 0);
      const last=seen.get(key) || {bucket:0,count:0,attempts:0};
      const bucket=Math.floor(attempts/1000);
      const show=bucket>last.bucket || count>last.count || p.exhausted || ["success","failed","cancelled","partial"].includes(event.status);
      seen.set(key,{bucket,count,attempts});
      return Boolean(show);
    });
  }
  function renderActivityTimeline(root, events, options = {}) {
    if (!root) return;
    const values = visibleSearchEvents(events).reverse();
    const visible = values.slice(0, options.limit || 8);
    root.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("li"); empty.className = "is-empty";
      const time = document.createElement("time"); time.textContent = "—";
      const copy = document.createElement("span"); copy.textContent = options.emptyCopy || "暂无运行记录";
      empty.append(time, copy); root.append(empty); return;
    }
    visible.forEach((event) => {
      const item = document.createElement("li");
      item.className = `event-${String(event.status || "info").replace(/[^a-z_]/g, "")}`;
      const time = document.createElement("time"); time.dateTime = event.at || ""; time.textContent = formatClock(event.at);
      const copy = document.createElement("span"); copy.textContent = eventCopy(event);
      item.append(time, copy); root.append(item);
    });
  }
  function jobProgressMetrics(job) {
    const summary = job?.result_summary || job?.result?.summary || {};
    const progress = job?.progress || {};
    const total = Math.max(0, Number(job?.work_item_count || summary.work_item_count || progress.total_work_items || 0));
    const success = Math.max(0, Number(summary.success_count || 0));
    const partial = Math.max(0, Number(summary.partial_success_count || summary.partial_count || 0));
    const failed = Math.max(0, Number(summary.failure_count || 0));
    const cancelled = Math.max(0, Number(summary.cancelled_count || 0));
    const countedFinished = success + partial + failed + cancelled;
    const reportedFinished = Math.max(0, Number(progress.completed_work_items || progress.finished_work_items || 0));
    const finished = Math.min(total, Math.max(countedFinished, reportedFinished));
    // These three UI buckets must be mutually exclusive. A partial item is
    // still pending its remaining candidate slots; it must not also inflate
    // either "success" or "failed / cancelled".
    const terminalFailures = Math.min(total, failed + cancelled);
    const completed = Math.min(Math.max(0, total - terminalFailures), success);
    const pending = Math.max(0, total - completed - terminalFailures);
    return { total, success: completed, partial, failed, cancelled, terminalFailures, pending, finished };
  }
  function availableResultForJob(job) {
    const serverValues = [job?.result, job?.partial_result, job?.result_preview, job?.available_result]
      .filter((value) => Array.isArray(value?.items));
    const jobIdentity = job?.job_id || job?.id;
    // state.jobResult may deliberately hold the parent snapshot while a local
    // continuation starts. As soon as this job publishes any server result,
    // that result is authoritative—even when a hard plan change validly drops
    // old candidates and therefore contains fewer rows/candidates.
    const fallbackAllowed = !job || !jobIdentity || !state.jobId || String(jobIdentity) === String(state.jobId);
    const results = serverValues.length
      ? serverValues
      : fallbackAllowed && Array.isArray(state.jobResult?.items) ? [state.jobResult] : [];
    return results.sort((left, right) => {
      const count = (value) => value.items.reduce((sum, item) => sum + (item?.candidates?.length || 0), 0);
      return count(right) - count(left) || right.items.length - left.items.length;
    })[0] || null;
  }
  function availableCandidatesForJob(job) {
    const result = availableResultForJob(job);
    return (result?.items || []).reduce((sum, item) => sum + (item?.candidates?.length || 0), 0);
  }
  function jobRecommendationsPending(job) {
    const values = [
      job?.recommendations_pending,
      job?.summary?.recommendations_pending,
      job?.result?.recommendations_pending,
      job?.result?.summary?.recommendations_pending,
      job?.parameter_recommendation_status,
      job?.summary?.parameter_recommendation_status,
      job?.result?.parameter_recommendation_status,
      job?.recommendation_summary?.status,
      job?.summary?.recommendation_summary?.status,
      job?.result?.recommendation_summary?.status,
    ];
    return values.some((value) => value === true || ["pending", "queued", "running", "probing"].includes(String(value || "").toLowerCase()));
  }
  function latestEvent(events, type) {
    return [...(events || [])]
      .filter((event) => event?.type === type)
      .sort((left, right) => Number(right.sequence ?? right.seq ?? 0) - Number(left.sequence ?? left.seq ?? 0))[0] || null;
  }
  function selectionStorageKey(jobId) { return `aipuzzle:candidate-selections:${jobId}`; }
  function normalizeSelectedCandidateIds(value) {
    const values = Array.isArray(value) ? value : typeof value === "string" && value ? [value] : [];
    return [...new Set(values.map((item) => String(item || "").trim()).filter(Boolean))];
  }
  function selectionsForJob(jobId) {
    if (state.selectionsByJob.has(jobId)) return state.selectionsByJob.get(jobId);
    let values = {};
    try {
      const raw = window.localStorage.getItem(selectionStorageKey(jobId));
      if (raw !== null) values = JSON.parse(raw)?.values || {};
    } catch { values = {}; }
    const map = new Map(Object.entries(values).map(([key, value]) => [key, normalizeSelectedCandidateIds(value)]));
    state.selectionsByJob.set(jobId, map);
    return map;
  }
  function persistSelections(jobId = state.jobId) {
    if (!jobId) return;
    const map = state.selectionsByJob.get(jobId) || state.selectedCandidates;
    try { window.localStorage.setItem(selectionStorageKey(jobId), JSON.stringify({ values: Object.fromEntries(map) })); } catch { /* Browser privacy mode may disable persistence. */ }
  }
  function selectedIdsForWork(itemOrKey) {
    const key = typeof itemOrKey === "string" ? itemOrKey : workKey(itemOrKey);
    return normalizeSelectedCandidateIds(state.selectedCandidates.get(key));
  }
  function isCandidateSelected(item, id) { return selectedIdsForWork(item).includes(id); }
  function selectedCandidateCount() {
    return [...state.selectedCandidates.values()].reduce((sum, value) => sum + normalizeSelectedCandidateIds(value).length, 0);
  }
  function selectedCandidateCountForTemplate(templateKey) {
    if (!templateKey) return 0;
    return successfulWorkItems()
      .filter((item) => item.template_key === templateKey)
      .reduce((sum, item) => sum + selectedIdsForWork(item).length, 0);
  }
  function uniqueLines(value) {
    return [...new Set(String(value || "").split(/[\n,，\s]+/).map((item) => item.trim()).filter(Boolean))];
  }
  function isValidTemplateId(value) {
    const normalized = String(value || "").trim();
    return normalized.length > 0 && normalized.length <= 180 && /^[A-Za-z0-9_-]+$/.test(normalized);
  }
  function hasValidTemplateIds(value) {
    const values = uniqueLines(value);
    return values.length > 0 && values.every(isValidTemplateId);
  }
  function hasValidTemplateFiles(files) {
    return Array.isArray(files)
      && files.length > 0
      && files.every((file) => file?.name?.endsWith("_m.bytes") && Number(file?.size) > 0);
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
      const random = crypto.getRandomValues(new Uint8Array(1))[0] & 15;
      const value = character === "x" ? random : (random & 3) | 8;
      return value.toString(16);
    });
  }
  function toast(message, kind = "info") {
    const item = document.createElement("div");
    item.className = `toast${kind === "error" ? " error" : ""}`;
    item.textContent = message;
    $("#toast-region").append(item);
    window.setTimeout(() => item.remove(), kind === "error" ? 6200 : 3600);
  }
  function requestConfirmation(title, copy, action, options = {}) {
    state.confirmAction = action;
    state.confirmTrigger = document.activeElement;
    $("#confirm-title").textContent = title;
    $("#confirm-copy").textContent = copy;
    $("#confirm-safe").textContent = options.safeLabel || "继续运行";
    const actionButton = $("#confirm-action");
    actionButton.textContent = options.confirmLabel || "确认终止";
    actionButton.className = options.danger === false ? "primary-button" : "danger-button";
    $("#confirm-dialog").returnValue = "";
    $("#confirm-dialog").showModal();
    window.setTimeout(() => $("#confirm-safe")?.focus(), 0);
  }
  function setBusy(button, busy, copy) {
    if (!button) return;
    if (busy) {
      button.dataset.isBusy = "true";
      button.dataset.originalCopy = button.textContent.trim();
      button.dataset.originalMarkup = button.innerHTML;
      button.disabled = true;
      button.textContent = copy || "处理中…";
    } else {
      if (button.dataset.isBusy !== "true") return;
      button.disabled = false;
      if (button.dataset.originalMarkup) button.innerHTML = button.dataset.originalMarkup;
      else button.textContent = button.dataset.originalCopy || copy || "完成";
      delete button.dataset.isBusy;
      delete button.dataset.originalCopy;
      delete button.dataset.originalMarkup;
    }
  }

  function syncAdjustmentRunControls() {
    $$('[data-adjustment-run-control], [data-run-staged-template]').forEach((button) => {
      button.disabled = state.adjustmentRunPending;
    });
  }
  function beginAdjustmentRun(parentJobId) {
    if (state.adjustmentRunPending) return null;
    const operation = {
      token: ++state.adjustmentRunToken,
      jobLoadToken: state.jobLoadToken,
      parentJobId: String(parentJobId || ""),
    };
    state.adjustmentRunPending = true;
    syncAdjustmentRunControls();
    return operation;
  }
  function adjustmentRunContextIsCurrent(operation, currentState = state) {
    return Boolean(
      operation
      && currentState.adjustmentRunPending
      && currentState.adjustmentRunToken === operation.token
      && currentState.jobLoadToken === operation.jobLoadToken
      && String(currentState.jobId || "") === operation.parentJobId
    );
  }
  function endAdjustmentRun(operation) {
    if (!operation || state.adjustmentRunToken !== operation.token) return;
    state.adjustmentRunPending = false;
    syncAdjustmentRunControls();
  }
  function invalidateAdjustmentRun() {
    state.adjustmentRunToken += 1;
    state.adjustmentRunPending = false;
    syncAdjustmentRunControls();
  }

  function parameterRequestContext(source = state.parameterSource, currentState = state) {
    const recentExport = source === "recent_export" ? currentState.recentExport : null;
    return {
      source,
      exportId: recentExport?.exportId || null,
      jobId: recentExport?.jobId || null,
    };
  }
  function parameterRequestContextIsCurrent(context, currentState = state) {
    if (!context || context.source !== currentState.parameterSource) return false;
    if (context.source !== "recent_export") return true;
    return Boolean(
      context.exportId
      && context.exportId === currentState.recentExport?.exportId
      && context.jobId === currentState.recentExport?.jobId
    );
  }
  function recentExportResolveFields(context) {
    if (!context?.exportId) throw new Error("请先完成一次验证导出。");
    return {
      export_id: context.exportId,
      ...(context.jobId ? { job_id: context.jobId } : {}),
    };
  }

  async function api(path, options = {}) {
    const request = { method: options.method || "GET", headers: { Accept: "application/json" } };
    if (request.method !== "GET") {
      request.headers["Content-Type"] = "application/json";
      request.headers["X-CSRF-Token"] = state.csrf;
      request.body = JSON.stringify(options.body || {});
    }
    let response;
    try {
      response = await fetch(`${API_ROOT}${path}`, request);
    } catch (error) {
      throw new Error("无法连接本地服务，请从启动工具重新打开页面。");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      const detail = payload.error || {};
      const suffix = detail.path ? `（${detail.path}）` : "";
      const error = new Error(`${detail.message || `请求失败 ${response.status}`}${suffix}`);
      error.status = response.status;
      error.code = detail.code || payload.code || "";
      error.detail = detail;
      throw error;
    }
    return payload;
  }

  function newPlanModel() {
    return {
      precisionMode: false,
      unitCount: 1,
      candidateCount: 6,
      searchAttemptBudget: 2000,
      difficultyTarget: "fidelity",
      steps: [5, 6],
      requiredPreGateRecycles: 0,
      firstRecycleLeadTiles: 2,
      layout: "x",
      towerMode: "disallow",
      serialDifficulty: "harder_low",
      replacementSources: MAJORITY_A_ON_TIE,
      typeCountKind: "range",
      typeMin: 3,
      typeMax: 5,
      structureGroupMode: "standard",
      uniformGroupsPerType: 0,
      automaticGroupMaxDifference: 3,
      customGroupsPerType: "2；1；1",
      maxLayerKind: "percent",
      maxLayerValue: 50,
      totalLimitKind: "percent",
      totalLimitValue: 35,
      totalCountMinimum: 0,
      totalMinimumKind: "percent",
      maxStructureLayerSpanKind: "absolute",
      maxStructureLayerSpan: 5,
      minSameTypeLayerSpan: 2,
      allowedTypes: [...STRUCTURE_TYPES],
      quality: {
        position: "unrestricted",
        symmetry: "off",
        density: "compact",
        experience_archetype: "auto",
        special_preset: "none",
        long_range_wave_count: "auto",
        online_style_reference: "level_200_300_v1",
      },
    };
  }

  function planSignature(plan) {
    if (plan.towerMode === "only") return "tower";
    return plan.steps.slice(0, plan.unitCount).map((step) => `s${step}`).join("");
  }
  function planHuman(plan) {
    if (plan.towerMode === "only") return "仅塔内均匀分布";
    const steps = plan.steps.slice(0, plan.unitCount).map((step) => `s${step}`).join(" + ");
    const layout = plan.unitCount === 1 ? "单谜题" : ({ x: "左右双谜题", y: "上下双谜题", z: "垂直双谜题" }[plan.layout] || "双谜题");
    return `${layout} ${steps}`;
  }
  function planLayerCopy(plan) {
    if (plan.maxLayerKind === "none") return "不限层";
    const scope = plan.towerMode === "disallow" ? "非塔最高层" : "整板最高层";
    return plan.maxLayerKind === "percent" ? `${scope} ${plan.maxLayerValue}%` : `最高第 ${plan.maxLayerValue} 层`;
  }
  function planRecycleCopy(plan) {
    if (plan.towerMode === "only") return "塔内模式不计算回收轨迹";
    const recycles = Math.max(0, Math.round(Number(plan.requiredPreGateRecycles) || 0));
    if (!recycles) return "门槛前不回收";
    return `门槛前回收 ${recycles} 次 · 首次回收前铺垫 ${Math.max(0, Math.round(Number(plan.firstRecycleLeadTiles) || 0))} 格`;
  }
  function parseStructureGroupVector(raw) {
    const source = String(raw ?? "").trim();
    if (!source) return { values: [], error: "请填写每种结构块的组数。" };
    const tokens = source.split(/[；;,，、\s]+/).filter(Boolean);
    if (!tokens.length) return { values: [], error: "请填写每种结构块的组数。" };
    if (tokens.length > 12) return { values: [], error: "自定义每类组数最多填写 12 项。" };
    const values = tokens.map((token) => Number(token));
    if (values.some((value) => !Number.isInteger(value) || value < 1)) {
      return { values: [], error: "自定义每类组数的每一项都必须是正整数。" };
    }
    return { values, error: "" };
  }
  function hydrateTypeCountModel(rawChoice, fallbackPlan) {
    const choice = rawChoice;
    const typeKind = typeof choice === "number" ? "exact" : String(choice?.kind || "range");
    const exactValue = typeof choice === "number" ? choice : choice?.value;
    const minimumFallback = typeKind === "random" ? 3 : fallbackPlan.typeMin;
    const maximumFallback = typeKind === "random" ? 7 : fallbackPlan.typeMax;
    const minimum = clamp(Math.round(Number(exactValue ?? choice?.minimum) || minimumFallback), 1, 12);
    const maximum = typeKind === "exact"
      ? minimum
      : clamp(Math.round(Number(choice?.maximum) || maximumFallback), 1, 12);
    return {
      kind: typeKind === "exact" ? "exact" : "range",
      legacyKind: typeKind === "random" ? "random" : "",
      minimum,
      maximum,
    };
  }
  function typeChoice(plan) {
    if (plan.structureGroupMode === "custom") {
      const parsed = parseStructureGroupVector(plan.customGroupsPerType);
      return { kind: "exact", value: clamp(parsed.values.length || 1, 1, 12) };
    }
    if (plan.typeCountKind === "exact") return { kind: "exact", value: clamp(Math.round(plan.typeMin), 1, 12) };
    return {
      kind: plan.legacyTypeCountKind === "random" ? "random" : "range",
      minimum: clamp(Math.round(plan.typeMin), 1, 12),
      maximum: clamp(Math.round(plan.typeMax), 1, 12),
    };
  }
  function structureGroupGenerationFields(plan) {
    if (plan.quality?.special_preset === "all_types_long_range") return {};
    if (plan.structureGroupMode === "custom") {
      const parsed = parseStructureGroupVector(plan.customGroupsPerType);
      return parsed.error ? {} : { groups_per_type: parsed.values };
    }
    const rawUniform = Number(plan.uniformGroupsPerType);
    const uniform = Number.isFinite(rawUniform) ? Math.max(0, Math.round(rawUniform)) : 1;
    return uniform === 1 ? {} : { uniform_groups_per_type: uniform };
  }
  function effectiveStructureTypeConfiguration(plan) {
    if (plan.quality?.special_preset === "all_types_long_range") {
      return {
        overridden: true,
        kind: "exact",
        minimum: 12,
        maximum: 12,
        allowedTypes: [...STRUCTURE_TYPES],
        tilesPerType: 3,
        totalTiles: 36,
        minimumTiles: 36,
        maximumTiles: 36,
        groupsPerType: Array(12).fill(1),
      };
    }
    if (plan.structureGroupMode === "custom") {
      const parsed = parseStructureGroupVector(plan.customGroupsPerType);
      const values = parsed.values;
      const totalTiles = values.reduce((sum, value) => sum + value, 0) * 3;
      return {
        overridden: false,
        kind: "exact",
        minimum: values.length,
        maximum: values.length,
        allowedTypes: [...plan.allowedTypes],
        tilesPerType: null,
        totalTiles,
        minimumTiles: totalTiles,
        maximumTiles: totalTiles,
        groupsPerType: values,
        error: parsed.error,
      };
    }
    const configured = typeChoice(plan);
    const exact = configured.kind === "exact";
    const rawUniform = Number(plan.uniformGroupsPerType);
    const uniform = Number.isFinite(rawUniform) ? Math.max(0, Math.round(rawUniform)) : 1;
    const minimum = exact ? configured.value : configured.minimum;
    const maximum = exact ? configured.value : configured.maximum;
    return {
      overridden: false,
      kind: configured.kind,
      minimum,
      maximum,
      allowedTypes: [...plan.allowedTypes],
      tilesPerType: uniform === 0 ? null : uniform * 3,
      totalTiles: uniform === 0 ? null : exact ? minimum * uniform * 3 : null,
      minimumTiles: uniform === 0 ? minimum * 3 : minimum * uniform * 3,
      maximumTiles: uniform === 0 ? Number.POSITIVE_INFINITY : maximum * uniform * 3,
      groupsPerType: uniform === 0 ? null : exact ? Array(minimum).fill(uniform) : null,
    };
  }
  function unitRoles(plan) {
    if (plan.unitCount < 2) return ["谜题1"];
    if (plan.layout === "x") return ["左侧", "右侧"];
    if (plan.layout === "y") return ["上区", "下区"];
    return plan.serialDifficulty === "easier_low" ? ["低层较简单", "高层较难"] : ["高层", "低层"];
  }
  function specialPresetCompatibility(plan, preset) {
    if (preset === "none") return { ok: true, reason: "" };
    if (preset === "all_types_long_range") {
      if (Number(plan.unitCount) !== 1) return { ok: false, reason: "需要单谜题方案" };
      if (plan.towerMode === "only") return { ok: false, reason: "仅塔内方案不能使用" };
      return { ok: true, reason: "强制 C–N 12 类×3，并按 3 或 4 个揭露波次生成" };
    }
    if (preset === "serial_mechanism") {
      if (Number(plan.unitCount) !== 2) return { ok: false, reason: "需要双谜题方案" };
      if (plan.towerMode === "only") return { ok: false, reason: "仅塔内方案不能使用" };
      if (plan.layout !== "z") return { ok: false, reason: "双谜题分布必须为 Z 层级串行" };
      return { ok: true, reason: "强制 Z 向 high→low 双单元" };
    }
    return { ok: false, reason: "未知特殊机制预设" };
  }
  function ensurePlanIsValid(plan, options = {}) {
    const groupDifference = Number(plan.automaticGroupMaxDifference ?? 3);
    if (!Number.isInteger(groupDifference) || groupDifference < 0) throw new Error("自动组数最大差值必须是非负整数。");
    const attemptBudget = Number(plan.searchAttemptBudget);
    if (!Number.isInteger(attemptBudget) || attemptBudget < 1 || attemptBudget > 100000) {
      throw new Error("每个成品项搜索上限必须是 1–100,000 的整数。");
    }
    if (!["auto", "intrinsic_gate", "board_reveal", "hybrid"].includes(plan.quality?.experience_archetype)) throw new Error("请选择有效的整板体验原型。");
    if (!["none", "all_types_long_range", "serial_mechanism"].includes(plan.quality?.special_preset)) throw new Error("请选择有效的特殊机制预设。");
    if (!["auto", "3", "4"].includes(String(plan.quality?.long_range_wave_count ?? "auto"))) throw new Error("全类型长程型的揭露波次只能选择自动、3 波或 4 波。");
    const presetCompatibility = specialPresetCompatibility(plan, plan.quality.special_preset);
    if (!presetCompatibility.ok) throw new Error(`${$("#special-preset")?.selectedOptions?.[0]?.textContent || "当前特殊机制预设"}不可用：${presetCompatibility.reason}。`);
    if (plan.quality.special_preset !== "all_types_long_range" && String(plan.quality.long_range_wave_count || "auto") !== "auto") {
      throw new Error("揭露波次只适用于全类型长程型；请改回自动，或选择全类型长程型。");
    }
    if (!["level_200_300_v1", "off"].includes(plan.quality?.online_style_reference)) throw new Error("请选择有效的线上风格参考。");
    if (!["standard", "custom"].includes(plan.structureGroupMode)) throw new Error("请选择标准配置或自定义每类组数。");
    if (plan.structureGroupMode === "standard") {
      const uniform = Number(plan.uniformGroupsPerType);
      if (!Number.isInteger(uniform) || uniform < 0) throw new Error("每种结构块组数必须是大于等于 0 的整数。");
      const minimum = Number(plan.totalCountMinimum ?? 0);
      const kind = plan.totalMinimumKind ?? "absolute";
      if (!["percent", "absolute", "none"].includes(kind)) throw new Error("请选择有效的结构块数量下限方式。");
      if (kind !== "none" && (!Number.isFinite(minimum) || minimum < 0 || (kind === "absolute" && !Number.isInteger(minimum)) || (kind === "percent" && minimum > 100))) throw new Error("数量下限须为非负整数，占比下限须在 0–100% 之间。");
      if (kind === "absolute" && plan.totalLimitKind === "absolute" && minimum > Math.floor(Number(plan.totalLimitValue) / 3) * 3) throw new Error("结构块数量下限超过按整组取整后的上限，请降低下限或提高上限。");
      if (kind === "percent" && plan.totalLimitKind === "percent" && minimum > Number(plan.totalLimitValue)) throw new Error("结构块数量下限不能超过上限。");
    } else {
      const parsedGroups = parseStructureGroupVector(plan.customGroupsPerType);
      if (parsedGroups.error) throw new Error(parsedGroups.error);
    }
    const effectiveTypes = effectiveStructureTypeConfiguration(plan);
    if (plan.towerMode !== "only" && plan.unitCount === 2) {
      const minimum = effectiveTypes.minimum;
      if (effectiveTypes.allowedTypes.length < minimum * 2) {
        throw new Error(`双谜题必须使用互斥结构块；当前至少需要 ${minimum * 2} 种可用结构块。`);
      }
      if (effectiveTypes.maximum > Math.floor(effectiveTypes.allowedTypes.length / 2)) {
        throw new Error("双谜题的单元结构种类上限超过互斥分组可提供的数量，请增加可用结构块或降低上限。");
      }
    }
    if (effectiveTypes.minimum > effectiveTypes.maximum && effectiveTypes.kind !== "exact") throw new Error("结构块种类范围的最小值不能大于最大值。");
    if (!effectiveTypes.allowedTypes.length) throw new Error("请至少选择一种 C–N 结构块。");
    if (plan.structureGroupMode === "standard" && plan.totalLimitKind === "percent" && (!Number.isFinite(Number(plan.totalLimitValue)) || Number(plan.totalLimitValue) <= 0 || Number(plan.totalLimitValue) > 100)) {
      throw new Error("结构块数量占比上限必须大于 0 且不超过 100%。");
    }
    const recycles = Number(plan.requiredPreGateRecycles);
    if (!Number.isInteger(recycles) || recycles < 0) throw new Error("门槛前回收次数必须是大于等于 0 的整数。");
    if (plan.towerMode !== "only" && recycles > 0) {
      const lead = Number(plan.firstRecycleLeadTiles);
      if (!Number.isInteger(lead) || lead < 0) throw new Error("选择门槛前回收后，请填写大于等于 0 的首次回收前铺垫块数。");
      plan.steps.slice(0, plan.unitCount).forEach((step) => {
        const firstRecycleClick = 3 + lead;
        const latestFeasibleClick = Number(step) + 2 * recycles;
        if (firstRecycleClick > latestFeasibleClick && !options.allowRecoverableRecycleConflicts) {
          throw new Error(`s${step} 且门槛前回收 ${recycles} 次时，首次三消需不晚于第 ${latestFeasibleClick} 次点击；当前铺垫 ${lead} 格会推迟到第 ${firstRecycleClick} 次。请减少铺垫块数，或调整回收次数/谜题步数。`);
        }
      });
    }
    if (plan.towerMode !== "only") {
      const configuredTileMaximum = Number(effectiveTypes.maximumTiles);
      plan.steps.slice(0, plan.unitCount).forEach((step) => {
        const requiredTileCount = Number(step) + 3 * recycles;
        if (configuredTileMaximum < requiredTileCount && !options.allowRecoverableRecycleConflicts) {
          throw new Error(`s${step} 且门槛前回收 ${recycles} 次时，路径至少需覆盖 ${requiredTileCount} 格结构块；当前配置最多生成 ${configuredTileMaximum} 格，请增加种类或组数，或减少回收次数/谜题步数。`);
        }
      });
    }
    const minimumSpan = Number(plan.minSameTypeLayerSpan ?? 0);
    if (!Number.isInteger(minimumSpan) || minimumSpan < 0) throw new Error("同种结构块最小间隔层必须是大于等于 0 的整数。");
    if (plan.maxStructureLayerSpanKind === "absolute") {
      const span = Number(plan.maxStructureLayerSpan);
      if (!Number.isInteger(span) || span < 0) throw new Error("结构块最大层间距必须是大于等于 0 的整数。");
      if (minimumSpan > span) throw new Error("同种结构块最小间隔层不能超过结构块最大层间距。");
    }
  }
  function ensurePlanFitsTemplate(plan, item, options = {}) {
    ensurePlanIsValid(plan, options);
    if (plan.towerMode !== "disallow") return;
    const available = Number(item?.inspection?.non_tower_tile_count);
    const highestLayer = Number(item?.inspection?.non_tower_max_layer);
    if (Number.isFinite(available) && Number.isFinite(highestLayer) && (available <= 0 || highestLayer <= 0)) {
      throw new Error(`${item.template_key} 没有可替换的非塔 Tile；请改为允许塔内、仅塔内，或跳过该模板。`);
    }
  }
  function generationSpec(plan) {
    const generation = {
      replacement_sources: Array.isArray(plan.replacementSources) ? [...plan.replacementSources] : plan.replacementSources,
      allowed_types: [...plan.allowedTypes],
      candidate_count: clamp(Math.round(plan.candidateCount), 1, 20),
      attempt_budget: clamp(Math.round(Number(plan.searchAttemptBudget) || 2000), 1, 100000),
      quality: {
        ...clone(plan.quality),
        difficulty_target: ["fidelity", "standard", "strong"].includes(plan.difficultyTarget) ? plan.difficultyTarget : "fidelity",
      },
      unit_defaults: { type_count: typeChoice(plan), ...structureGroupGenerationFields(plan) },
      automatic_group_max_difference: Number(plan.automaticGroupMaxDifference ?? 3),
      min_same_type_layer_span: Number(plan.minSameTypeLayerSpan ?? 0),
    };
    if (plan.precisionMode) generation.precision_mode = true;
    if (plan.towerMode !== "only") {
      generation.required_pre_gate_recycles = Math.max(0, Math.round(Number(plan.requiredPreGateRecycles) || 0));
      if (generation.required_pre_gate_recycles > 0) {
        generation.first_recycle_lead_tiles = Math.max(0, Math.round(Number(plan.firstRecycleLeadTiles) || 0));
      }
    }
    if (plan.maxLayerKind === "percent") generation.max_layer_percent = clamp(Number(plan.maxLayerValue), 1, 100);
    if (plan.maxLayerKind === "absolute") generation.max_layer = Math.max(1, Math.round(plan.maxLayerValue));
    if (plan.structureGroupMode === "standard" && plan.totalLimitKind === "percent") generation.total_percent_limit = clamp(Number(plan.totalLimitValue), 1, 100);
    if (plan.structureGroupMode === "standard" && plan.totalLimitKind === "absolute") generation.total_count_limit = Math.max(0, Math.round(plan.totalLimitValue));
    if (plan.structureGroupMode === "standard") {
      if (plan.totalMinimumKind === "percent") generation.total_percent_minimum = Number(plan.totalCountMinimum ?? 0);
      else generation.total_count_minimum = plan.totalMinimumKind === "none" ? 0 : Number(plan.totalCountMinimum ?? 0);
    }
    if (plan.maxStructureLayerSpanKind === "absolute") {
      generation.max_structure_layer_span = Math.max(0, Math.round(Number(plan.maxStructureLayerSpan) || 0));
    }
    return generation;
  }
  function toPlanSpec(plan, planId, options = {}) {
    ensurePlanIsValid(plan, options);
    const onlyTower = plan.towerMode === "only";
    const roles = unitRoles(plan);
    const planSpec = {
      plan_id: planId,
      units: [],
      tower_mode: plan.towerMode,
      serial_difficulty: plan.serialDifficulty,
      generation: generationSpec(plan),
    };
    if (onlyTower) {
      planSpec.tower_generation = { type_count: typeChoice(plan), ...structureGroupGenerationFields(plan), allowed_types: [...plan.allowedTypes] };
      return planSpec;
    }
    planSpec.layout = plan.unitCount === 1 ? "single" : plan.layout;
    const typeGroups = plan.unitCount === 2
      ? [plan.allowedTypes.slice(0, Math.ceil(plan.allowedTypes.length / 2)), plan.allowedTypes.slice(Math.ceil(plan.allowedTypes.length / 2))]
      : [[...plan.allowedTypes]];
    planSpec.units = plan.steps.slice(0, plan.unitCount).map((step, index) => ({
      unit_id: roles[index] || `谜题${index + 1}`,
      step: clamp(Math.round(step), 4, 9),
      generation: { type_count: typeChoice(plan), ...structureGroupGenerationFields(plan), allowed_types: typeGroups[index] },
    }));
    return planSpec;
  }

  function buildBatchRequest(options = {}) {
    if (!state.imported?.items?.length) throw new Error("请先解析模板。");
    ensurePlanIsValid(state.batchPlan, options);
    const templateKeys = state.imported.items.map((item) => item.template_key);
    state.imported.items.forEach((item) => {
      const rule = state.templateRules.get(item.template_key);
      if (!rule || rule.mode === "use_batch" || rule.mode === "append") ensurePlanFitsTemplate(state.batchPlan, item, options);
      if (rule && (rule.mode === "replace" || rule.mode === "append")) ensurePlanFitsTemplate(rule.plan, item, options);
    });
    const request = {
      schema_version: 1,
      template_keys: templateKeys,
      batch_plans: [toPlanSpec(state.batchPlan, `batch_${planSignature(state.batchPlan)}`, options)],
      template_rules: [],
    };
    const seed = String(state.seed || "").trim();
    if (seed && (!state.seedWasAutoResolved || $("#lock-seed").checked)) {
      if (!/^\d+$/.test(seed)) throw new Error("谜题生成种子必须是非负十进制整数。");
      const normalizedSeed = BigInt(seed);
      if (normalizedSeed > 18446744073709551615n) throw new Error("谜题生成种子不能超过 uint64 上限。");
      request.batch_seed = normalizedSeed.toString();
    }
    for (const templateKey of templateKeys) {
      const rule = state.templateRules.get(templateKey);
      if (!rule || rule.mode === "use_batch") continue;
      const payload = { template_key: templateKey, mode: rule.mode, plans: [] };
      if (rule.mode === "replace" || rule.mode === "append") {
        payload.plans = [toPlanSpec(rule.plan, `local_${templateKey}_${planSignature(rule.plan)}`, options)];
      }
      request.template_rules.push(payload);
    }
    return request;
  }

  function navigate(view) {
    $$(".view").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
    $$("[data-nav]").forEach((item) => item.classList.toggle("is-active", item.dataset.nav === view));
    if (view === "tasks") refreshTasks();
    if (view === "reports") refreshReports();
    if (view === "parameters") prepareParameterView();
    if (view === "consumption") prepareConsumptionView();
    if (view === "settings") renderProfile();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  const HELP_VIEW_TOPICS = Object.freeze({
    tasks: "help-tasks",
    reports: "help-reports",
    parameters: "help-parameters",
    consumption: "help-calibration",
    settings: "help-settings",
  });
  let helpReturnFocus = null;
  let helpScrollFrame = null;
  const HELP_SEARCH_ALIASES = Object.freeze({
    seed: ["seed", "randomseed", "随机种子"],
    randomseed: ["seed", "randomseed", "随机种子"],
    随机种子: ["seed", "randomseed", "随机种子"],
    strategygroudid: ["strategygroudid", "策略组", "策略组id", "结构降难", "1003", "1004"],
    策略组: ["strategygroudid", "策略组", "策略组id", "结构降难", "1003", "1004"],
    策略组id: ["strategygroudid", "策略组", "策略组id", "结构降难", "1003", "1004"],
    结构降难: ["strategygroudid", "策略组", "策略组id", "结构降难", "1003", "1004"],
    "1003": ["1003", "1004", "结构降难", "策略组", "strategygroudid"],
    "1004": ["1004", "1003", "结构降难", "策略组", "strategygroudid"],
    duppara: ["duppara", "副本参数", "副本配置"],
    副本参数: ["duppara", "副本参数", "副本配置"],
    副本配置: ["duppara", "副本参数", "副本配置"],
  });

  function currentHelpTopic() {
    const activeView = $(".view.is-active")?.dataset.view || "create";
    if (activeView !== "create") return HELP_VIEW_TOPICS[activeView] || "help-start";
    if (!$("#focus-workspace")?.classList.contains("is-hidden")) return "help-board";
    if (!$("#compare-workspace")?.classList.contains("is-hidden")) return "help-candidates";
    if (!$("#candidate-workspace")?.classList.contains("is-hidden")) return "help-candidates";
    return ({ 1: "help-import", 2: "help-plan", 3: "help-generation", 4: "help-export" })[state.currentFlow] || "help-start";
  }

  function setActiveHelpTopic(topicId) {
    $$(".help-nav a").forEach((link) => {
      const active = link.getAttribute("href") === `#${topicId}`;
      link.classList.toggle("is-active", active);
      link.toggleAttribute("aria-current", active);
    });
    updateHelpLocation(topicId);
  }

  function updateHelpLocation(topicId) {
    const location = $("#help-current-topic");
    if (!location) return;
    if (!topicId) {
      location.textContent = "搜索结果";
      return;
    }
    const topic = document.getElementById(topicId);
    location.textContent = $("h3", topic)?.textContent.trim() || "使用说明";
  }

  function scrollToHelpTopic(topicId, behavior = "auto") {
    const topic = document.getElementById(topicId) || $("#help-start");
    if (!topic || topic.hidden) return;
    topic.scrollIntoView({ behavior, block: "start" });
    setActiveHelpTopic(topic.id);
  }

  function helpSearchTerms(rawQuery) {
    const query = String(rawQuery || "").trim().toLocaleLowerCase("zh-CN");
    const tokens = query.split(/\s+/).filter(Boolean);
    const groups = tokens.map((token) => [...new Set(HELP_SEARCH_ALIASES[token] || [token])]);
    return { query, groups, highlights: [...new Set(groups.flat())] };
  }

  function helpSearchMatch(text, groups) {
    const searchable = String(text || "").toLocaleLowerCase("zh-CN");
    return groups.every((aliases) => aliases.some((alias) => searchable.includes(alias)));
  }

  function highlightHelpText(text, terms) {
    const escaped = escapeHtml(String(text || ""));
    const patterns = terms
      .filter(Boolean)
      .sort((left, right) => right.length - left.length)
      .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!patterns.length) return escaped;
    return escaped.replace(new RegExp(`(${patterns.join("|")})`, "giu"), (match) => "<mark>" + match + "</mark>");
  }

  function helpSearchExcerpt(text, terms) {
    const compact = String(text || "").replace(/\s+/g, " ").trim();
    const lowered = compact.toLocaleLowerCase("zh-CN");
    const positions = terms.map((term) => lowered.indexOf(term)).filter((index) => index >= 0);
    const first = positions.length ? Math.min(...positions) : 0;
    const start = Math.max(0, first - 54);
    const end = Math.min(compact.length, Math.max(first + 108, start + 150));
    return `${start ? "…" : ""}${compact.slice(start, end)}${end < compact.length ? "…" : ""}`;
  }

  function renderHelpSearchResults(search) {
    const results = $("#help-search-results");
    if (!results) return [];
    if (!search.groups.length) {
      results.hidden = true;
      results.replaceChildren();
      return [];
    }
    const hits = [];
    $$("[data-help-topic]").forEach((topic) => {
      const topicTitle = $("h3", topic)?.textContent.trim() || "使用说明";
      const aliases = topic.dataset.helpAliases || "";
      const blocks = $$('[data-help-section]', topic);
      blocks.forEach((block, index) => {
        const sectionTitle = block.dataset.helpSection || $("h4", block)?.textContent.trim() || "概览";
        const searchable = `${topicTitle} ${aliases} ${sectionTitle} ${block.textContent}`;
        if (!helpSearchMatch(searchable, search.groups)) return;
        if (!block.id) block.id = `${topic.id}-result-${index + 1}`;
        hits.push({ topicTitle, sectionTitle, targetId: block.id, text: block.textContent });
      });
      if (!blocks.length && helpSearchMatch(`${topicTitle} ${aliases} ${topic.textContent}`, search.groups)) {
        hits.push({ topicTitle, sectionTitle: "概览", targetId: topic.id, text: topic.textContent });
      }
    });
    results.innerHTML = hits.map((hit) => `<button class="help-search-result" type="button" data-help-result-target="${escapeHtml(hit.targetId)}"><span>${highlightHelpText(hit.topicTitle, search.highlights)} · ${highlightHelpText(hit.sectionTitle, search.highlights)}</span><small>${highlightHelpText(helpSearchExcerpt(hit.text, search.highlights), search.highlights)}</small></button>`).join("");
    results.hidden = false;
    return hits;
  }

  function openHelpTarget(targetId, behavior = "smooth") {
    const dialog = $("#help-dialog");
    if (!dialog) return;
    if (!dialog.open) {
      helpReturnFocus = document.activeElement;
      dialog.showModal();
    }
    const search = $("#help-search-input");
    if (search) search.value = "";
    filterHelpTopics("");
    const target = document.getElementById(targetId) || $(`[data-help-target="${targetId}"]`) || $("#help-start");
    target?.scrollIntoView({ behavior, block: "start" });
    setActiveHelpTopic(target?.closest?.("[data-help-topic]")?.id || target?.id || "help-start");
    $("#help-content")?.focus({ preventScroll: true });
  }

  function filterHelpTopics(rawQuery) {
    const search = helpSearchTerms(rawQuery);
    const hits = renderHelpSearchResults(search);
    const matchedTopics = new Set(hits.map((hit) => document.getElementById(hit.targetId)?.closest?.("[data-help-topic]")?.id).filter(Boolean));
    $$("[data-help-topic]").forEach((topic) => {
      topic.hidden = Boolean(search.groups.length);
      $$(`.help-nav a[href="#${topic.id}"]`).forEach((link) => { link.hidden = Boolean(search.groups.length) && !matchedTopics.has(topic.id); });
      $$("details", topic).forEach((detail) => {
        if (!search.groups.length && detail.dataset.helpSearchOpened === "true") {
          detail.open = false;
          delete detail.dataset.helpSearchOpened;
        }
      });
    });
    $$("[data-help-nav-group]").forEach((group) => {
      group.hidden = $$('a', group).every((link) => link.hidden);
    });
    $("#help-empty")?.classList.toggle("is-hidden", !search.groups.length || hits.length > 0);
    const summary = $("#help-search-summary");
    if (summary) {
      summary.hidden = !search.groups.length;
      summary.textContent = search.groups.length ? `找到 ${hits.length} 个相关小节` : "";
    }
    if (search.groups.length) setActiveHelpTopic("");
    else syncHelpTopicFromScroll();
    return hits.length;
  }

  function openHelpDialog() {
    const dialog = $("#help-dialog");
    if (!dialog) return;
    helpReturnFocus = document.activeElement;
    const search = $("#help-search-input");
    if (search) search.value = "";
    filterHelpTopics("");
    dialog.showModal();
    window.requestAnimationFrame(() => {
      scrollToHelpTopic(currentHelpTopic());
      search?.focus();
    });
  }

  function closeHelpDialog() {
    $("#help-dialog")?.close();
  }

  function syncHelpTopicFromScroll() {
    helpScrollFrame = null;
    if ($("#help-search-input")?.value.trim()) return;
    const content = $("#help-content");
    if (!content) return;
    const guideTop = content.getBoundingClientRect().top + 76;
    let active = $("[data-help-topic]:not([hidden])")?.id || "help-start";
    $$("[data-help-topic]:not([hidden])").forEach((topic) => {
      if (topic.getBoundingClientRect().top <= guideTop) active = topic.id;
    });
    setActiveHelpTopic(active);
  }

  function gradientExampleValues(strategy) {
    const firstA = 8;
    const firstB = 6;
    if (strategy === "flat") return Array.from({ length: 10 }, () => `${firstA}/${firstB}`);
    const balanced = strategy === "balanced";
    const startA = balanced ? 2 : 3;
    const startB = balanced ? 3 : 4;
    const step = balanced ? 2 : ({ aggressive: 1, steady: 2, gentle: 3 }[strategy] || 2);
    return Array.from({ length: 10 }, (_, index) => {
      const duplicate = index + 1;
      const decreaseA = duplicate < startA ? 0 : 1 + Math.floor((duplicate - startA) / step);
      const decreaseB = duplicate < startB ? 0 : 1 + Math.floor((duplicate - startB) / step);
      return `${firstA <= 4 ? firstA : Math.max(4, firstA - decreaseA)}/${firstB <= 3 ? firstB : Math.max(3, firstB - decreaseB)}`;
    });
  }

  function renderGradientExample() {
    const output = $("#help-gradient-example-output");
    if (!output) return;
    output.textContent = gradientExampleValues($("#help-gradient-example-mode")?.value || "balanced").join(" → ");
  }

  function setFlow(step, options = {}) {
    if (step > state.unlockedFlow) return;
    if (state.isRecoveredJob && step < 3) {
      toast("这是从任务记录恢复的已锁定结果；如需改参数，请新建任务。");
      return;
    }
    state.currentFlow = step;
    $("#stepper")?.classList.toggle("is-final", step === 4);
    $$(".flow-page").forEach((page) => page.classList.toggle("is-active", Number(page.dataset.flow) === step));
    $$("#stepper li").forEach((item) => {
      const itemStep = Number(item.dataset.step);
      const done = state.completedSteps.has(itemStep);
      item.classList.toggle("is-current", itemStep === step);
      item.classList.toggle("is-done", done);
      const button = $("button", item);
      button.disabled = itemStep > state.unlockedFlow || (state.isRecoveredJob && itemStep < 3);
      button.toggleAttribute("aria-current", itemStep === step);
      $(".step-index", item).textContent = done ? "✓" : String(itemStep);
    });
    const hasTaskContext = Boolean(state.imported || state.jobId || state.jobResult || state.exportId || state.exportSnapshot);
    $("#abandon-task")?.classList.toggle("is-hidden", step < 2 || !hasTaskContext);
    if (!options.preserveScroll) window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function unlockFlow(step, options = {}) { state.unlockedFlow = Math.max(state.unlockedFlow, step); setFlow(state.currentFlow, options); }

  function resetCurrentTaskWorkspace() {
    state.jobLoadToken += 1;
    invalidateAdjustmentRun();
    if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
    if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
    state.jobPollTimer = null; state.exportPollTimer = null;
    stopPlayer();
    state.importFiles = []; state.imported = null; state.templateRules = new Map(); state.activeTemplate = null;
    state.batchPlan = newPlanModel(); state.precisionPlanSnapshot = null; state.editorScope = "batch"; state.seed = ""; state.seedWasAutoResolved = false;
    state.jobId = null; state.jobResult = null; state.jobSnapshot = null; state.jobStartedAt = null; state.jobPollFailures = 0; state.jobRecommendationPollAttempts = 0;
    state.isRecoveredJob = false; state.selectedCandidates = new Map(); state.activeWorkIndex = 0; state.candidatePage = 0;
    state.compareIds = new Set(); state.focusCandidate = null; state.focusReturnId = null; state.focusReturnPage = null; state.focusReturnWorkKey = null; state.candidateOverviewOpened = false;
    state.pendingUsePartialReview = false;
    state.adjustmentContext = null;
    state.stagedTemplateAdjustments = new Map();
    state.runningAdjustmentKeys = new Set();
    state.adjustmentDialog = null;
    const scopeLabel = $(".template-adjustment-heading .soft-label");
    if (scopeLabel) scopeLabel.textContent = "仅修改当前成品项";
    state.preflightSnapshot = null;
    state.lastParameterSuggestionUndo = null;
    state.parameterResolveToken += 1;
    state.parameterCalculationToken += 1;
    state.parameterSelectionId = null;
    state.parameterSelectionContext = null;
    state.exportLoadToken += 1; state.exportId = null; state.exportSnapshot = null; state.exportStartedAt = null; state.exportPollFailures = 0;
    state.completedSteps = new Set(); state.currentFlow = 1; state.unlockedFlow = 1;
    $("#template-ids").value = ""; $("#template-id-count").textContent = "已输入 0 项";
    $("#template-files").value = ""; $("#template-file-list").replaceChildren();
    $("#batch-seed").value = ""; $("#lock-seed").checked = false;
    switchImportTab("template_ids"); syncPlanForm(state.batchPlan);
    $("#template-matrix-body")?.replaceChildren(); $("#source-board")?.replaceChildren();
    state.sourcePreviewToken++; state.taskSourceItem = null; state.sourceLayerViews.clear();
    $("#config-source-controls")?.replaceChildren(); $("#task-source-preview")?.close();
    $("#generation-state")?.classList.remove("is-hidden"); $("#candidate-workspace")?.classList.add("is-hidden");
    $("#compare-workspace")?.classList.add("is-hidden"); $("#focus-workspace")?.classList.add("is-hidden");
    $(".export-panel")?.classList.remove("is-complete");
    $("#export-progress")?.classList.add("is-hidden"); $("#export-success")?.classList.add("is-hidden");
    $("#candidate-task-overview")?.removeAttribute("open");
    clearConfigPreflight();
    updateAppliedSuggestionNotice();
    renderActivityTimeline($("#generation-activity-log"), [], { emptyCopy: "任务尚未开始" });
    renderActivityTimeline($("#candidate-task-events"), []);
    renderActivityTimeline($("#export-activity-log"), [], { emptyCopy: "导出尚未开始" });
    navigate("create"); setFlow(1);
    window.setTimeout(() => $("#template-ids")?.focus(), 0);
  }
  function abandonCurrentTask() {
    const jobId = state.jobId;
    const jobCanCancel = Boolean(jobId && state.jobSnapshot?.actions?.can_cancel);
    const exportId = state.exportId;
    const exportCanCancel = Boolean(exportId && state.exportSnapshot?.actions?.can_cancel);
    const exportCommitting = state.exportSnapshot?.status === "committing";
    const requests = [];
    if (jobCanCancel) requests.push(api(`/jobs/${encodeURIComponent(jobId)}/control`, { method: "POST", body: { action: "cancel" } }));
    if (exportCanCancel) requests.push(api(`/exports/${encodeURIComponent(exportId)}/control`, { method: "POST", body: { action: "cancel" } }));
    resetCurrentTaskWorkspace();
    if (requests.length) {
      Promise.allSettled(requests).then((results) => {
        if (results.some((result) => result.status === "rejected")) toast("工作区已重置；部分后台任务未能确认终止，请在任务记录中查看。", "error");
      });
    }
    toast(exportCommitting
      ? "已重新开始创建；原导出已进入安全写入，会继续完成并保留在任务记录中。"
      : "已放弃当前工作区并重新开始；历史任务、已完成结果和输出文件均已保留。" );
  }

  function initStaticControls() {
    for (const selector of ["#unit-1-step", "#unit-2-step"]) {
      const select = $(selector);
      for (let step = 4; step <= 9; step += 1) {
        const option = document.createElement("option");
        option.value = String(step); option.textContent = `${step} 步`; select.append(option);
      }
    }
    $("#unit-1-step").value = "5"; $("#unit-2-step").value = "6";
    const typeRoot = $("#allowed-types");
    STRUCTURE_TYPES.forEach((type) => {
      const label = document.createElement("label"); label.className = "type-check";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = type; input.checked = true;
      const span = document.createElement("span"); span.textContent = type;
      label.append(input, span); typeRoot.append(label);
    });
    const layoutWrap = $("#layout-wrap");
    const serialLabel = document.createElement("label");
    serialLabel.id = "serial-difficulty-wrap"; serialLabel.className = "is-hidden";
    const title = document.createElement("span"); title.textContent = "垂直顺序";
    const select = document.createElement("select"); select.id = "serial-difficulty";
    [["harder_low", "更难谜题在低层"], ["easier_low", "更简单谜题在低层"], ["unrestricted", "不限制"]].forEach(([value, label]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option);
    });
    serialLabel.append(title, select); layoutWrap.append(serialLabel);
  }

  function renderProfile() {
    const profile = state.profile?.profile || {};
    $("#setting-template-path").value = profile.template_dir || "";
    $("#setting-output-path").value = profile.output_root || "";
    $("#setting-ulink-path").value = profile.ulinkconfig_root || "";
    $("#source-path-label").textContent = profile.template_dir || "首次使用模板 ID 前，请先在设置中选择 tile 仓库";
    $("#export-output-path").textContent = profile.output_root || "未配置";
    $("#ulink-path-label").textContent = profile.ulinkconfig_root || "首次使用时请选择";
    $("#unlaunched-ulink-path-label").textContent = profile.ulinkconfig_root || "首次使用时请选择";
    $("#environment-text").textContent = profile.tile_repo_root ? "本地 · 源模板只读" : "本地 · 待配置源路径";
    updateParameterResolveState();
  }

  async function refreshProfile() {
    const payload = await api("/profile"); state.profile = payload; renderProfile(); return payload;
  }

  function setPrimaryActionAvailability(button, enabled) {
    if (!button) return;
    button.disabled = !enabled;
    button.classList.toggle("primary-button", enabled);
    button.classList.toggle("secondary-button", !enabled);
    button.setAttribute("aria-disabled", String(!enabled));
  }
  function updateImportActionState() {
    const valid = state.importMode === "template_ids"
      ? hasValidTemplateIds($("#template-ids")?.value || "")
      : hasValidTemplateFiles(state.importFiles);
    setPrimaryActionAvailability($("#inspect-templates"), valid);
  }
  function updateParameterResolveState() {
    const sourceValid = state.parameterSource === "output_ids"
      ? hasValidTemplateIds($("#parameter-template-ids")?.value || "")
      : state.parameterSource === "files"
        ? hasValidTemplateFiles(state.parameterFiles)
        : Boolean(state.recentExport?.result?.outputs?.length);
    const hasUnlaunchedSource = state.parameterMode !== "unlaunched"
      || Boolean(state.profile?.profile?.ulinkconfig_root);
    setPrimaryActionAvailability($("#resolve-parameter-templates"), sourceValid && hasUnlaunchedSource);
  }
  function updateCalibrationResolveState() {
    const sourceValid = state.calibrationSource === "output_ids"
      ? hasValidTemplateIds($("#calibration-template-ids")?.value || "")
      : state.calibrationSource === "files"
        ? hasValidTemplateFiles(state.calibrationFiles)
        : Boolean(state.recentExport?.result?.outputs?.length);
    setPrimaryActionAvailability(
      $("#resolve-calibration-templates"),
      sourceValid && Boolean(state.profile?.profile?.ulinkconfig_root),
    );
  }

  function switchImportTab(mode) {
    state.importMode = mode;
    $$('[data-import-tab]').forEach((button) => {
      const active = button.dataset.importTab === mode;
      button.classList.toggle("is-selected", active); button.setAttribute("aria-selected", String(active));
    });
    $$('[data-import-panel]').forEach((panel) => panel.classList.toggle("is-active", panel.dataset.importPanel === mode));
    updateImportActionState();
  }

  async function filesToPayload(files) {
    const values = [];
    for (const file of files) {
      if (!file.name.endsWith("_m.bytes")) throw new Error(`文件名必须以 _m.bytes 结尾：${file.name}`);
      if (!(Number(file.size) > 0)) throw new Error(`不能上传空文件：${file.name}`);
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
      values.push({ name: file.name, data_base64: btoa(binary) });
    }
    return values;
  }
  function renderFileList(rootSelector, files) {
    const root = $(rootSelector); root.replaceChildren();
    files.forEach((file) => {
      const row = document.createElement("div"); row.className = "resolved-template-row";
      const name = document.createElement("span"); name.textContent = file.name;
      const size = document.createElement("span"); size.textContent = Number(file.size) > 0 ? `${Math.max(1, Math.round(file.size / 1024))} KB` : "空文件";
      row.append(name, size); root.append(row);
    });
  }

  async function inspectTemplates() {
    const button = $("#inspect-templates");
    try {
      setBusy(button, true, "正在解析…");
      let body;
      if (state.importMode === "template_ids") {
        const ids = uniqueLines($("#template-ids").value);
        if (!ids.length) throw new Error("请至少输入一个模板 ID。");
        if (!ids.every(isValidTemplateId)) throw new Error("模板 ID 只能包含英文字母、数字、下划线和连字符，且长度不超过 180 个字符。");
        body = { mode: "template_ids", template_ids: ids };
      } else {
        if (!state.importFiles.length) throw new Error("请选择至少一个 _m.bytes 文件。");
        body = { mode: "files", files: await filesToPayload(state.importFiles) };
      }
      const payload = await api("/templates/inspect", { method: "POST", body });
      state.isRecoveredJob = false;
      state.imported = payload;
      state.templateRules = new Map(payload.items.map((item) => [item.template_key, { mode: "use_batch", plan: clone(state.batchPlan) }]));
      state.activeTemplate = payload.items[0]?.template_key || null;
      state.completedSteps.add(1); unlockFlow(2); renderConfiguration(); setFlow(2);
      toast(`已解析 ${payload.items.length} 个模板，源文件未修改。`);
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); }
  }

  function currentEditedPlan() {
    if (state.adjustmentDialog?.draft) return state.adjustmentDialog.draft;
    if (state.editorScope === "batch") return state.batchPlan;
    const rule = state.templateRules.get(state.activeTemplate);
    return !rule || ["use_batch", "skip"].includes(rule.mode) ? state.batchPlan : rule.plan;
  }
  function syncPlanForm(plan) {
    state.syncingForm = true;
    $("#unit-count").value = String(plan.unitCount);
    $("#candidate-count").value = String(plan.candidateCount);
    $("#search-attempt-budget").value = String(Math.max(1, Math.round(Number(plan.searchAttemptBudget) || 2000)));
    const difficultyTarget = ["fidelity", "standard", "strong"].includes(plan.difficultyTarget) ? plan.difficultyTarget : "fidelity";
    const difficultyTargetInput = $(`input[name="difficulty-target"][value="${difficultyTarget}"]`);
    if (difficultyTargetInput) difficultyTargetInput.checked = true;
    $("#unit-1-step").value = String(plan.steps[0]); $("#unit-2-step").value = String(plan.steps[1]);
    $("#required-pre-gate-recycles").value = String(Math.max(0, Math.round(Number(plan.requiredPreGateRecycles) || 0)));
    $("#first-recycle-lead-tiles").value = String(Math.max(0, Math.round(Number(plan.firstRecycleLeadTiles) || 0)));
    $("#layout").value = plan.layout; $("#tower-mode").value = plan.towerMode;
    const replacementSourceValue = Array.isArray(plan.replacementSources)
      ? plan.replacementSources.join("")
      : plan.replacementSources;
    $("#replacement-sources").value = replacementSourceValue;
    const structureGroupMode = plan.structureGroupMode === "custom" ? "custom" : "standard";
    const groupModeInput = $(`input[name="structure-group-mode"][value="${structureGroupMode}"]`);
    if (groupModeInput) groupModeInput.checked = true;
    $("#type-count-kind").value = plan.typeCountKind === "exact" ? "exact" : "range";
    $("#type-count-min").value = String(plan.typeMin); $("#type-count-max").value = String(plan.typeMax);
    const rawUniform = Number(plan.uniformGroupsPerType);
    $("#uniform-groups-per-type").value = String(Number.isFinite(rawUniform) ? Math.max(0, Math.round(rawUniform)) : 1);
    $("#automatic-group-max-difference").value = String(plan.automaticGroupMaxDifference ?? 3);
    $("#custom-groups-per-type").value = String(plan.customGroupsPerType || "2；1；1");
    $("#max-layer-kind").value = plan.maxLayerKind; $("#max-layer-value").value = String(plan.maxLayerValue);
    $("#total-limit-kind").value = plan.totalLimitKind; $("#total-limit-value").value = String(plan.totalLimitValue);
    $("#total-count-minimum").value = String(plan.totalCountMinimum ?? 0);
    $("#total-minimum-kind").value = plan.totalMinimumKind ?? "absolute";
    $("#max-structure-layer-span-kind").value = plan.maxStructureLayerSpanKind || "none";
    $("#min-same-type-layer-span").value = String(plan.minSameTypeLayerSpan ?? 0);
    $("#max-structure-layer-span").value = String(Math.max(0, Math.round(Number(plan.maxStructureLayerSpan) || 0)));
    $("#quality-position").value = plan.quality.position; $("#quality-symmetry").value = plan.quality.symmetry; $("#quality-density").value = plan.quality.density;
    $("#experience-archetype").value = ["auto", "intrinsic_gate", "board_reveal", "hybrid"].includes(plan.quality.experience_archetype) ? plan.quality.experience_archetype : "auto";
    $("#special-preset").value = ["none", "all_types_long_range", "serial_mechanism"].includes(plan.quality.special_preset) ? plan.quality.special_preset : "none";
    $("#long-range-wave-count").value = ["auto", "3", "4"].includes(String(plan.quality.long_range_wave_count ?? "auto")) ? String(plan.quality.long_range_wave_count ?? "auto") : "auto";
    $("#online-style-reference").value = ["level_200_300_v1", "off"].includes(plan.quality.online_style_reference) ? plan.quality.online_style_reference : "level_200_300_v1";
    $("#serial-difficulty").value = plan.serialDifficulty;
    $$("#allowed-types input").forEach((input) => { input.checked = plan.allowedTypes.includes(input.value); });
    refreshConditionalPlanFields(); state.syncingForm = false;
  }
  function capturePlanForm(event) {
    if (state.syncingForm) return;
    const editingCandidateDraft = Boolean(state.adjustmentDialog?.draft);
    if (!editingCandidateDraft) invalidateGenerationForPlanChange();
    const plan = currentEditedPlan();
    plan.unitCount = Number($("#unit-count").value);
    plan.candidateCount = clamp(Math.round(numberValue("#candidate-count", 6)), 1, 20);
    plan.searchAttemptBudget = clamp(Math.round(numberValue("#search-attempt-budget", 2000)), 1, 100000);
    plan.difficultyTarget = $('input[name="difficulty-target"]:checked')?.value || "fidelity";
    plan.steps = [Number($("#unit-1-step").value), Number($("#unit-2-step").value)];
    plan.requiredPreGateRecycles = Math.max(0, Math.round(numberValue("#required-pre-gate-recycles", 0)));
    plan.firstRecycleLeadTiles = Math.max(0, Math.round(numberValue("#first-recycle-lead-tiles", 2)));
    plan.layout = $("#layout").value; plan.towerMode = $("#tower-mode").value;
    plan.serialDifficulty = $("#serial-difficulty").value;
    const replacementSourceValue = $("#replacement-sources").value;
    plan.replacementSources = [MAJORITY_A_ON_TIE, LEGACY_PREFER_A_ELSE_B].includes(replacementSourceValue)
      ? replacementSourceValue
      : replacementSourceValue.split("");
    if (["type-count-kind", "type-count-min", "type-count-max"].includes(event?.target?.id)) {
      plan.legacyTypeCountKind = "";
    }
    plan.typeCountKind = $("#type-count-kind").value;
    plan.typeMin = clamp(Math.round(numberValue("#type-count-min", 3)), 1, 12);
    plan.typeMax = clamp(Math.round(numberValue("#type-count-max", plan.typeMin)), 1, 12);
    plan.structureGroupMode = $('input[name="structure-group-mode"]:checked')?.value || "standard";
    plan.uniformGroupsPerType = Math.max(0, Math.round(numberValue("#uniform-groups-per-type", 1)));
    plan.automaticGroupMaxDifference = numberValue("#automatic-group-max-difference", 3);
    plan.customGroupsPerType = $("#custom-groups-per-type").value;
    plan.maxLayerKind = $("#max-layer-kind").value; plan.maxLayerValue = numberValue("#max-layer-value", 50);
    plan.totalLimitKind = $("#total-limit-kind").value; plan.totalLimitValue = numberValue("#total-limit-value", 35);
    plan.totalCountMinimum = numberValue("#total-count-minimum", 0);
    plan.totalMinimumKind = $("#total-minimum-kind").value;
    plan.maxStructureLayerSpanKind = $("#max-structure-layer-span-kind").value;
    plan.minSameTypeLayerSpan = numberValue("#min-same-type-layer-span", 2);
    plan.maxStructureLayerSpan = Math.max(0, Math.round(numberValue("#max-structure-layer-span", 5)));
    plan.allowedTypes = $$("#allowed-types input:checked").map((input) => input.value);
    plan.quality = {
      position: $("#quality-position").value,
      symmetry: $("#quality-symmetry").value,
      density: $("#quality-density").value,
      experience_archetype: $("#experience-archetype").value,
      special_preset: $("#special-preset").value,
      long_range_wave_count: $("#long-range-wave-count").value,
      online_style_reference: $("#online-style-reference").value,
    };
    refreshConditionalPlanFields();
    if (editingCandidateDraft) {
      const summary = $("#template-adjustment-context");
      if (summary) summary.textContent = `${state.adjustmentDialog.itemScope} · ${planHuman(plan)} · ${plan.candidateCount} 个候选 · ${planRecycleCopy(plan)}`;
      updateContinuationTargetLabels();
      return;
    }
    renderConfigurationSummary(); renderMatrix(); renderSourceBoard();
  }
  function invalidateGenerationForPlanChange() {
    clearConfigPreflight();
    if (state.lastParameterSuggestionUndo?.source === "preflight") state.lastParameterSuggestionUndo = null;
    if (!state.jobId && !state.jobResult) return;
    if (state.adjustmentContext?.parentJobId === state.jobId) {
      state.completedSteps.delete(2); state.completedSteps.delete(3); state.completedSteps.delete(4); state.unlockedFlow = 2;
      return;
    }
    state.jobId = null; state.jobResult = null; state.selectedCandidates.clear(); state.compareIds.clear(); state.focusCandidate = null; stopPlayer();
    state.completedSteps.delete(2); state.completedSteps.delete(3); state.completedSteps.delete(4); state.unlockedFlow = 2;
    $("#export-success").classList.add("is-hidden");
    toast("方案已变化，旧候选仍保留在任务记录中；请重新生成当前方案。" );
  }
  function refreshSpecialPresetAvailability(plan = currentEditedPlan()) {
    const select = $("#special-preset");
    const waveWrap = $("#long-range-wave-count-wrap");
    const waveSelect = $("#long-range-wave-count");
    const help = $("#special-preset-help");
    if (!select || !waveWrap || !waveSelect || !help) return;
    const labels = {
      none: "不使用特殊预设",
      all_types_long_range: "全类型长程型",
      serial_mechanism: "串行机关型",
    };
    const compatibilities = Object.fromEntries(
      Object.keys(labels).map((preset) => [preset, specialPresetCompatibility(plan, preset)])
    );
    Object.entries(compatibilities).forEach(([preset, compatibility]) => {
      const option = select.querySelector(`option[value="${preset}"]`);
      if (!option) return;
      option.disabled = !compatibility.ok;
      option.textContent = compatibility.ok || preset === "none"
        ? labels[preset]
        : `${labels[preset]}（${compatibility.reason}）`;
    });
    const requested = select.value;
    const requestedCompatibility = compatibilities[requested] || { ok: false, reason: "未知预设" };
    let resetReason = "";
    if (!requestedCompatibility.ok) {
      resetReason = `${labels[requested] || "原特殊预设"}已关闭：${requestedCompatibility.reason}。`;
      select.value = "none";
      plan.quality.special_preset = "none";
      plan.quality.long_range_wave_count = "auto";
      waveSelect.value = "auto";
    }
    const activePreset = select.value;
    const isLongRange = activePreset === "all_types_long_range";
    waveWrap.classList.toggle("is-hidden", !isLongRange);
    waveSelect.disabled = !isLongRange;
    if (!isLongRange) {
      plan.quality.long_range_wave_count = "auto";
      waveSelect.value = "auto";
    }
    const unavailable = ["all_types_long_range", "serial_mechanism"]
      .filter((preset) => !compatibilities[preset].ok)
      .map((preset) => `${labels[preset]}不可用（${compatibilities[preset].reason}）`);
    if (activePreset === "all_types_long_range") {
      help.textContent = "全类型长程型强制 C–N 12 类×3，并按所选 3/4 波语法生成；当前尚未证明悬置 1/2 张的整板持续上限，因此不会通过严格导出。";
    } else if (activePreset === "serial_mechanism") {
      help.textContent = "串行机关型强制 Z 向 high→low 双单元，上一簇完成后才允许进入下一簇。";
    } else {
      help.textContent = `${resetReason}${unavailable.length ? unavailable.join("；") : "当前方案可使用两个特殊预设"}。`;
    }
    syncSpecialPresetEffectiveConfiguration(plan);
  }
  function syncStructureCountMode(plan = currentEditedPlan()) {
    const overridden = plan.quality?.special_preset === "all_types_long_range";
    const mode = plan.structureGroupMode === "custom" ? "custom" : "standard";
    const standardPanel = $("#standard-structure-count-panel");
    const customPanel = $("#custom-structure-count-panel");
    $("#structure-count-mode-fieldset")?.classList.toggle("is-hidden", overridden);
    standardPanel?.classList.toggle("is-hidden", overridden || mode !== "standard");
    customPanel?.classList.toggle("is-hidden", overridden || mode !== "custom");
    $$('input[name="structure-group-mode"]').forEach((input) => { input.disabled = overridden; });
    ["#type-count-kind", "#type-count-min", "#type-count-max", "#uniform-groups-per-type", "#total-limit-kind", "#total-limit-value", "#total-count-minimum", "#total-minimum-kind"].forEach((selector) => {
      const control = $(selector);
      if (control) control.disabled = overridden || mode !== "standard" || (selector === "#type-count-max" && plan.typeCountKind === "exact") || (selector === "#total-limit-value" && plan.totalLimitKind === "none") || (selector === "#total-count-minimum" && plan.totalMinimumKind === "none");
    });
    if ($("#custom-groups-per-type")) $("#custom-groups-per-type").disabled = overridden || mode !== "custom";
    const differenceControl = $("#automatic-group-max-difference");
    if (differenceControl) differenceControl.disabled = overridden || mode !== "standard" || Number(plan.uniformGroupsPerType) !== 0;

    const parsed = parseStructureGroupVector(plan.customGroupsPerType);
    announce(
      "#custom-groups-summary",
      parsed.error ? parsed.error : `${parsed.values.length} 种 · ${parsed.values.reduce((sum, value) => sum + value, 0)} 组 · ${parsed.values.reduce((sum, value) => sum + value, 0) * 3} 格 / 谜题单元`,
    );
  }
  function syncSpecialPresetEffectiveConfiguration(plan = currentEditedPlan()) {
    const effective = effectiveStructureTypeConfiguration(plan);
    const overridden = effective.overridden;
    const typeRow = $("#type-count-row");
    const effectivePanel = $("#all-types-effective-config");
    const allowedTypesFieldset = $("#allowed-types-fieldset");
    typeRow?.classList.toggle("is-hidden", overridden || plan.structureGroupMode === "custom");
    effectivePanel?.classList.toggle("is-hidden", !overridden);
    allowedTypesFieldset?.classList.toggle("is-hidden", overridden);
    $$("#allowed-types input").forEach((input) => { input.disabled = overridden; });
    syncStructureCountMode(plan);
  }
  function refreshConditionalPlanFields() {
    const count = Number($("#unit-count").value);
    const towerOnly = $("#tower-mode").value === "only";
    $("#unit-2-step-wrap").classList.toggle("is-hidden", count !== 2 || towerOnly);
    $("#layout-wrap").classList.toggle("is-hidden", count !== 2 || towerOnly);
    $("#serial-difficulty-wrap").classList.toggle("is-hidden", count !== 2 || towerOnly || $("#layout").value !== "z");
    refreshSpecialPresetAvailability();
    $("#required-pre-gate-recycles-wrap").classList.toggle("is-hidden", towerOnly);
    const recycles = Math.max(0, Math.round(numberValue("#required-pre-gate-recycles", 0)));
    const leadWrap = $("#first-recycle-lead-wrap");
    const leadInput = $("#first-recycle-lead-tiles");
    const showLead = !towerOnly && recycles > 0;
    leadWrap.classList.toggle("is-hidden", !showLead);
    $("#recycle-rule-row").classList.toggle("is-hidden", towerOnly || (count !== 2 && !showLead));
    leadInput.disabled = !showLead;
    leadInput.required = showLead;
    $("#required-pre-gate-recycles").setAttribute("aria-expanded", String(showLead));
    $("#type-count-max-wrap").classList.toggle("is-hidden", $("#type-count-kind").value === "exact");
    $("#type-min-label").textContent = $("#type-count-kind").value === "exact" ? "种类数" : "最少";
    const layerKind = $("#max-layer-kind").value;
    $("#max-layer-value").disabled = layerKind === "none"; $("#max-layer-unit").textContent = layerKind === "percent" ? "%" : layerKind === "absolute" ? "层" : "—";
    const limitKind = $("#total-limit-kind").value;
    const customGroupMode = $('input[name="structure-group-mode"]:checked')?.value === "custom";
    $("#total-limit-kind").disabled = customGroupMode;
    const minimumKind = $("#total-minimum-kind").value;
    $("#total-minimum-kind").disabled = customGroupMode;
    $("#total-count-minimum").disabled = customGroupMode || minimumKind === "none";
    $("#total-minimum-unit").textContent = minimumKind === "percent" ? "%" : minimumKind === "absolute" ? "格" : "—";
    $("#total-count-minimum").step = minimumKind === "percent" ? "any" : "1";
    if (minimumKind === "percent") $("#total-count-minimum").max = "100";
    else $("#total-count-minimum").removeAttribute("max");
    $("#total-limit-value").disabled = customGroupMode || limitKind === "none"; $("#total-limit-unit").textContent = limitKind === "percent" ? "%" : limitKind === "absolute" ? "格" : "—";
    const disallowTower = $("#tower-mode").value === "disallow";
    $("#total-limit-value").max = "100";
    const spanKind = $("#max-structure-layer-span-kind").value;
    const spanInput = $("#max-structure-layer-span");
    spanInput.disabled = spanKind === "none";
    syncStructureCountMode(currentEditedPlan());
    const scope = $("#constraint-scope");
    scope.classList.remove("is-warning");
    const item = state.adjustmentDialog?.item || activeImportedItem();
    const inspection = item?.inspection || {};
    const layerBase = Number(disallowTower ? inspection.non_tower_max_layer : inspection.max_layer);
    scope.textContent = "";
    if (Number.isFinite(layerBase) && layerBase > 0) {
      const effectiveLayer = layerKind === "percent"
        ? Math.floor(layerBase * clamp(numberValue("#max-layer-value", 50), 1, 100) / 100)
        : layerKind === "absolute" ? Math.min(layerBase, Math.max(1, Math.floor(numberValue("#max-layer-value", 1)))) : layerBase;
      scope.textContent = effectiveLayer > 0 ? `在当前参数设置下，结构块最高至第 ${effectiveLayer} 层。` : "在当前参数设置下，没有可用的结构层。";
      scope.classList.toggle("is-warning", effectiveLayer <= 0);
    } else if (disallowTower && item && Number(inspection.non_tower_tile_count) === 0) {
      scope.textContent = `${item.template_key} 没有可用的非塔 Tile，当前“禁止塔内”模式无法生成结构。`;
      scope.classList.add("is-warning");
    }
    scope.classList.toggle("is-hidden", !scope.textContent);
  }

  function editBatchPlan() {
    state.editorScope = "batch";
    $("#editor-scope-label").textContent = "批次统一方案"; $("#editor-title").textContent = "默认成品方案";
    $("#template-mode-block").classList.add("is-hidden"); $("#close-template-editor").classList.add("is-hidden");
    $("#plan-fields").classList.remove("is-hidden"); syncPlanForm(state.batchPlan); updateAppliedSuggestionNotice(); renderConfigurationSummary();
  }
  function editTemplatePlan(templateKey) {
    state.activeTemplate = templateKey; state.editorScope = "template";
    const rule = state.templateRules.get(templateKey);
    $("#editor-scope-label").textContent = "当前模板"; $("#editor-title").textContent = templateKey;
    $("#template-mode-block").classList.remove("is-hidden"); $("#close-template-editor").classList.remove("is-hidden");
    $("#template-plan-mode").value = rule.mode; $("#template-mode-help").textContent = PLAN_MODE_COPY[rule.mode];
    $("#plan-fields").classList.toggle("is-hidden", ["use_batch", "skip"].includes(rule.mode));
    syncPlanForm(rule.plan); updateAppliedSuggestionNotice(); renderMatrix(); renderSourceBoard(); renderConfigurationSummary();
  }
  function renderConfiguration() { editBatchPlan(); renderMatrix(); renderSourceBoard(); renderConfigurationSummary(); }
  function syncPrecisionModeUi() {
    const enabled = Boolean(state.batchPlan.precisionMode);
    const toggle = $("#precision-mode-toggle");
    toggle?.classList.toggle("is-active", enabled);
    toggle?.setAttribute("aria-pressed", String(enabled));
    if (toggle) toggle.textContent = enabled ? "退出精准模式" : "进入精准模式";
    const generate = $("#generate-candidates");
    if (generate) {
      generate.innerHTML = enabled
        ? `确认清单并进行精准生成${icon("arrow")}`
        : `确认清单并生成${icon("arrow")}`;
    }
  }
  function applyPrecisionDefaults(plan) {
    plan.candidateCount = 3;
    plan.searchAttemptBudget = 20000;
    plan.maxStructureLayerSpanKind = "absolute";
    plan.maxStructureLayerSpan = 5;
    plan.quality.position = "unrestricted";
    plan.quality.symmetry = "off";
    plan.quality.density = "compact";
  }
  function applyStandardModeDefaults(plan) {
    plan.precisionMode = false;
    plan.candidateCount = 6;
    plan.searchAttemptBudget = 2000;
    plan.maxStructureLayerSpanKind = "absolute";
    plan.maxStructureLayerSpan = 5;
    plan.quality.position = "unrestricted";
    plan.quality.symmetry = "off";
    plan.quality.density = "compact";
  }
  function capturePrecisionPlanSnapshot() {
    state.precisionPlanSnapshot = {
      batchPlan: clone(state.batchPlan),
      templatePlans: new Map([...state.templateRules].map(([templateKey, rule]) => [
        templateKey,
        rule?.plan ? clone(rule.plan) : null,
      ])),
    };
  }
  function restorePrePrecisionPlans() {
    const snapshot = state.precisionPlanSnapshot;
    if (snapshot?.batchPlan) {
      state.batchPlan = clone(snapshot.batchPlan);
    } else {
      applyStandardModeDefaults(state.batchPlan);
    }
    for (const [templateKey, rule] of state.templateRules) {
      if (!rule?.plan) continue;
      const previousPlan = snapshot?.templatePlans?.get(templateKey);
      if (previousPlan) rule.plan = clone(previousPlan);
      else applyStandardModeDefaults(rule.plan);
    }
    state.precisionPlanSnapshot = null;
  }
  function setPrecisionMode(enabled) {
    invalidateGenerationForPlanChange();
    if (enabled) {
      if (!state.batchPlan.precisionMode) capturePrecisionPlanSnapshot();
      state.batchPlan.precisionMode = true;
      applyPrecisionDefaults(state.batchPlan);
      for (const rule of state.templateRules.values()) {
        if (!rule?.plan) continue;
        rule.plan.precisionMode = true;
        applyPrecisionDefaults(rule.plan);
      }
    } else {
      restorePrePrecisionPlans();
    }
    editBatchPlan();
    syncPrecisionModeUi();
    toast(enabled
      ? "已进入精准模式：只保留参数、难度目标与机制压力全部达标的候选。"
      : "已退出精准模式：已恢复进入前的统一方案与模板单独方案。"
    );
  }
  function renderConfigurationSummary() {
    const plan = state.batchPlan;
    $("#batch-summary-text").textContent = `${plan.precisionMode ? "精准模式 · " : ""}${planHuman(plan)} · ${plan.candidateCount} 个候选 · ${planLayerCopy(plan)} · ${planRecycleCopy(plan)}`;
    const active = currentEditedPlan();
    $("#drawer-summary").textContent = active.towerMode === "only"
      ? `每份提供 ${active.candidateCount} 个候选；塔内均匀分布不计算 sN。`
      : `最终将生成 ${planHuman(active)}，每份提供 ${active.candidateCount} 个候选；${planRecycleCopy(active)}。`;
    const count = state.imported?.items?.length || 0;
    const outputs = [...state.templateRules.values()].reduce((sum, rule) => sum + (rule.mode === "skip" ? 0 : rule.mode === "append" ? 2 : 1), 0);
    $("#work-summary").textContent = `${count} 个模板 · ${outputs} 份成品方案`;
    syncPrecisionModeUi();
  }
  function renderMatrix() {
    const root = $("#template-matrix-body"); root.replaceChildren();
    const items = state.imported?.items || [];
    $("#matrix-count").textContent = `${items.length} 个模板`;
    items.forEach((item) => {
      const rule = state.templateRules.get(item.template_key) || { mode: "use_batch", plan: state.batchPlan };
      const row = document.createElement("tr"); row.dataset.templateKey = item.template_key;
      row.classList.toggle("is-selected", item.template_key === state.activeTemplate);
      const effective = rule.mode === "use_batch" ? state.batchPlan : rule.plan;
      let primary = planSignature(effective), note = "使用统一方案", outputs = 1, status = "已配置";
      if (rule.mode === "replace") { note = "仅本模板 · 替代统一方案"; status = "已单独设置"; }
      if (rule.mode === "append") { primary = `${planSignature(state.batchPlan)} + ${planSignature(rule.plan)}`; note = "保留统一方案，并新增 1 份成品"; outputs = 2; status = "已追加"; }
      if (rule.mode === "skip") { primary = "跳过"; note = "本批次不生成"; outputs = 0; status = "已跳过"; }
      const inspection = item.inspection || {};
      row.innerHTML = `<td><strong>${escapeHtml(item.template_key)}</strong><small>Tile 总数：${escapeHtml(inspection.tile_count ?? "—")} · 非塔 Tile：${escapeHtml(inspection.non_tower_tile_count ?? "—")} · 非塔层数：${escapeHtml(inspection.non_tower_max_layer ?? "—")} 层</small></td><td><strong>${escapeHtml(primary)}</strong><small>${escapeHtml(note)}</small></td><td>${outputs}</td><td><span class="status-chip${rule.mode === "skip" ? "" : " success"}">${escapeHtml(status)}</span></td>`;
      root.append(row);
    });
  }
  function activeImportedItem() { return state.imported?.items?.find((item) => item.template_key === state.activeTemplate) || state.imported?.items?.[0] || null; }
  function renderSourceBoard() {
    const item = activeImportedItem();
    $("#source-board-title").textContent = item?.template_key || "—";
    renderSourceLayers("config", item);
    refreshConditionalPlanFields();
  }
  function renderSourceLayers(context, item) {
    const controls = $(`#${context}-source-controls`);
    const boardRoot = $(context === "config" ? "#source-board" : "#task-source-board");
    const key = `${context}:${context === "task" ? state.jobId : state.imported?.import_id}:${item?.template_key}`;
    const maximum = Math.max(1, ...(item?.board?.tiles || []).map(tile => Number(tile.layer || tile.z + 1)));
    const view = state.sourceLayerViews.get(key) || {mode:"all",layer:maximum};
    view.layer = clamp(view.layer, 1, maximum);
    state.sourceLayerViews.set(key, view);
    const inspection = item?.inspection || {};
    controls.innerHTML = `<p class="source-facts">Tile 总数 ${escapeHtml(inspection.tile_count ?? "—")} · 非塔 Tile ${escapeHtml(inspection.non_tower_tile_count ?? "—")} · 非塔层数 ${escapeHtml(inspection.non_tower_max_layer ?? "—")}</p><div class="source-layer-controls" data-source-context="${context}" data-source-view-key="${escapeHtml(key)}"><label>显示方式<select data-source-layer-mode aria-label="原模板显示方式"><option value="all">全部层</option><option value="single">仅当前层</option><option value="through">第 1 层至当前层</option></select></label><button type="button" class="secondary-button" data-source-layer-step="-1" ${view.layer<=1?'disabled':''}>上一层</button><label>当前层<input type="range" min="1" max="${maximum}" value="${view.layer}" data-source-layer-range aria-label="原模板当前层"></label><output>第 ${view.layer} / ${maximum} 层</output><button type="button" class="secondary-button" data-source-layer-step="1" ${view.layer>=maximum?'disabled':''}>下一层</button></div>`;
    $("select", controls).value = view.mode;
    renderBoard(boardRoot, item?.board, {source:true,
      plan: context === "config" ? currentEditedPlan() : null,
      minLayer:view.mode === "single" ? view.layer : 1,
      maxLayer:view.mode === "all" ? maximum : view.layer,
      highlightInitialVisibility:true});
  }
  function changeSourceLayer(control, delta = null) {
    const group = control.closest("[data-source-context]");
    if (!group) return;
    const view = state.sourceLayerViews.get(group.dataset.sourceViewKey);
    if (!view) return;
    if (control.matches("[data-source-layer-mode]")) view.mode = control.value;
    else { view.layer = delta === null ? Number(control.value) : view.layer + delta; if (view.mode === "all") view.mode = "through"; }
    const context = group.dataset.sourceContext;
    renderSourceLayers(context, context === "config" ? activeImportedItem() : state.taskSourceItem);
    // Re-rendered controls retain keyboard focus, including range-arrow input.
    const selector = control.matches("[data-source-layer-mode]") ? "[data-source-layer-mode]" : delta === null ? "[data-source-layer-range]" : `[data-source-layer-step="${delta}"]`;
    $(`#${context}-source-controls ${selector}`)?.focus({preventScroll:true});
  }
  async function showTaskSource(templateKey) {
    const jobId = state.jobId; const token = ++state.sourcePreviewToken;
    const panel = $("#task-source-preview"); panel.classList.remove("is-hidden");
    if (!panel.open) panel.showModal();
    panel.dataset.jobId = jobId;
    $("#task-source-title").textContent = `原模板 · ${templateKey}`;
    $("#task-source-controls").replaceChildren();
    $("#task-source-board").textContent = "正在读取本任务的原模板…";
    try {
      const imported = state.imported?.import_id === state.jobSnapshot?.import_id ? state.imported?.items?.find(item => item.template_key === templateKey) : null;
      const item = imported || await api(`/jobs/${encodeURIComponent(jobId)}/source/${encodeURIComponent(templateKey)}`);
      if (token !== state.sourcePreviewToken || state.jobId !== jobId) return;
      state.taskSourceItem = item;
      renderSourceLayers("task", item);
      $("#task-source-title").focus({preventScroll:true});
    } catch (error) {
      if (token !== state.sourcePreviewToken || state.jobId !== jobId) return;
      $("#task-source-board").textContent = `无法读取原模板：${error.message}。若本次服务尚未更新，请先保留候选，勿直接重启。`;
    }
  }

  async function startGeneration() {
    const button = $("#generate-candidates");
    try {
      capturePlanForm();
      const request = buildBatchRequest({ allowRecoverableRecycleConflicts: true });
      setBusy(button, true, "正在校验配置…");
      const preflight = await api("/plans/resolve", {
        method: "POST",
        body: { import_id: state.imported.import_id, batch_request: request },
      });
      if (preflight?.valid === false) {
        renderConfigPreflight(preflight);
        toast("当前配置尚不能生成；已给出针对当前模板的可验证修改建议。", "error");
        return;
      }
      clearConfigPreflight();
      const parentContext = state.adjustmentContext;
      const inheritedResult = parentContext
        ? (state.jobResult || availableResultForJob(state.jobSnapshot))
        : null;
      const inheritedSelections = new Map([...state.selectedCandidates].map(([key, value]) => [key, [...value]]));
      const createBody = { import_id: state.imported.import_id, batch_request: request };
      if (parentContext?.parentJobId) {
        createBody.parent_job_id = parentContext.parentJobId;
        createBody.retry_scope = { template_key: parentContext.templateKey, plan_id: parentContext.planId || "" };
      }
      const job = await api("/jobs", { method: "POST", body: createBody });
      if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
      state.jobId = job.job_id; state.jobResult = inheritedResult; state.jobSnapshot = job;
      state.jobRecommendationPollAttempts = 0;
      state.pendingUsePartialReview = false;
      state.isRecoveredJob = false;
      state.jobStartedAt = job.started_at || job.created_at || new Date().toISOString();
      state.selectedCandidates = parentContext ? inheritedSelections : new Map();
      state.selectionsByJob.set(state.jobId, state.selectedCandidates); persistSelections(state.jobId);
      state.compareIds.clear(); state.candidateOverviewOpened = false;
      state.adjustmentContext = null;
      $("#candidate-task-overview")?.removeAttribute("open");
      state.completedSteps.add(2); unlockFlow(3); setFlow(3);
      $("#generation-state").classList.remove("is-hidden"); $("#candidate-workspace").classList.add("is-hidden");
      $("#compare-workspace").classList.add("is-hidden"); $("#focus-workspace").classList.add("is-hidden");
      renderGenerationProgress(job);
      pollJob(job.job_id);
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); }
  }
  function generationPhase(job) {
    const phase = job?.control?.phase || "";
    const { finished, success } = jobProgressMetrics(job);
    const lastCompletedItem = latestEvent(job?.events, "work_item_progress");
    if (phase === "generation_complete") return { index: 3, copy: "整理候选结果" };
    if (phase === "generation_work_item") {
      return success > 0 && lastCompletedItem
        ? { index: 2, copy: `已有 ${success} 项通过硬校验，准备下一项` }
        : finished > 0
          ? { index: 1, copy: "上一成品项未通过，继续搜索下一项" }
        : { index: 0, copy: "准备当前模板" };
    }
    if (["generation_candidate", "search_attempt", "assignment_attempt"].includes(phase)) {
      return { index: 1, copy: "结构搜索与独立校验" };
    }
    if (lastCompletedItem && success > 0) return { index: 2, copy: `已完成 ${success} 项独立硬校验` };
    return { index: 0, copy: job?.status === "queued" ? "等待任务开始" : "准备任务" };
  }
  function renderGenerationProgress(job) {
    $(".generation-hero").classList.toggle("is-hidden", job.status === "completed");
    $("#collapse-candidates").classList.toggle("is-hidden", job.status === "completed");
    const sourcePanel = $("#task-source-preview");
    if (sourcePanel?.dataset.jobId && sourcePanel.dataset.jobId !== job.job_id) {
      state.sourcePreviewToken++; state.taskSourceItem = null;
      sourcePanel.close(); sourcePanel.classList.add("is-hidden"); delete sourcePanel.dataset.jobId;
    }
    state.jobSnapshot = job;
    state.jobStartedAt = job.started_at || state.jobStartedAt || job.created_at || new Date().toISOString();
    const { total, success, terminalFailures, finished } = jobProgressMetrics(job);
    const availableResult = availableResultForJob(job);
    const resultItems = availableResult?.items || [];
    const retainedCandidateCount = resultItems.reduce((sum, item) => sum + (item.candidates?.length || 0), 0);
    const reportedSearchFound = Number(job.search?.candidate_found_count);
    const searchFound = Number.isFinite(reportedSearchFound)
      ? Math.max(reportedSearchFound, retainedCandidateCount)
      : retainedCandidateCount;
    const searchTarget = Number(job.search?.candidate_target_count);
    const hasCandidateTarget = Number.isFinite(searchFound) && Number.isFinite(searchTarget) && searchTarget > 0;
    const percent = hasCandidateTarget
      ? clamp(Math.round((searchFound / searchTarget) * 100), 0, 100)
      : total ? Math.round((finished / total) * 100) : 0;
    const phase = generationPhase(job);
    const active = ["queued", "running", "pause_requested", "paused", "cancel_requested"].includes(job.status);
    const pauseRequested = job.status === "pause_requested";
    const paused = job.status === "paused";
    const partialState = job.status === "partial";
    const failedState = ["failed", "cancelled"].includes(job.status);
    const usableItemCount = resultItems.filter((item) => item.candidates?.length).length;
    const unresolvedItems = resultItems.filter((item) => !item.candidates?.length && ["failed", "cancelled"].includes(item.status));
    const completedFromItems = resultItems.filter((item) => {
      const progress = itemCandidateProgress(item);
      return item.status === "success" || (progress.target !== null && progress.target > 0 && progress.found >= progress.target);
    }).length;
    const failuresFromItems = resultItems.filter((item) => ["failed", "cancelled"].includes(item.status)).length;
    const displayCompleted = Math.min(total, Math.max(success, completedFromItems));
    const displayFailures = Math.min(Math.max(0, total - displayCompleted), Math.max(terminalFailures, failuresFromItems));
    const displayPending = Math.max(0, total - displayCompleted - displayFailures);
    $("#generation-title").textContent = ({
      queued: "任务已加入队列", running: "正在生成候选", pause_requested: "正在安全暂停",
      paused: "任务已暂停", cancel_requested: "正在安全终止", cancelled: "任务已终止",
      completed: "候选生成完成", partial: "候选部分生成完成", failed: "候选生成失败",
    })[job.status] || "候选生成任务";
    $("#generation-status-text").textContent = job.error?.message || ({
      queued: "正在准备只读源模板与锁定配置。",
      running: "正在搜索结构候选并执行独立硬规则验证。",
      pause_requested: "已收到暂停请求，将在下一个安全检查点暂停。",
      paused: "计算已暂停；已完成结果仍保留在当前会话。",
      cancel_requested: "已收到终止请求，正在等待安全检查点。",
      cancelled: "任务已终止；已找到的候选仍保留，可直接使用当前结果或继续处理未完成数量。",
      completed: "所有成品方案已完成候选搜索与独立校验。",
      partial: "已找到部分可用候选；可直接使用当前结果，也可继续搜索未完成项。",
      failed: "当前任务未完整达成目标；已找到的候选仍可使用，其余请按失败原因继续搜索或调整参数。",
    })[job.status] || statusLabel(job.status);
    $("#generation-progress-copy").textContent = hasCandidateTarget
      ? `${searchFound} / ${searchTarget} 个候选 · ${finished} / ${total} 工作项已处理`
      : active ? `${finished} / ${total} 工作项` : `${finished} / ${total} 工作项已处理`;
    $("#generation-progress-percent").textContent = `${percent}%`;
    $("#generation-progress").setAttribute("aria-valuenow", String(percent));
    $("#generation-progress-fill").style.width = `${percent}%`;
    $("#generation-completed-count").textContent = String(displayCompleted);
    $("#generation-pending-count").textContent = String(displayPending);
    $("#generation-failure-count").textContent = String(displayFailures);
    const generatedCandidateCount = availableResult?.items
      ? availableResult.items.reduce((sum, item) => sum + (item.candidates?.length || 0), 0)
      : (job.events || []).filter((event) => event.type === "work_item_progress").reduce((sum, event) => sum + Number(event.details?.candidate_count || 0), 0);
    $("#generation-candidate-count").textContent = String(generatedCandidateCount);
    $("#generation-phase-copy").textContent = phase.copy;
    const elapsed = effectiveTaskElapsed(job);
    $("#generation-elapsed-inline").textContent = formatDuration(elapsed);
    const remaining = active && finished > 0 && finished < total
      ? (elapsed / finished) * (total - finished)
      : null;
    $("#generation-time-copy").textContent = active
      ? paused
        ? `有效计算 ${formatDuration(elapsed)} · 已暂停，继续后恢复剩余时间估算`
        : pauseRequested
          ? `有效计算 ${formatDuration(elapsed)} · 正在前往安全暂停点`
          : remaining !== null
        ? `已用时 ${formatDuration(elapsed)} · 剩余 ${formatApproxDuration(remaining)}（粗估，受模板复杂度影响）`
        : `已用时 ${formatDuration(elapsed)} · ${total > 1 ? "首份成品项完成后给出粗略剩余时间" : "单份耗时取决于求解状态空间"}`
      : `总用时 ${formatDuration(elapsed)} · ${statusLabel(job.status)}`;
    const lastProgressAt = job.last_progress_at || job.started_at || job.created_at;
    const progressAge = lastProgressAt ? Math.max(0, Date.now() - new Date(lastProgressAt).getTime()) : Number.POSITIVE_INFINITY;
    const currentWork = job.current_work_item || job.progress?.current_work_item || latestEvent(job.events, "work_item_started")?.details;
    const lastWork = latestEvent(job.events, "work_item_progress")?.details;
    const workScope = currentWork || lastWork;
    const workLabel = [workScope?.template_key, workScope?.plan_signature || workScope?.plan_id].filter(Boolean).join(" · ");
    const generationFinished = ["completed", "partial", "failed", "cancelled"].includes(job.status);
    $("#generation-current-template").textContent = generationFinished
      ? finished >= total && total ? "全部处理完成" : "任务已停止"
      : workLabel || "正在准备首个成品项";
    renderActivityTimeline($("#generation-activity-log"), job.events, { limit: 9, emptyCopy: `${phase.copy} · 等待后台事件` });
    const health = $("#generation-health");
    const stale = active && !paused && !pauseRequested && progressAge > 60_000;
    const serviceCopy = paused ? "任务已暂停" : pauseRequested ? "正在安全暂停" : failedState ? "任务已停止" : stale ? "仍在计算，等待新进度" : active ? "本地服务运行正常" : "本地服务可用";
    $("#generation-service-inline").className = stale || paused || pauseRequested ? "is-caution" : failedState ? "is-error" : "is-healthy";
    $("#generation-service-inline").innerHTML = `<span class="status-dot"></span>${escapeHtml(serviceCopy)}`;
    announce(
      "#generation-live",
      `${statusLabel(job.status)}，已处理 ${finished} / ${total} 工作项，${percent}%${workLabel ? `，当前 ${workLabel}` : ""}，${serviceCopy}`,
    );
    health.className = `health-chip${paused || pauseRequested || stale || partialState ? " paused" : failedState ? " failed" : ""}`;
    health.innerHTML = `<i></i>${escapeHtml(paused ? "已暂停" : pauseRequested ? "正在暂停" : failedState ? statusLabel(job.status) : stale ? "计算时间较长" : active ? "运行中" : statusLabel(job.status))}`;
    $$("#generation-phase-list li").forEach((item) => {
      const index = Number(item.dataset.phaseIndex);
      const done = job.status === "completed" || (!(active && index === phase.index) && (
        (index === 0 && (phase.index > 0 || finished > 0))
        || (index === 1 && (phase.index > 1 || finished > 0))
        || (index === 2 && Math.max(success, usableItemCount) > 0)
      ));
      item.classList.toggle("is-done", done);
      item.classList.toggle("is-current", active && index === phase.index);
      item.classList.toggle("is-warning", partialState && index === 3);
    });
    $("#generation-spinner").classList.toggle("is-hidden", !active || paused);
    const stateIcon = $("#generation-state-icon");
    stateIcon.classList.toggle("is-hidden", active && !paused);
    stateIcon.classList.toggle("is-error", failedState);
    stateIcon.classList.toggle("is-paused", paused || partialState);
    stateIcon.innerHTML = paused ? icon("pause") : failedState ? icon("stop") : partialState ? "<span aria-hidden=\"true\">!</span>" : icon("check");
    $("#generation-pause").disabled = !(job.actions?.can_pause || job.actions?.can_resume);
    $("#generation-pause").dataset.action = job.actions?.can_resume ? "resume" : "pause";
    $("#generation-pause").innerHTML = job.actions?.can_resume
      ? `${icon("play")}<span>继续</span>`
      : `${icon("pause")}<span>暂停</span>`;
    $("#generation-cancel").disabled = !job.actions?.can_cancel;
    const failures = unresolvedItems;
    const incompleteAvailable = resultItems.filter(itemNeedsMoreCandidates);
    const deterministic = failures.filter(isDeterministicFailure);
    const transient = failures.filter((item) => !isDeterministicFailure(item));
    const budgetFailures = [...failures.filter(isBudgetFailure), ...incompleteAvailable.filter(isBudgetFailure)];
    const retryAllowed = Boolean(job.actions?.can_retry && transient.length > 0);
    const recovery = $("#generation-retry");
    const canContinueSearch = jobCanContinueSearch(job, budgetFailures);
    const canResumeUnfinished = canContinueSearch || retryAllowed;
    renderGenerationBudget(job, canResumeUnfinished);
    const sourceActions = $("#generation-source-actions");
    const showSourceActions = !canResumeUnfinished &&
      ["paused", "partial", "failed", "cancelled"].includes(job.status);
    sourceActions.classList.toggle("is-hidden", !showSourceActions);
    sourceActions.replaceChildren(...(showSourceActions ? [createUnfinishedSourceList(resultItems)] : []));
    recovery.classList.toggle("is-hidden", !deterministic.length);
    recovery.dataset.action = "adjust";
    recovery.innerHTML = `${icon("sliders")}<span>调整失败模板参数</span>`;
    const usePartial = $("#generation-use-partial");
    const usableCandidateCount = availableCandidatesForJob(job);
    const canUsePartial = usableCandidateCount > 0 && Boolean(
      job.actions?.can_use_partial_results || ["completed", "partial"].includes(job.status)
    );
    usePartial.classList.toggle("is-hidden", !canUsePartial);
    // setBusy() temporarily replaces this button's nested markup with text.
    // A poll can render in that window, so only update the label when it exists.
    const usePartialLabel = $("span", usePartial);
    if (usePartialLabel) usePartialLabel.textContent = `使用当前 ${usableCandidateCount} 个候选`;
    const adjustAndContinue = $("#generation-adjust-and-continue");
    const canAdjustAndContinue = generationFinished && resultItems.some((item) => !item?.candidates?.length || itemNeedsMoreCandidates(item));
    adjustAndContinue.classList.toggle("is-hidden", !canAdjustAndContinue);
    renderGenerationFailures(generationFinished ? resultItems : []);
  }
  function itemErrorCopy(item) {
    return item?.error?.message || item?.error?.detail?.message || item?.error?.code || "未在当前限制内找到可通过硬校验的候选";
  }
  function itemCandidateProgress(item) {
    const progress = item?.error?.candidate_progress || item?.error?.detail?.candidate_progress || item?.candidate_progress || {};
    const found = Number(progress.found ?? progress.found_count ?? item?.candidates?.length ?? 0);
    const target = Number(progress.target ?? progress.target_count ?? item?.target_candidate_count ?? item?.candidate_target_count);
    return {
      found: Number.isFinite(found) ? Math.max(0, found) : (item?.candidates?.length || 0),
      target: Number.isFinite(target) ? Math.max(0, target) : null,
    };
  }
  function itemNeedsMoreCandidates(item) {
    if (!item?.candidates?.length) return false;
    const progress = itemCandidateProgress(item);
    return item.status === "partial" || (progress.target !== null && progress.found < progress.target);
  }
  function itemErrorCode(item) { return item?.error?.code || item?.error?.detail?.code || ""; }
  function failureKind(item) {
    if (!item) return "temporary";
    const code = String(itemErrorCode(item)).toUpperCase();
    const explicit = String(item?.failure_kind || item?.diagnosis || item?.error?.failure_kind || item?.error?.detail?.failure_kind || "").toLowerCase();
    const message = itemErrorCopy(item);
    if (explicit === "missing_required_evidence") return "missing_required_evidence";
    if (
      ["parameter_conflict", "proven_conflict", "proven_infeasible", "unsatisfiable"].includes(explicit)
      || /PARAMETER.*CONFLICT|PROVEN.*CONFLICT|INFEASIBLE|UNSATISFIABLE/.test(code)
      || (/已证明|已证/.test(message) && /参数冲突|无法同时满足|不可能生成/.test(message))
      || item?.error?.evidence?.proven_infeasible === true
    ) return "parameter_conflict";
    if (
      code === "NO_VALID_CANDIDATE"
      || /BUDGET|SEARCH_LIMIT|TIMEOUT|EXHAUSTED/.test(code)
      || message.includes("搜索预算")
      || message.includes("当前预算")
      || message.includes("满足全部硬规则的候选")
    ) return "budget_exhausted";
    if (item.candidates?.length && !itemNeedsMoreCandidates(item)) return "available";
    if (item.candidates?.length) return "available_partial";
    if (["cancelled", "pending"].includes(item.status)) return "interrupted";
    return "temporary";
  }
  function isDeterministicFailure(item) {
    return failureKind(item) === "parameter_conflict";
  }
  function isBudgetFailure(item) {
    return failureKind(item) === "budget_exhausted";
  }
  function jobCanContinueSearch(job, knownBudgetItems = null) {
    if (["queued", "running", "paused", "pause_requested", "cancel_requested"].includes(job?.status)) return false;
    if (job?.actions && Object.prototype.hasOwnProperty.call(job.actions, "can_resume_unfinished")) {
      return Boolean(job.actions.can_resume_unfinished);
    }
    if (job?.actions && Object.prototype.hasOwnProperty.call(job.actions, "can_continue_search")) {
      return Boolean(job.actions.can_continue_search);
    }
    const items = knownBudgetItems || availableResultForJob(job)?.items || [];
    return items.some(isBudgetFailure);
  }
  function jobSearchBudget(job = state.jobSnapshot) {
    const search = job?.search || job?.summary?.search || {};
    const budget = search.budget || {};
    const recommended = clamp(Math.round(Number(
      budget.recommended_next_multiplier
      ?? search.recommended_next_multiplier
      ?? search.attempt_budget_multiplier
      ?? 2,
    )), 1, Number(budget.limit_multiplier || 16));
    return {
      search,
      budget,
      recommended,
      limit: clamp(Math.round(Number(budget.limit_multiplier || 16)), 1, 16),
      duration: search.estimated_additional_duration || job?.estimated_additional_duration || {},
      probability: search.success_probability || job?.success_probability || {},
    };
  }
  function compactInteger(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Math.max(0, numeric)) : "—";
  }
  function formatSecondsRange(payload) {
    const reason = payload?.reason || payload?.message || (typeof payload?.basis === "string" ? payload.basis : "完成样本不足");
    if (payload?.status !== "available") return `暂无法可靠估算：${reason}`;
    const low = Number(payload.low_seconds); const high = Number(payload.high_seconds);
    if (!Number.isFinite(low) || !Number.isFinite(high)) return "暂无法可靠估算：缺少稳定耗时样本";
    const basis = payload?.message || (typeof payload?.basis === "string" ? payload.basis : "按本任务已完成尝试粗估");
    return `${formatApproxDuration(low * 1000)}–${formatApproxDuration(high * 1000)}（${basis}）`;
  }
  function formatProbabilityRange(payload) {
    const reason = payload?.reason || payload?.message || (typeof payload?.basis === "string" ? payload.basis : "有效样本不足");
    if (payload?.status !== "available") return `暂无法可靠估算：${reason}`;
    const lowRaw = Number(payload.low ?? payload.low_probability ?? payload.low_percent);
    const highRaw = Number(payload.high ?? payload.high_probability ?? payload.high_percent);
    if (!Number.isFinite(lowRaw) || !Number.isFinite(highRaw)) return "暂无法可靠估算：有效样本不足";
    const percent = (value) => Math.round(value <= 1 ? value * 100 : value);
    const basis = payload?.message || (typeof payload?.basis === "string" ? payload.basis : "启发式区间，仅供选预算");
    return `${clamp(percent(lowRaw), 0, 100)}%–${clamp(percent(highRaw), 0, 100)}%（${basis}）`;
  }
  function budgetAttemptRange(entries, field) {
    const values = (entries || []).map((entry) => Number(entry?.[field])).filter(Number.isFinite);
    if (!values.length) return "—";
    const low = Math.min(...values); const high = Math.max(...values);
    return low === high ? compactInteger(low) : `${compactInteger(low)}–${compactInteger(high)}`;
  }
  function slotAttemptRange(entries, field) {
    const values = (entries || []).flatMap((entry) => {
      const slots = Array.isArray(entry?.unfinished_slots) ? entry.unfinished_slots : [];
      if (slots.length) return slots.map((slot) => Number(slot?.[field])).filter(Number.isFinite);
      const direct = Number(entry?.[field]);
      return Number.isFinite(direct) ? [direct] : [];
    });
    if (!values.length) return "—";
    const low = Math.min(...values); const high = Math.max(...values);
    return low === high ? compactInteger(low) : `${compactInteger(low)}–${compactInteger(high)}`;
  }
  function selectedAttemptBudgetRange(entries, selectedMultiplier, currentMultiplier) {
    const values = (entries || []).map((entry) => {
      const base = Number(entry?.base_attempt_budget);
      if (Number.isFinite(base)) return base * selectedMultiplier;
      const current = Number(entry?.current_attempt_budget);
      return Number.isFinite(current) && currentMultiplier > 0 ? Math.round((current / currentMultiplier) * selectedMultiplier) : NaN;
    }).filter(Number.isFinite);
    if (!values.length) return "—";
    const low = Math.min(...values); const high = Math.max(...values);
    return low === high ? compactInteger(low) : `${compactInteger(low)}–${compactInteger(high)}`;
  }
  function selectedAdditionalAttemptsRange(entries, recommendedEntries, selectedMultiplier, recommendedMultiplier, currentMultiplier) {
    if (selectedMultiplier === recommendedMultiplier) {
      const resumableRecommendations = (recommendedEntries || [])
        .filter((entry) => entry?.resumable !== false)
        .map((entry) => ({
          ...entry,
          unfinished_slots: Array.isArray(entry?.unfinished_slots)
            ? entry.unfinished_slots.filter((slot) => slot?.resumable !== false)
            : entry?.unfinished_slots,
        }));
      const supplied = slotAttemptRange(resumableRecommendations, "additional_attempts");
      if (supplied !== "—") return supplied;
    }
    const values = (entries || []).filter((entry) => entry?.resumable !== false).flatMap((entry) => {
      const base = Number(entry?.base_attempt_budget);
      const currentLimit = Number(entry?.current_attempt_budget);
      const selectedLimit = Number.isFinite(base)
        ? base * selectedMultiplier
        : Number.isFinite(currentLimit) && currentMultiplier > 0
          ? Math.round((currentLimit / currentMultiplier) * selectedMultiplier)
          : NaN;
      if (entry?.scope === "per_work_item") {
        const completed = Number(entry.attempts_completed);
        return Number.isFinite(selectedLimit) && Number.isFinite(completed)
          ? [Math.max(0, selectedLimit - completed)] : [];
      }
      const slots = Array.isArray(entry?.unfinished_slots)
        ? entry.unfinished_slots.filter((slot) => slot?.resumable !== false)
        : [];
      return slots.map((slot) => {
        const completed = Number(slot?.attempts_completed);
        return Number.isFinite(selectedLimit) && Number.isFinite(completed) ? Math.max(0, selectedLimit - completed) : NaN;
      }).filter(Number.isFinite);
    });
    if (!values.length) return "—";
    const low = Math.min(...values); const high = Math.max(...values);
    return low === high ? compactInteger(low) : `${compactInteger(low)}–${compactInteger(high)}`;
  }
  function createSearchBudgetPanel(job = state.jobSnapshot, options = {}) {
    const { search, budget, recommended, limit, duration, probability } = jobSearchBudget(job);
    const entries = Array.isArray(budget.current_attempt_budget_by_item) ? budget.current_attempt_budget_by_item : [];
    const currentAttempts = budgetAttemptRange(entries, "current_attempt_budget");
    const recommendedEntries = Array.isArray(budget.recommended_next_attempt_budget_by_item) ? budget.recommended_next_attempt_budget_by_item : [];
    const recommendedAttempts = budgetAttemptRange(recommendedEntries, "recommended_next_attempt_budget") !== "—"
      ? budgetAttemptRange(recommendedEntries, "recommended_next_attempt_budget")
      : budgetAttemptRange(recommendedEntries, "recommended_attempt_budget") !== "—"
        ? budgetAttemptRange(recommendedEntries, "recommended_attempt_budget")
      : budgetAttemptRange(recommendedEntries, "current_attempt_budget");
    const fallbackRecommendedAttempts = entries.length
      ? budgetAttemptRange(entries.map((entry) => ({ value: Number(entry.base_attempt_budget || 0) * recommended })), "value")
      : "—";
    const budgetExhausted = (availableResultForJob(job)?.items || []).some(isBudgetFailure);
    const currentMultiplier = Math.max(1, Math.round(Number(search.attempt_budget_multiplier ?? budget.current_multiplier ?? 1)));
    const minimumMultiplier = budgetExhausted ? Math.min(limit, currentMultiplier + 1) : 1;
    const selected = clamp(Math.round(Number(state.resumeBudgetMultiplier || recommended)), minimumMultiplier, limit);
    state.resumeBudgetMultiplier = selected;
    const section = document.createElement("section");
    section.className = `search-budget-card${options.compact ? " is-compact" : ""}`;
    section.setAttribute("aria-label", "继续搜索预算建议");
    const heading = document.createElement("header");
    const title = document.createElement("div");
    const stagedCount = state.stagedTemplateAdjustments.size;
    title.innerHTML = `<strong>继续搜索未完成项</strong><span>${stagedCount
      ? `将同时应用 ${escapeHtml(stagedCount)} 个已暂存的单模板参数；已有候选和人工选择保持不变。`
      : "已找到的候选会保留，只搜索每个成品项尚缺的候选。"}</span>`;
    const scope = document.createElement("span"); scope.className = "budget-scope-chip"; scope.textContent = "每个成品项累计上限";
    heading.append(title, scope);
    const facts = document.createElement("dl"); facts.className = "search-budget-facts";
    const factRows = new Map();
    [
      ["current", "当前累计上限"],
      ["recommended", "推荐累计上限"],
      ["additional", "本轮预计新增"],
      ["cumulative", "任务累计尝试"],
      ["duration", "推荐档预计用时"],
      ["probability", "本任务搜索成功率（参考）"],
    ].forEach(([key, termCopy]) => {
      const row = document.createElement("div"); const term = document.createElement("dt"); const detail = document.createElement("dd");
      term.textContent = termCopy; row.append(term, detail); facts.append(row); factRows.set(key, detail);
    });
    const resultItems = availableResultForJob(job)?.items || [];
    section.append(createUnfinishedSourceList(resultItems));
    const controls = document.createElement("div"); controls.className = "search-budget-controls";
    const label = document.createElement("label"); label.textContent = "下一轮累计预算档位";
    const select = document.createElement("select"); select.setAttribute("data-resume-budget", ""); select.setAttribute("aria-label", "选择下一轮每个成品项的累计预算档位");
    const levels = [...new Set([1, 2, 4, 8, 16, recommended, selected]
      .map((value) => Math.round(Number(value)))
      .filter((value) => value >= minimumMultiplier && value <= limit))]
      .sort((left, right) => left - right);
    levels.forEach((value) => {
      const option = document.createElement("option"); option.value = String(value); option.textContent = `${value}×${value === recommended ? "（推荐）" : ""}`; select.append(option);
    });
    const refreshFacts = (multiplier) => {
      const chosen = Number(multiplier);
      const selectedLimit = selectedAttemptBudgetRange(entries, chosen, currentMultiplier);
      const additional = selectedAdditionalAttemptsRange(entries, recommendedEntries, chosen, recommended, currentMultiplier);
      factRows.get("current").textContent = `${currentAttempts} 次 / 项`;
      factRows.get("recommended").textContent = `${recommendedAttempts !== "—" ? recommendedAttempts : fallbackRecommendedAttempts} 次 / 项`;
      factRows.get("additional").textContent = additional === "—" ? "暂无法计算" : `${additional} 次 / 项（选择 ${chosen}× 后）`;
      factRows.get("cumulative").textContent = `${compactInteger(budget.cumulative_attempts_completed ?? search.cumulative_attempts_completed)} 次`;
      const durationCopy = formatSecondsRange(duration);
      factRows.get("duration").textContent = chosen === recommended
        ? durationCopy
        : `${durationCopy}；仅对应推荐 ${recommended}×，当前 ${chosen}× 未单独估算`;
      factRows.get("probability").textContent = formatProbabilityRange(probability);
      scope.textContent = selectedLimit === "—" ? "每个成品项累计上限" : `选择后累计上限 ${selectedLimit} 次 / 项`;
    };
    select.value = String(selected); label.append(select); refreshFacts(selected);
    select.addEventListener("change", () => {
      state.resumeBudgetMultiplier = Number(select.value); refreshFacts(state.resumeBudgetMultiplier);
      $$('[data-resume-budget]').forEach((input) => { if (input !== select) input.value = select.value; });
    });
    const button = document.createElement("button"); button.type = "button"; button.className = "primary-button";
    button.dataset.adjustmentRunControl = "";
    button.disabled = state.adjustmentRunPending;
    button.innerHTML = `${icon("play")}<span>继续搜索未完成项</span>`;
    button.addEventListener("click", () => continueCurrentJobSearch(Number(select.value), button));
    controls.append(label, button);
    section.prepend(heading, facts);
    section.append(controls);
    return section;
  }
  function renderGenerationBudget(job, visible) {
    const root = $("#generation-budget-panel"); if (!root) return;
    root.classList.toggle("is-hidden", !visible);
    if (!visible) { root.replaceChildren(); return; }
    root.replaceChildren(createSearchBudgetPanel(job));
  }
  function createUnfinishedSourceList(items) {
    const list = document.createElement("ul"); list.className = "search-budget-progress-list";
    items.filter((item) => itemNeedsMoreCandidates(item) || !item?.candidates?.length).forEach((item) => {
      const progress = itemCandidateProgress(item); const row = document.createElement("li");
      const scopeCopy = [item.template_key, item.plan_signature || item.plan_id].filter(Boolean).join(" · ");
      const name = document.createElement("span"); name.textContent = scopeCopy;
      const actions = document.createElement("div"); actions.className = "unfinished-source-actions";
      const source = document.createElement("button"); source.type = "button"; source.className = "text-button";
      source.dataset.viewSource = item.template_key; source.textContent = "查看原模板";
      source.setAttribute("aria-label", `查看 ${scopeCopy} 原模板`);
      const count = document.createElement("strong");
      count.textContent = `已找到 ${progress.found}${progress.target !== null ? ` / ${progress.target}` : ""} 个候选`;
      actions.append(source, count); row.append(name, actions); list.append(row);
    });
    return list;
  }
  function taskHasDeterministicFailure(task) {
    const items = task?.result?.items || task?.items || task?.result_items || [];
    if (items.length) return items.some(isDeterministicFailure);
    return (task?.events || []).some((event) => {
      const error = event?.details?.error || {};
      const code = error.code || error.detail?.code || "";
      const message = error.message || error.detail?.message || event.message || "";
      return /PARAMETER.*CONFLICT|PROVEN.*CONFLICT|INFEASIBLE|UNSATISFIABLE/.test(String(code).toUpperCase())
        || String(message).includes("已证明参数冲突");
    });
  }
  function taskHasTransientFailure(task) {
    const items = task?.result?.items || task?.items || task?.result_items || [];
    if (items.length) return items.some((item) => !item.candidates?.length && !isDeterministicFailure(item));
    return !taskHasDeterministicFailure(task);
  }
  function failureStatusCopy(item) {
    if (failureKind(item) === "missing_required_evidence") return "缺少精准模式所需证据";
    if (isDeterministicFailure(item)) return "参数已证冲突";
    if (isBudgetFailure(item)) return "当前预算未找到";
    if (failureKind(item) === "interrupted") return "任务已中断";
    return "暂未完成";
  }
  function failureAdjustmentHint(item) {
    if (failureKind(item) === "missing_required_evidence") return "请补齐或去歧义 config_level 中原关卡的 DupPara1–10，或关闭精准模式后重新生成。";
    const counts = item?.rejection_counts || item?.error?.evidence?.rejection_counts || {};
    const strongest = Object.entries(counts).sort((left, right) => Number(right[1]) - Number(left[1]))[0]?.[0] || "";
    if (strongest.includes("tower_capacity")) return "可调整塔内生成方式、结构块上限或结构块种类数。";
    if (strongest.includes("geometry") || strongest.includes("preferred_coord") || strongest.includes("no_initial_entry")) return "可放宽结构块最高层、结构块数量或替换来源。";
    if (strongest.includes("topology")) return "可调整谜题步数、结构块种类数或分布方式。";
    if (strongest.includes("type_assignment") || strongest.includes("proof")) return "可增加可用结构块、调整种类数或降低目标步数。";
    return "建议放宽结构块最高层、结构块上限、替换来源或谜题步数中的一项。";
  }
  const SUGGESTION_FIELD_LABELS = {
    candidate_count: "候选数量",
    max_layer_percent: "结构块最高层百分比",
    max_layer: "结构块最高层",
    max_structure_layer_span: "结构块最大层间距",
    total_percent_limit: "结构块数量占比",
    total_count_limit: "结构块数量上限",
    total_count_minimum: "结构块数量下限",
    total_percent_minimum: "结构块数量占比下限",
    required_pre_gate_recycles: "门槛前回收次数",
    first_recycle_lead_tiles: "首次回收前铺垫块数",
    replacement_sources: "替换来源",
    tower_mode: "塔内生成",
    step: "谜题步数",
    unit_1_step: "谜题 1 步数",
    unit_2_step: "谜题 2 步数",
  };
  function parameterSuggestionValues(item) {
    const localRaw = item?.parameter_recommendations
      || item?.parameter_suggestions
      || item?.suggested_adjustments
      || item?.error?.evidence?.parameter_recommendations
      || item?.error?.evidence?.parameter_suggestions
      || item?.error?.detail?.parameter_recommendations
      || [];
    const normalize = (raw) => {
      if (!raw) return [];
      if (Array.isArray(raw)) return raw.flatMap(normalize);
      if (Array.isArray(raw?.suggestions)) return raw.suggestions.flatMap(normalize);
      if (Array.isArray(raw?.recommendations)) return raw.recommendations.flatMap(normalize);
      if (raw && typeof raw === "object" && (raw.field || raw.parameter || raw.key || raw.changes || raw.apply_payload || raw.parameters)) return [raw];
      if (raw && typeof raw === "object" && item?.template_key && raw[item.template_key]) return normalize(raw[item.template_key]);
      return [];
    };
    const jobSources = [
      state.jobSnapshot?.parameter_recommendations,
      state.jobSnapshot?.summary?.parameter_recommendations,
      state.jobSnapshot?.result?.parameter_recommendations,
      state.jobSnapshot?.result?.summary?.parameter_recommendations,
      state.jobResult?.parameter_recommendations,
      state.jobResult?.summary?.parameter_recommendations,
    ];
    const itemPlanIds = new Set([item?.plan_id, item?.resolved_plan_id, item?.source_plan_id].filter(Boolean).map(String));
    const matchesItem = (suggestion) => {
      const templateKey = suggestion?.template_key || suggestion?.source_template_id;
      if (templateKey && String(templateKey) !== String(item?.template_key || "")) return false;
      const planId = suggestion?.plan_id || suggestion?.resolved_plan_id || suggestion?.source_plan_id;
      return !(planId && itemPlanIds.size && !itemPlanIds.has(String(planId)));
    };
    const combined = [...normalize(localRaw), ...jobSources.flatMap(normalize)].filter(matchesItem);
    const seen = new Set();
    return combined.filter((suggestion) => {
      let key;
      const recommendationId = suggestion?.recommendation_id || suggestion?.id;
      if (recommendationId) key = `recommendation:${recommendationId}`;
      else try { key = JSON.stringify(suggestion); } catch { key = String(suggestion); }
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
  }
  function parameterSuggestionApplicability(suggestion) {
    const trial = suggestion?.trial_result ?? suggestion?.probe_result ?? suggestion?.test_result ?? suggestion?.validation_result;
    const status = typeof trial === "object" ? String(trial?.status || "").toLowerCase() : "";
    const passedStatus = ["passed", "success", "verified", "found"].includes(status);
    const verified = suggestion?.verified === true || (suggestion?.verified == null && passedStatus);
    const opportunity = suggestion?.opportunity_only === true
      || suggestion?.application_semantics === "unverified_opportunity";
    const explicitlyApplicable = suggestion?.applicable === true
      || suggestion?.can_apply === true
      || suggestion?.actions?.can_apply === true;
    return {
      verified,
      opportunity,
      applicable: Boolean(
        (suggestion?.recommendation_id || suggestion?.id)
        && suggestionChangeEntries(suggestion).length
        && (verified || (opportunity && explicitlyApplicable))
      ),
    };
  }
  function rankedApplicableParameterSuggestions(item) {
    return parameterSuggestionValues(item)
      .map((suggestion, index) => ({ suggestion, index, ...parameterSuggestionApplicability(suggestion) }))
      .filter((entry) => entry.applicable)
      .sort((left, right) => {
        const score = (entry) => (entry.verified ? 200 : 0)
          + (String(entry.suggestion?.field || "").replace(/^generation\./, "") === "max_structure_layer_span" ? 40 : 0)
          + (entry.opportunity ? 10 : 0);
        const scoreDelta = score(right) - score(left);
        if (scoreDelta) return scoreDelta;
        const leftRank = Number(left.suggestion?.recommendation_rank ?? left.suggestion?.rank ?? Number.MAX_SAFE_INTEGER);
        const rightRank = Number(right.suggestion?.recommendation_rank ?? right.suggestion?.rank ?? Number.MAX_SAFE_INTEGER);
        return leftRank - rightRank || left.index - right.index;
      })
      .map((entry) => entry.suggestion);
  }
  function suggestionChangeEntries(suggestion) {
    if (Array.isArray(suggestion?.unit_adjustments) && suggestion.unit_adjustments.length) {
      return suggestion.unit_adjustments.flatMap((adjustment) => {
        const field = String(adjustment?.field || Object.keys(adjustment?.changes || {})[0] || "");
        const unitIndex = Number(adjustment?.unit_index);
        if (!field || !Number.isInteger(unitIndex) || unitIndex < 0 || adjustment?.after === undefined) return [];
        return [{
          field: `unit_${unitIndex + 1}_${field.replace(/^generation\./, "")}`,
          label: adjustment?.label,
          before: adjustment?.before,
          after: adjustment?.after,
        }];
      });
    }
    const raw = suggestion?.changes || suggestion?.apply_payload || suggestion?.parameters || null;
    if (Array.isArray(raw)) {
      return raw.map((entry) => ({
        field: String(entry?.field || entry?.parameter || entry?.key || ""),
        label: entry?.label,
        before: entry?.before ?? entry?.from ?? entry?.current_value,
        after: entry?.after ?? entry?.to ?? entry?.suggested_value ?? entry?.value,
      })).filter((entry) => entry.field && entry.after !== undefined);
    }
    if (raw && typeof raw === "object") {
      const fields = Object.entries(raw);
      return fields.map(([field, value]) => {
        const matchesScalarField = fields.length === 1 && String(suggestion?.field || suggestion?.parameter || suggestion?.key || "") === field;
        const scalarBefore = matchesScalarField && (suggestion?.before === null || typeof suggestion?.before !== "object") ? suggestion?.before : undefined;
        const scalarAfter = matchesScalarField && (suggestion?.after === null || typeof suggestion?.after !== "object") ? suggestion?.after : undefined;
        return {
          field,
          label: matchesScalarField ? suggestion?.label : value?.label,
          before: value?.before ?? value?.from ?? scalarBefore ?? suggestion?.before?.[field] ?? suggestion?.current?.[field],
          after: value?.after ?? value?.to ?? value?.value ?? scalarAfter ?? value,
        };
      });
    }
    const field = suggestion?.field || suggestion?.parameter || suggestion?.key;
    if (!field) return [];
    return [{
      field: String(field),
      label: suggestion?.label,
      before: suggestion?.before ?? suggestion?.from ?? suggestion?.current_value,
      after: suggestion?.after ?? suggestion?.to ?? suggestion?.suggested_value ?? suggestion?.value,
    }].filter((entry) => entry.after !== undefined);
  }
  function suggestionFieldLabel(field) {
    const normalized = String(field || "")
      .replace(/^generation\./, "")
      .replace(/^units(?:\.|\[)0(?:\]|\.)\.?/, "unit_1_")
      .replace(/^units(?:\.|\[)1(?:\]|\.)\.?/, "unit_2_");
    const unitField = normalized.match(/^unit_(\d+)_(.+)$/);
    if (unitField) return `谜题 ${unitField[1]} · ${SUGGESTION_FIELD_LABELS[unitField[2]] || unitField[2].replaceAll("_", " ")}`;
    return SUGGESTION_FIELD_LABELS[normalized] || String(field || "参数").replaceAll("_", " ");
  }
  function formatSuggestionValue(field, value) {
    if (value === undefined || value === null || value === "") return "未设置";
    if (String(field).includes("percent")) return `${value}%`;
    if (String(field).includes("layer") || String(field).includes("span")) return `${value} 层`;
    if (String(field).includes("recycles")) return `${value} 次`;
    if (String(field).includes("tiles") || String(field).includes("count")) return `${value}`;
    return String(value);
  }
  function focusSuggestionField(field) {
    const normalized = String(field || "")
      .replace(/^generation\./, "")
      .replace(/^units(?:\.|\[)0(?:\]|\.)\.?/, "unit_1_")
      .replace(/^units(?:\.|\[)1(?:\]|\.)\.?/, "unit_2_")
      .replace(/^unit_\d+_(required_pre_gate_recycles|first_recycle_lead_tiles)$/, "$1");
    const selector = ({
      candidate_count: "#candidate-count",
      max_layer_percent: "#max-layer-value",
      max_layer: "#max-layer-value",
      max_structure_layer_span: "#max-structure-layer-span",
      total_percent_limit: "#total-limit-value",
      total_count_limit: "#total-limit-value",
      total_count_minimum: "#total-count-minimum",
      total_percent_minimum: "#total-count-minimum",
      required_pre_gate_recycles: "#required-pre-gate-recycles",
      first_recycle_lead_tiles: "#first-recycle-lead-tiles",
      replacement_sources: "#replacement-sources",
      tower_mode: "#tower-mode",
      step: "#unit-1-step",
      unit_1_step: "#unit-1-step",
      unit_2_step: "#unit-2-step",
    })[normalized] || "#required-pre-gate-recycles";
    clearConfigPreflight();
    const input = $(selector);
    input?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => input?.focus(), 0);
  }
  function adjustPreflightSuggestion(item, suggestion, field) {
    const templateKey = item?.template_key || suggestion?.template_key || "";
    const rule = state.templateRules.get(templateKey);
    if (!templateKey || !rule) {
      toast("当前模板的编辑上下文已失效，请重新导入。", "error");
      return;
    }
    const sourcePlanId = String(suggestion?.source_plan_id || "");
    const targetsBatchPlan = sourcePlanId.startsWith("batch_");
    if (rule.mode === "append" && targetsBatchPlan) {
      requestConfirmation(
        `${templateKey} 同时有追加方案`,
        "这项冲突来自统一方案。为避免改动其他模板或覆盖当前追加方案，将新建只含该模板的调整任务。",
        () => prefillSingleTemplateTask(item),
        { safeLabel: "保留当前配置", confirmLabel: "新建调整任务", danger: false },
      );
      return;
    }
    invalidateGenerationForPlanChange();
    if (["use_batch", "skip"].includes(rule.mode)) {
      rule.mode = "replace";
      rule.plan = clone(state.batchPlan);
    }
    state.activeTemplate = templateKey;
    editTemplatePlan(templateKey);
    setFlow(2);
    toast(`已打开 ${templateKey} 的对应方案，只修改该模板。`);
    focusSuggestionField(field);
  }
  function applyChangeToPlan(plan, entry) {
    const field = String(entry.field || "").replace(/^generation\./, "");
    const value = entry.after;
    if (field === "candidate_count") plan.candidateCount = clamp(Math.round(Number(value)), 1, 20);
    else if (field === "max_layer_percent") { plan.maxLayerKind = "percent"; plan.maxLayerValue = Number(value); }
    else if (field === "max_layer") { plan.maxLayerKind = "absolute"; plan.maxLayerValue = Number(value); }
    else if (field === "max_structure_layer_span") { plan.maxStructureLayerSpanKind = value === null ? "none" : "absolute"; plan.maxStructureLayerSpan = Math.max(0, Math.round(Number(value) || 0)); }
    else if (field === "total_percent_limit") { plan.totalLimitKind = "percent"; plan.totalLimitValue = Number(value); }
    else if (field === "total_count_limit") { plan.totalLimitKind = "absolute"; plan.totalLimitValue = Number(value); }
    else if (field === "total_count_minimum") { plan.totalMinimumKind = "absolute"; plan.totalCountMinimum = Number(value); }
    else if (field === "total_percent_minimum") { plan.totalMinimumKind = "percent"; plan.totalCountMinimum = Number(value); }
    else if (field === "required_pre_gate_recycles") plan.requiredPreGateRecycles = Math.max(0, Math.round(Number(value) || 0));
    else if (field === "first_recycle_lead_tiles") plan.firstRecycleLeadTiles = Math.max(0, Math.round(Number(value) || 0));
    else if (field === "replacement_sources") plan.replacementSources = Array.isArray(value) ? [...value] : String(value);
    else if (field === "tower_mode") plan.towerMode = String(value);
    else if (["step", "unit_1_step", "units.0.step", "units[0].step"].includes(field)) plan.steps[0] = clamp(Math.round(Number(value)), 4, 9);
    else if (["unit_2_step", "units.1.step", "units[1].step"].includes(field)) plan.steps[1] = clamp(Math.round(Number(value)), 4, 9);
    else return false;
    return true;
  }
  function updateAppliedSuggestionNotice(copy = state.lastParameterSuggestionUndo?.copy || "") {
    const root = $("#applied-suggestion"); if (!root) return;
    const undo = state.lastParameterSuggestionUndo;
    const visible = Boolean(copy && (
      (undo?.source === "local" && state.editorScope === "template" && state.activeTemplate === undo.templateKey)
      || (undo?.source === "preflight" && state.currentFlow === 2)
    ));
    root.classList.toggle("is-hidden", !visible);
    $("#applied-suggestion-copy").textContent = copy || "—";
  }
  function clearConfigPreflight() {
    state.preflightSnapshot = null;
    const root = $("#config-preflight-feedback");
    root?.classList.add("is-hidden");
    $("#config-preflight-suggestions")?.replaceChildren();
  }
  function renderConfigPreflight(payload) {
    const root = $("#config-preflight-feedback");
    const suggestionRoot = $("#config-preflight-suggestions");
    if (!root || !suggestionRoot) return;
    state.preflightSnapshot = payload;
    const error = payload?.error;
    const errorCopy = typeof error === "string"
      ? error
      : error?.message || error?.detail?.message || "当前回收规则与谜题步数或结构块数量不能同时满足。";
    $("#config-preflight-copy").textContent = errorCopy;
    const raw = payload?.parameter_recommendations || payload?.recommendations || [];
    const suggestions = Array.isArray(raw)
      ? raw
      : Array.isArray(raw?.suggestions)
        ? raw.suggestions
        : Array.isArray(raw?.recommendations) ? raw.recommendations : [];
    suggestionRoot.replaceChildren();
    suggestions.forEach((suggestion) => {
      const templateKey = suggestion?.template_key || payload?.template_key || state.activeTemplate;
      const importedItem = state.imported?.items?.find((item) => item.template_key === templateKey) || activeImportedItem() || {};
      const item = {
        ...importedItem,
        template_key: templateKey || importedItem.template_key,
        plan_id: suggestion?.resolved_plan_id || suggestion?.source_plan_id || suggestion?.plan_id,
        error,
      };
      suggestionRoot.append(createParameterSuggestionCard(item, { ...suggestion, __preflight_id: payload.preflight_id }));
    });
    if (!suggestions.length) {
      const empty = document.createElement("p"); empty.className = "field-help";
      empty.textContent = payload?.recommendations_pending
        ? "正在用小预算试算可行的修改，请稍候。"
        : "暂未找到通过小预算试算的一键修改，请按提示手动调整。";
      suggestionRoot.append(empty);
    }
    root.classList.remove("is-hidden");
    window.setTimeout(() => root.scrollIntoView({ behavior: "smooth", block: "nearest" }), 0);
  }
  function suggestionDifferenceCopy(entries) {
    return entries.map((entry) => `${entry.label || suggestionFieldLabel(entry.field)} ${formatSuggestionValue(entry.field, entry.before)} → ${formatSuggestionValue(entry.field, entry.after)}`).join("；");
  }
  function refreshParameterSuggestionSurfaces() {
    if (state.preflightSnapshot) renderConfigPreflight(state.preflightSnapshot);
    if (!$("#generation-state")?.classList.contains("is-hidden") && state.jobSnapshot) renderGenerationProgress(state.jobSnapshot);
    if (!$("#candidate-workspace")?.classList.contains("is-hidden") && state.jobResult) {
      renderCandidateTaskOverview();
      renderCandidateBatchWarning();
    }
  }
  async function applyParameterSuggestion(item, suggestion, trigger = null) {
    const templateKey = item?.template_key || "";
    const entries = suggestionChangeEntries(suggestion);
    const rule = state.templateRules.get(templateKey);
    const importedItem = state.imported?.items?.find((entry) => entry.template_key === templateKey);
    const recommendationId = suggestion?.recommendation_id || suggestion?.id;
    const preflightId = suggestion?.__preflight_id;
    const unitScoped = suggestion?.unit_index !== undefined && suggestion?.unit_index !== null;
    // A recommendation attached to a finished job is scoped by its resolved
    // plan (and sometimes by unit_index). Let the service build that one-template
    // draft so an appended plan or a batch-sourced unit cannot be changed by
    // accident. The local path is only for a genuinely pre-job editor state.
    if (rule && importedItem && !state.isRecoveredJob && !state.jobId && !preflightId && !recommendationId && !unitScoped) {
      const previousRule = clone(rule);
      invalidateGenerationForPlanChange();
      if (["use_batch", "skip"].includes(rule.mode)) {
        rule.mode = "replace";
        rule.plan = clone(state.batchPlan);
      }
      const applied = entries.filter((entry) => applyChangeToPlan(rule.plan, entry));
      if (!applied.length) {
        state.templateRules.set(templateKey, previousRule);
        toast("建议包含的参数尚不支持一键应用，已为你打开单模板调整。", "error");
        state.activeTemplate = templateKey; editTemplatePlan(templateKey); setFlow(2); return;
      }
      const copy = suggestionDifferenceCopy(applied);
      state.lastParameterSuggestionUndo = { source: "local", templateKey, previousRule, copy };
      state.activeTemplate = templateKey;
      editTemplatePlan(templateKey);
      setFlow(2);
      updateAppliedSuggestionNotice(copy);
      window.setTimeout(() => $("#applied-suggestion")?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 0);
      return;
    }
    if ((!preflightId && !state.jobId) || !recommendationId || !entries.length) {
      adjustFailedTemplate(templateKey, item?.plan_id || "");
      return;
    }
    setBusy(trigger, true, "正在应用…");
    try {
      const applyPath = preflightId
        ? `/preflights/${encodeURIComponent(preflightId)}/recommendations/apply`
        : `/jobs/${encodeURIComponent(state.jobId)}/recommendations/apply`;
      const payload = await api(applyPath, {
        method: "POST",
        body: { recommendation_id: recommendationId },
      });
      const continueRequest = payload?.continue_request || {};
      const continueBody = continueRequest?.body || {};
      const differences = Array.isArray(payload.differences) && payload.differences.length
        ? payload.differences
        : entries;
      const copy = suggestionDifferenceCopy(differences);
      const previousUndo = state.lastParameterSuggestionUndo;
      state.lastParameterSuggestionUndo = {
        source: preflightId ? "preflight" : "remote",
        jobId: state.jobId,
        preflightId,
        importId: continueBody.import_id || payload.import_id || state.preflightSnapshot?.import_id || state.jobSnapshot?.import_id,
        templateKey: payload.template_key || templateKey,
        recommendationId,
        undoToken: payload.undo_token,
        batchRequest: continueBody.batch_request || payload.batch_request,
        continueRequest: continueRequest?.path ? continueRequest : null,
        copy,
        previousUndo: preflightId ? previousUndo : null,
      };
      refreshParameterSuggestionSurfaces();
      toast(`已将建议应用到 ${payload.template_key || templateKey} 的参数草案；原任务和已有候选保持不变。`);
    } catch (error) {
      toast(error.message, "error");
    } finally { setBusy(trigger, false); }
  }
  async function undoParameterSuggestion(trigger = null) {
    const undo = state.lastParameterSuggestionUndo;
    if (!undo) return;
    if (["remote", "preflight"].includes(undo.source)) {
      setBusy(trigger, true, "正在撤销…");
      try {
        const undoPath = undo.source === "preflight"
          ? `/preflights/${encodeURIComponent(undo.preflightId)}/recommendations/undo`
          : `/jobs/${encodeURIComponent(undo.jobId)}/recommendations/undo`;
        const payload = await api(undoPath, {
          method: "POST",
          body: { undo_token: undo.undoToken },
        });
        state.lastParameterSuggestionUndo = undo.previousUndo || null;
        if (undo.source === "preflight" && undo.importId && payload?.batch_request) {
          const preflight = await api("/plans/resolve", {
            method: "POST",
            body: { import_id: undo.importId, batch_request: payload.batch_request },
          });
          if (preflight?.valid === false) renderConfigPreflight(preflight);
          else clearConfigPreflight();
          updateAppliedSuggestionNotice();
        }
        refreshParameterSuggestionSurfaces();
        toast("已撤销参数草案，原任务与已找到的候选均未改动。");
      } catch (error) { toast(error.message, "error"); }
      finally { setBusy(trigger, false); }
      return;
    }
    state.templateRules.set(undo.templateKey, clone(undo.previousRule));
    state.lastParameterSuggestionUndo = null;
    state.activeTemplate = undo.templateKey;
    editTemplatePlan(undo.templateKey);
    updateAppliedSuggestionNotice();
    toast("已撤销上一次参数建议，只恢复了当前模板的方案。");
  }
  async function regenerateFromParameterDraft(trigger = null) {
    const draft = state.lastParameterSuggestionUndo;
    if (!draft || !["remote", "preflight"].includes(draft.source) || !draft.batchRequest) return;
    const importId = draft.importId || state.preflightSnapshot?.import_id || state.jobSnapshot?.import_id;
    if (!importId) { toast("当前任务缺少可复用的导入记录，请重新导入该模板。", "error"); return; }
    setBusy(trigger, true, "正在继续…");
    try {
      const preflight = await api("/plans/resolve", {
        method: "POST",
        body: { import_id: importId, batch_request: draft.batchRequest },
      });
      if (preflight?.valid === false) {
        renderConfigPreflight(preflight);
        updateAppliedSuggestionNotice(draft.copy);
        toast("这项修改已生效，但批次中还有一处配置冲突；请继续处理后再生成。", "error");
        return;
      }
      const inheritedSelections = new Map([...state.selectedCandidates].map(([key, value]) => [key, [...value]]));
      const inheritedResult = state.jobResult || availableResultForJob(state.jobSnapshot);
      const reviewWorkspaceVisible = !$("#candidate-workspace")?.classList.contains("is-hidden");
      const createBody = {
        ...(draft.continueRequest?.body || {}),
        import_id: importId,
        batch_request: draft.batchRequest,
      };
      const job = await api("/jobs", { method: "POST", body: createBody });
      if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
      state.jobId = job.job_id; state.jobSnapshot = job; state.jobResult = inheritedResult;
      state.jobStartedAt = job.started_at || job.created_at || new Date().toISOString();
      state.jobRecommendationPollAttempts = 0; state.jobPollFailures = 0; state.isRecoveredJob = draft.source === "remote";
      state.selectedCandidates = inheritedSelections; state.selectionsByJob.set(state.jobId, state.selectedCandidates); persistSelections(state.jobId);
      state.compareIds.clear(); state.activeWorkIndex = 0; state.candidatePage = 0; state.candidateOverviewOpened = false;
      state.lastParameterSuggestionUndo = null;
      clearConfigPreflight();
      state.completedSteps = new Set([1, 2]); state.unlockedFlow = 3;
      $("#compare-workspace").classList.add("is-hidden"); $("#focus-workspace").classList.add("is-hidden");
      navigate("create"); setFlow(3); renderGenerationProgress(job);
      if (reviewWorkspaceVisible && inheritedResult) {
        $("#generation-state").classList.add("is-hidden");
        $("#candidate-workspace").classList.remove("is-hidden");
        renderCandidateWorkspace();
      } else {
        $("#candidate-workspace").classList.add("is-hidden");
        $("#generation-state").classList.remove("is-hidden");
      }
      pollJob(job.job_id);
      toast("已按建议参数继续当前模板搜索；其他候选、进度与人工选择均已保留。");
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(trigger, false); }
  }
  async function applyParameterSuggestionAndContinue(item, suggestion, trigger = null) {
    const recommendationId = suggestion?.recommendation_id || suggestion?.id;
    await applyParameterSuggestion(item, suggestion, trigger);
    const draft = state.lastParameterSuggestionUndo;
    if (!draft || draft.recommendationId !== recommendationId || draft.source !== "remote" || !draft.batchRequest) return;
    await regenerateFromParameterDraft(trigger);
  }
  function createParameterSuggestionCard(item, suggestion) {
    const card = document.createElement("article"); card.className = "parameter-suggestion-card";
    const header = document.createElement("header");
    const title = document.createElement("strong");
    const scopeCopy = [item?.template_key, item?.plan_signature || item?.plan_id].filter(Boolean).join(" · ");
    const titleCopy = suggestion?.title || (suggestion?.label ? `推荐调整：${suggestion.label}` : "推荐调整当前模板");
    title.textContent = scopeCopy ? `${scopeCopy} · ${titleCopy}` : titleCopy;
    const trialResultRaw = suggestion?.trial_result ?? suggestion?.probe_result ?? suggestion?.test_result ?? suggestion?.validation_result;
    const trialStatusValue = typeof trialResultRaw === "object" ? String(trialResultRaw?.status || "").toLowerCase() : "";
    const trialStatusPassed = ["passed", "success", "verified", "found"].includes(trialStatusValue);
    const trialPassed = ["pending", "queued", "running", "probing"].includes(trialStatusValue)
      ? undefined
      : trialStatusPassed
        ? suggestion?.verified === true
        : ["failed", "error", "conflict", "no_result", "not_found"].includes(trialStatusValue)
          ? false
          : suggestion?.verified === true && !trialResultRaw
            ? true
            : suggestion?.trial_passed ?? suggestion?.probe_passed;
    const opportunityOnly = suggestion?.opportunity_only === true || suggestion?.application_semantics === "unverified_opportunity";
    const trialStatus = trialPassed === true ? "小预算已验证" : opportunityOnly ? "未验证但可尝试" : trialPassed === false ? "小预算未找到" : "小预算试算中";
    const chip = document.createElement("span"); chip.className = `status-chip${trialPassed === true ? " success" : trialPassed === false ? " warning" : ""}`; chip.textContent = trialStatus;
    header.append(title, chip);
    const entries = suggestionChangeEntries(suggestion);
    const diffs = document.createElement("div"); diffs.className = "suggestion-diffs";
    entries.forEach((entry) => {
      const row = document.createElement("div");
      const label = document.createElement("span"); label.textContent = entry.label || suggestionFieldLabel(entry.field);
      const value = document.createElement("strong"); value.textContent = `${formatSuggestionValue(entry.field, entry.before)} → ${formatSuggestionValue(entry.field, entry.after)}`;
      row.append(label, value); diffs.append(row);
    });
    const reason = suggestion?.reason || suggestion?.why || (isDeterministicFailure(item)
      ? "当前参数组合已证明无法同时满足。"
      : "当前搜索预算内尚未找到满足所有硬规则的候选。");
    const baseImpact = suggestion?.impact || suggestion?.effect || "放宽当前限制后，可用坐标或类型组合会增加。";
    const impact = opportunityOnly
      ? `${baseImpact} 当前只确认了方向，尚未在小预算内找到合格候选，不保证续跑一定成功。`
      : baseImpact;
    let trial = "尚未返回小预算试算结果。";
    if (trialResultRaw && typeof trialResultRaw === "object") {
      const details = [];
      if (trialResultRaw.message) details.push(String(trialResultRaw.message));
      const found = Number(trialResultRaw.candidate_count ?? trialResultRaw.found_count);
      if (Number.isFinite(found) && !details.some((copy) => copy.includes(String(found)))) details.push(`找到 ${found} 个合格候选`);
      const budget = trialResultRaw.budget ?? trialResultRaw.search_budget ?? trialResultRaw.attempt_budget;
      if (budget !== undefined && budget !== null && budget !== "") details.push(`试算预算 ${budget}`);
      trial = details
        .map((copy, index) => index < details.length - 1 ? copy.replace(/[。；]+$/u, "") : copy)
        .join("；") || (trialStatusValue ? `试算状态：${trialStatusValue}` : trial);
    } else if (trialResultRaw) trial = String(trialResultRaw);
    const context = document.createElement("dl"); context.className = "suggestion-context";
    [["原因", reason], ["影响", impact], ["小预算结果", trial]].forEach(([labelCopy, valueCopy]) => {
      const row = document.createElement("div"); const term = document.createElement("dt"); term.textContent = labelCopy; const detail = document.createElement("dd"); detail.textContent = String(valueCopy); row.append(term, detail); context.append(row);
    });
    const actions = document.createElement("div"); actions.className = "suggestion-actions";
    const recommendationId = suggestion?.recommendation_id || suggestion?.id;
    const preflightId = suggestion?.__preflight_id;
    const backendCanApply = Boolean(recommendationId && entries.length && (
      trialPassed === true
      || (opportunityOnly && (
        suggestion?.applicable === true
        || suggestion?.can_apply === true
        || suggestion?.actions?.can_apply === true
      ))
    ));
    const activeDraft = state.lastParameterSuggestionUndo;
    const isAppliedDraft = activeDraft?.recommendationId === recommendationId && (
      (activeDraft.source === "remote" && activeDraft.jobId === state.jobId)
      || (activeDraft.source === "preflight" && activeDraft.preflightId === preflightId)
    );
    const apply = document.createElement("button"); apply.type = "button"; apply.className = "secondary-button";
    if (isAppliedDraft) {
      card.classList.add("is-applied");
      apply.textContent = "已应用到参数草案"; apply.disabled = true; actions.append(apply);
      const regenerate = document.createElement("button"); regenerate.type = "button"; regenerate.className = "primary-button";
      regenerate.textContent = activeDraft.source === "preflight" ? "使用建议并生成" : "用此参数重新生成";
      regenerate.addEventListener("click", () => regenerateFromParameterDraft(regenerate)); actions.append(regenerate);
      const undo = document.createElement("button"); undo.type = "button"; undo.className = "text-button"; undo.textContent = "撤销";
      undo.addEventListener("click", () => undoParameterSuggestion(undo)); actions.append(undo);
    } else if (preflightId && entries.length && trialPassed === true) {
      apply.textContent = "一键应用到当前模板";
      apply.addEventListener("click", () => applyParameterSuggestion(item, suggestion, apply)); actions.append(apply);
    } else if (trialPassed === undefined) {
      apply.textContent = "等待小预算试算"; apply.disabled = true; actions.append(apply);
    } else {
      if (!preflightId && backendCanApply) {
        const continueButton = document.createElement("button"); continueButton.type = "button"; continueButton.className = "primary-button";
        continueButton.textContent = "一键修改该参数并继续运行";
        continueButton.title = trialPassed === true
          ? "小预算试算已找到硬校验通过的候选"
          : "这是未验证的机会型修改，可能提高成功率，但不保证本轮一定找到候选";
        continueButton.addEventListener("click", () => applyParameterSuggestionAndContinue(item, suggestion, continueButton));
        actions.append(continueButton);
      }
      apply.textContent = preflightId ? "手动修改配置" : "手动调整此模板";
      apply.addEventListener("click", () => {
        if (preflightId) adjustPreflightSuggestion(item, suggestion, entries[0]?.field);
        else adjustFailedTemplate(item?.template_key || "", item?.plan_id || "");
      });
      actions.append(apply);
    }
    card.append(header);
    if (entries.length) card.append(diffs);
    card.append(context, actions);
    return card;
  }
  function prefillSingleTemplateTask(item) {
    const templateKey = item?.template_key || "";
    const previousMode = state.importMode;
    const matchingFiles = state.importFiles.filter((file) => file.name === `${templateKey}_m.bytes` || file.name.startsWith(`${templateKey}_`));
    resetCurrentTaskWorkspace();
    if (previousMode === "files" && matchingFiles.length) {
      state.importFiles = matchingFiles; switchImportTab("files"); renderFileList("#template-file-list", matchingFiles);
      toast(`已保留 ${templateKey} 的上传文件，请重新解析后调整参数。`);
    } else {
      $("#template-ids").value = templateKey; $("#template-id-count").textContent = templateKey ? "已输入 1 项" : "已输入 0 项";
      toast(`已建立 ${templateKey} 的单模板调整入口，请先解析模板。`);
    }
    updateImportActionState();
  }

  function candidateAdjustmentKey(item) {
    return `${item?.template_key || ""}::${item?.plan_id || item?.plan_signature || ""}`;
  }
  function currentLockedBatchRequest() {
    const candidates = [
      state.jobSnapshot?.batch_request,
      state.jobSnapshot?.locked_batch_request,
      state.jobSnapshot?.config_payload,
      state.jobResult?.batch_request,
    ];
    const locked = candidates.find((value) => value && typeof value === "object" && Array.isArray(value.batch_plans));
    if (locked) return clone(locked);
    if (!state.imported?.items?.length) return null;
    try { return buildBatchRequest({ allowRecoverableRecycleConflicts: true }); }
    catch { return null; }
  }
  function planModelFromSpec(spec = {}) {
    const plan = newPlanModel();
    const generation = spec.generation || {};
    plan.automaticGroupMaxDifference = generation.automatic_group_max_difference ?? 3;
    plan.minSameTypeLayerSpan = generation.min_same_type_layer_span ?? 0;
    const units = Array.isArray(spec.units) ? spec.units : [];
    const typeChoiceValue = generation.unit_defaults?.type_count || units[0]?.generation?.type_count || {};
    const customGroups = generation.unit_defaults?.groups_per_type || units[0]?.generation?.groups_per_type || spec.tower_generation?.groups_per_type || [];
    const uniformGroups = generation.unit_defaults?.uniform_groups_per_type ?? units[0]?.generation?.uniform_groups_per_type ?? spec.tower_generation?.uniform_groups_per_type;
    const hydratedTypeCount = hydrateTypeCountModel(typeChoiceValue, plan);
    const unitAllowedTypes = units.flatMap((unit) => unit?.generation?.allowed_types || []);
    plan.precisionMode = generation.precision_mode === true;
    plan.unitCount = clamp(units.length || 1, 1, 2);
    plan.steps = [0, 1].map((index) => {
      const raw = units[index]?.step;
      return clamp(Math.round(Number(typeof raw === "object" ? raw.value ?? raw.minimum : raw) || (index ? 6 : 5)), 4, 9);
    });
    plan.candidateCount = clamp(Math.round(Number(generation.candidate_count) || plan.candidateCount), 1, 20);
    plan.searchAttemptBudget = clamp(Math.round(Number(generation.attempt_budget) || plan.searchAttemptBudget), 1, 100000);
    plan.difficultyTarget = ["fidelity", "standard", "strong"].includes(generation.quality?.difficulty_target)
      ? generation.quality.difficulty_target
      : plan.difficultyTarget;
    plan.requiredPreGateRecycles = Math.max(0, Math.round(Number(generation.required_pre_gate_recycles) || 0));
    plan.firstRecycleLeadTiles = Math.max(0, Math.round(Number(generation.first_recycle_lead_tiles) || 2));
    plan.layout = spec.layout || plan.layout;
    plan.towerMode = spec.tower_mode || plan.towerMode;
    plan.serialDifficulty = spec.serial_difficulty || plan.serialDifficulty;
    plan.replacementSources = generation.replacement_sources ?? plan.replacementSources;
    plan.typeCountKind = hydratedTypeCount.kind;
    plan.legacyTypeCountKind = hydratedTypeCount.legacyKind;
    plan.typeMin = hydratedTypeCount.minimum;
    plan.typeMax = hydratedTypeCount.maximum;
    if (Array.isArray(customGroups) && customGroups.length) {
      plan.structureGroupMode = "custom";
      plan.customGroupsPerType = customGroups.map((value) => Math.max(1, Math.round(Number(value) || 1))).join("；");
      plan.typeCountKind = "exact";
      plan.typeMin = customGroups.length;
      plan.typeMax = customGroups.length;
    } else {
      plan.structureGroupMode = "standard";
      const parsedUniform = Number(uniformGroups);
      plan.uniformGroupsPerType = Number.isFinite(parsedUniform) ? Math.max(0, Math.round(parsedUniform)) : 1;
    }
    if (generation.max_layer_percent !== undefined) { plan.maxLayerKind = "percent"; plan.maxLayerValue = Number(generation.max_layer_percent); }
    else if (generation.max_layer !== undefined) { plan.maxLayerKind = "absolute"; plan.maxLayerValue = Number(generation.max_layer); }
    else plan.maxLayerKind = "none";
    if (generation.total_percent_limit !== undefined) { plan.totalLimitKind = "percent"; plan.totalLimitValue = Number(generation.total_percent_limit); }
    else if (generation.total_count_limit !== undefined) { plan.totalLimitKind = "absolute"; plan.totalLimitValue = Number(generation.total_count_limit); }
    else plan.totalLimitKind = "none";
    plan.totalMinimumKind = generation.total_percent_minimum != null ? "percent" : Number(generation.total_count_minimum ?? 0) > 0 ? "absolute" : "none";
    plan.totalCountMinimum = Number(generation.total_percent_minimum ?? generation.total_count_minimum ?? 0);
    if (generation.max_structure_layer_span !== undefined && generation.max_structure_layer_span !== null) {
      plan.maxStructureLayerSpanKind = "absolute";
      plan.maxStructureLayerSpan = Math.max(0, Math.round(Number(generation.max_structure_layer_span) || 0));
    } else plan.maxStructureLayerSpanKind = "none";
    plan.allowedTypes = [...new Set(generation.allowed_types || unitAllowedTypes || STRUCTURE_TYPES)].filter((value) => STRUCTURE_TYPES.includes(value));
    if (!plan.allowedTypes.length) plan.allowedTypes = [...STRUCTURE_TYPES];
    plan.quality = { ...plan.quality, ...(generation.quality || {}) };
    delete plan.quality.difficulty_target;
    if (!["auto", "intrinsic_gate", "board_reveal", "hybrid"].includes(plan.quality.experience_archetype)) plan.quality.experience_archetype = "auto";
    if (!["none", "all_types_long_range", "serial_mechanism"].includes(plan.quality.special_preset)) plan.quality.special_preset = "none";
    if (!["auto", "3", "4"].includes(String(plan.quality.long_range_wave_count ?? "auto"))) plan.quality.long_range_wave_count = "auto";
    if (!["level_200_300_v1", "off"].includes(plan.quality.online_style_reference)) plan.quality.online_style_reference = "level_200_300_v1";
    return plan;
  }
  function rawPlanContextForItem(item, request = currentLockedBatchRequest()) {
    if (!request) return null;
    const batchPlans = Array.isArray(request.batch_plans) ? request.batch_plans : [];
    const rule = (request.template_rules || []).find((entry) => entry.template_key === item?.template_key);
    const localPlans = Array.isArray(rule?.plans) ? rule.plans : [];
    const parameterSource = String(item?.parameter_source || "").toLowerCase();
    let plans = rule?.mode === "replace" ? localPlans : parameterSource.includes("batch") || !localPlans.length ? batchPlans : localPlans;
    if (!plans.length) plans = rule?.mode === "replace" ? localPlans : [...batchPlans, ...localPlans];
    const sourcePlanId = String(item?.source_plan_id || "");
    const requestedId = String(item?.plan_id || "");
    const requestedSignature = String(item?.plan_signature || "");
    const exactMatches = plans.filter((entry) => {
      const planId = String(entry.plan_id || "");
      return sourcePlanId ? planId === sourcePlanId : planId === requestedId;
    });
    const signatureMatches = exactMatches.length ? [] : plans.filter((entry) => (
      requestedSignature && planSignature(planModelFromSpec(entry)) === requestedSignature
    ));
    const matches = exactMatches.length ? exactMatches : signatureMatches;
    const spec = matches.length === 1 ? matches[0] : null;
    const effectiveSpec = spec ? { ...spec, generation: { ...spec.generation,
      automatic_group_max_difference: spec.generation?.automatic_group_max_difference ?? request.generation_defaults?.automatic_group_max_difference ?? 3,
      min_same_type_layer_span: spec.generation?.min_same_type_layer_span ?? request.generation_defaults?.min_same_type_layer_span ?? 0,
    }} : null;
    return spec ? { request, spec: effectiveSpec, sourcePlanId: String(spec.plan_id || requestedId), parameterSource } : null;
  }
  function effectivePlanSpecsForTemplate(request, templateKey) {
    const batchPlans = Array.isArray(request.batch_plans) ? request.batch_plans : [];
    const rule = (request.template_rules || []).find((entry) => entry.template_key === templateKey);
    const localPlans = Array.isArray(rule?.plans) ? rule.plans : [];
    if (rule?.mode === "replace") return clone(localPlans);
    if (rule?.mode === "append") return clone([...batchPlans, ...localPlans]);
    return clone(batchPlans);
  }
  function batchRequestWithAdjustments(entries) {
    const request = currentLockedBatchRequest();
    if (!request) throw new Error("当前任务缺少可编辑的锁定配置，请从本次会话的候选页调整，或重新打开原导入批次。");
    request.template_rules = Array.isArray(request.template_rules) ? request.template_rules : [];
    entries.forEach((entry) => {
      const plans = effectivePlanSpecsForTemplate(request, entry.templateKey);
      const targetIndex = plans.findIndex((plan) => String(plan.plan_id || "") === entry.sourcePlanId);
      if (targetIndex < 0) {
        throw new Error(`${entry.templateKey} 的原始成品方案无法唯一定位；为避免修改错误方案，本次参数未应用。`);
      }
      const existing = plans[targetIndex];
      const sourcePlanId = entry.sourcePlanId || String(existing.plan_id || `adjusted_${entry.templateKey}`);
      plans[targetIndex] = toPlanSpec(entry.plan, sourcePlanId, { allowRecoverableRecycleConflicts: true });
      request.template_rules = request.template_rules.filter((rule) => rule.template_key !== entry.templateKey);
      request.template_rules.push({ template_key: entry.templateKey, mode: "replace", plans });
    });
    return request;
  }
  function restoreAdjustmentForm() {
    const context = state.adjustmentDialog;
    if (!context) return;
    if (context.advancedSummary) context.advancedSummary.textContent = context.previousAdvancedSummary;
    const fields = $("#plan-fields");
    if (fields && context.homeParent) {
      if (context.homeNext?.parentNode === context.homeParent) context.homeParent.insertBefore(fields, context.homeNext);
      else context.homeParent.append(fields);
    }
    state.editorScope = context.previousEditorScope;
    state.activeTemplate = context.previousActiveTemplate;
    state.adjustmentDialog = null;
    $("#continuation-adjustment-scope")?.classList.add("is-hidden");
    $("#continuation-target-wrap")?.classList.add("is-hidden");
    if ($("#continuation-template-list")) $("#continuation-template-list").replaceChildren();
    if ($("#save-template-adjustment")) $("#save-template-adjustment").textContent = "保存此模板参数";
    syncPlanForm(currentEditedPlan());
    renderConfigurationSummary();
    window.setTimeout(() => context.trigger?.isConnected && context.trigger.focus(), 0);
  }
  function openTemplateAdjustment(item, trigger = null) {
    const dialog = $("#template-adjustment-dialog");
    const fields = $("#plan-fields");
    if (!dialog || !fields) return;
    const key = candidateAdjustmentKey(item);
    const staged = state.stagedTemplateAdjustments.get(key);
    const rawContext = rawPlanContextForItem(item);
    if (!staged && !rawContext) {
      toast("当前任务记录缺少可编辑的锁定方案，无法安全判断应修改哪一份参数。", "error");
      return;
    }
    const draft = clone(staged?.plan || planModelFromSpec(rawContext.spec));
    const itemScope = [item.template_key, item.plan_signature || item.plan_id].filter(Boolean).join(" · ");
    const advancedSummary = $(".advanced-panel summary > span:last-child", fields);
    state.adjustmentDialog = {
      kind: "single",
      key,
      item,
      itemScope,
      draft,
      sourcePlanId: staged?.sourcePlanId || rawContext?.sourcePlanId || item.plan_id || "",
      parameterSource: staged?.parameterSource || rawContext?.parameterSource || item.parameter_source || "",
      homeParent: fields.parentNode,
      homeNext: fields.nextSibling,
      previousEditorScope: state.editorScope,
      previousActiveTemplate: state.activeTemplate,
      advancedSummary,
      previousAdvancedSummary: advancedSummary?.textContent || "",
      trigger,
    };
    $("#template-adjustment-title").textContent = `调整 ${itemScope || item.template_key}`;
    $("#template-adjustment-copy").textContent = "保存为候选页内的独立草案；当前任务、已有候选与人工选择都不会清空。";
    $("#continuation-adjustment-scope")?.classList.add("is-hidden");
    $("#save-template-adjustment").textContent = "保存此模板参数";
    $("#template-adjustment-error").classList.add("is-hidden");
    $("#template-adjustment-error").textContent = "";
    $("#template-adjustment-form-slot").append(fields);
    if (advancedSummary) advancedSummary.textContent = "搜索预算、类型与质量偏好";
    syncPlanForm(draft);
    $("#template-adjustment-context").textContent = `${itemScope} · ${planHuman(draft)} · ${draft.candidateCount} 个候选 · ${planRecycleCopy(draft)}`;
    dialog.showModal();
    window.setTimeout(() => $("#candidate-count")?.focus(), 0);
  }
  function continuationItemsForMode(items, mode, targetKey, selectedKeys = []) {
    const keys = new Set(mode === "individual" ? [targetKey] : selectedKeys);
    return items.filter(item => keys.has(`${item?.template_key || ""}::${item?.plan_id || item?.plan_signature || ""}`));
  }
  function continuationPlanParameters(plan) {
    // Compare effective generation inputs, not inactive controls or draft identity.
    return JSON.stringify({
      towerMode: plan.towerMode,
      layout: plan.towerMode === "only" ? null : plan.unitCount === 1 ? "single" : plan.layout,
      steps: plan.towerMode === "only" ? [] : plan.steps.slice(0, plan.unitCount),
      serialDifficulty: plan.serialDifficulty,
      generation: generationSpec(plan),
    });
  }
  function continuationTargetLabel(item, draft, baseline) {
    const changed = draft && baseline && continuationPlanParameters(draft) !== continuationPlanParameters(baseline);
    return `${[item.template_key, item.plan_signature || item.plan_id].filter(Boolean).join(" · ")} · 已有 ${item.candidates?.length || 0} 个候选${changed ? "（已修改参数）" : ""}`;
  }
  function continuationSelectedItems() {
    const context = state.adjustmentDialog;
    if (context?.kind !== "continuation") return [];
    const selectedKeys = $$("#continuation-template-list input:checked").map((input) => input.value);
    const mode = $('input[name="continuation-adjustment-mode"]:checked')?.value || "uniform";
    return continuationItemsForMode(context.items, mode, context.currentKey, selectedKeys);
  }
  function continuationDraftForItem(item) {
    const context = state.adjustmentDialog;
    if (context?.kind !== "continuation") return null;
    const mode = $('input[name="continuation-adjustment-mode"]:checked')?.value || "uniform";
    return mode === "uniform" ? context.uniformDraft : context.drafts.get(candidateAdjustmentKey(item));
  }
  function updateContinuationTargetLabels() {
    const context = state.adjustmentDialog;
    if (context?.kind !== "continuation") return;
    const items = new Map(context.items.map(item => [candidateAdjustmentKey(item), item]));
    for (const option of $("#continuation-adjustment-target").options) {
      const item = items.get(option.value);
      if (item) option.textContent = continuationTargetLabel(item, context.drafts.get(option.value), context.baselines.get(option.value));
    }
  }
  function refreshContinuationTargetOptions() {
    const context = state.adjustmentDialog;
    if (context?.kind !== "continuation") return;
    const select = $("#continuation-adjustment-target");
    const individual = $('input[name="continuation-adjustment-mode"]:checked')?.value === "individual";
    const selected = individual ? context.items : continuationSelectedItems();
    $("#continuation-template-list").classList.toggle("is-hidden", individual);
    $("#continuation-target-wrap").classList.toggle("is-hidden", !individual);
    select.replaceChildren(...selected.map((item) => {
      const option = document.createElement("option");
      option.value = candidateAdjustmentKey(item);
      option.textContent = continuationTargetLabel(item, context.drafts.get(option.value), context.baselines.get(option.value));
      return option;
    }));
    if (!selected.length) {
      $("#save-template-adjustment").disabled = true;
      $("#template-adjustment-context").textContent = "请至少选择一个需要继续生成的模板。";
      return;
    }
    $("#save-template-adjustment").disabled = false;
    if (individual) switchContinuationAdjustmentTarget(context.currentKey || candidateAdjustmentKey(selected[0]));
    else {
      context.item = selected[0];
      context.currentKey = candidateAdjustmentKey(selected[0]);
      context.draft = context.uniformDraft;
      context.itemScope = `统一应用到 ${selected.length} 个成品项`;
      syncPlanForm(context.draft);
      $("#template-adjustment-context").textContent = `${context.itemScope} · ${planHuman(context.draft)} · ${context.draft.candidateCount} 个候选`;
    }
  }
  function switchContinuationAdjustmentTarget(key = $("#continuation-adjustment-target")?.value) {
    const context = state.adjustmentDialog;
    if (context?.kind !== "continuation") return;
    if (context.draft) capturePlanForm();
    const item = context.items.find((entry) => candidateAdjustmentKey(entry) === key);
    const draft = item && context.drafts.get(key);
    if (!item || !draft) return;
    context.item = item;
    context.currentKey = key;
    context.draft = draft;
    context.itemScope = [item.template_key, item.plan_signature || item.plan_id].filter(Boolean).join(" · ");
    $("#continuation-adjustment-target").value = key;
    syncPlanForm(draft);
    $("#template-adjustment-context").textContent = `${context.itemScope} · ${planHuman(draft)} · ${draft.candidateCount} 个候选 · ${planRecycleCopy(draft)}`;
  }
  function openContinuationParameterEditor(trigger = null) {
    const dialog = $("#template-adjustment-dialog");
    const fields = $("#plan-fields");
    const items = unfinishedCandidateItems().filter((item) => rawPlanContextForItem(item));
    if (!dialog || !fields || !items.length) {
      toast("当前任务没有可继续修改的未完成模板。", "error");
      return;
    }
    const contexts = new Map(items.map((item) => [candidateAdjustmentKey(item), rawPlanContextForItem(item)]));
    const baselines = new Map(items.map(item => {
      const key = candidateAdjustmentKey(item);
      return [key, planModelFromSpec(contexts.get(key).spec)];
    }));
    const drafts = new Map(items.map((item) => {
      const key = candidateAdjustmentKey(item);
      const staged = state.stagedTemplateAdjustments.get(key);
      return [key, clone(staged?.plan || baselines.get(key))];
    }));
    const first = items[0];
    const firstKey = candidateAdjustmentKey(first);
    const advancedSummary = $(".advanced-panel summary > span:last-child", fields);
    state.adjustmentDialog = {
      kind: "continuation",
      items,
      contexts,
      drafts,
      baselines,
      uniformDraft: clone(drafts.get(firstKey)),
      currentKey: firstKey,
      item: first,
      draft: null,
      itemScope: "",
      homeParent: fields.parentNode,
      homeNext: fields.nextSibling,
      previousEditorScope: state.editorScope,
      previousActiveTemplate: state.activeTemplate,
      advancedSummary,
      previousAdvancedSummary: advancedSummary?.textContent || "",
      trigger,
    };
    $("#template-adjustment-title").textContent = "修改参数并继续生成";
    const scopeLabel = $(".template-adjustment-heading .soft-label");
    if (scopeLabel) scopeLabel.textContent = "保留已有候选";
    $("#template-adjustment-copy").textContent = "选择要继续生成的模板，可统一修改，也可逐项设置；已生成候选与人工选择均会保留。";
    $("#template-adjustment-error").classList.add("is-hidden");
    $("#continuation-adjustment-scope").classList.remove("is-hidden");
    $('input[name="continuation-adjustment-mode"][value="uniform"]').checked = true;
    const list = $("#continuation-template-list");
    list.replaceChildren(...items.map((item) => {
      const key = candidateAdjustmentKey(item);
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" value="${escapeHtml(key)}" checked /><span><strong>${escapeHtml(item.template_key)}</strong><small>${escapeHtml(item.plan_signature || item.plan_id || "成品方案")} · 已有 ${item.candidates?.length || 0} 个候选</small></span>`;
      return label;
    }));
    $("#template-adjustment-form-slot").append(fields);
    if (advancedSummary) advancedSummary.textContent = "搜索预算、类型与质量偏好";
    $("#save-template-adjustment").textContent = "应用参数并继续生成";
    refreshContinuationTargetOptions();
    dialog.showModal();
    $(".template-adjustment-body", dialog).scrollTop = 0;
    window.setTimeout(() => $('input[name="continuation-adjustment-mode"]:checked')?.focus({ preventScroll: true }), 0);
  }
  function saveTemplateAdjustment() {
    const context = state.adjustmentDialog;
    if (!context) return;
    const error = $("#template-adjustment-error");
    try {
      capturePlanForm();
      if (context.kind === "continuation") {
        const selected = continuationSelectedItems();
        if (!selected.length) throw new Error("请至少选择一个需要继续生成的模板。");
        const entries = selected.map((item) => {
          const raw = context.contexts.get(candidateAdjustmentKey(item));
          const plan = clone(continuationDraftForItem(item));
          ensurePlanIsValid(plan);
          const retained = itemCandidateProgress(item);
          if (plan.candidateCount < retained.found) throw new Error(`${item.template_key} 已保留 ${retained.found} 个候选，候选数量不能调低。`);
          const inspected = state.imported?.items?.find((entry) => entry.template_key === item.template_key) || item;
          ensurePlanFitsTemplate(plan, inspected);
          return {
            templateKey: item.template_key,
            planId: item.plan_id || "",
            planSignature: item.plan_signature || "",
            sourcePlanId: raw.sourcePlanId || item.source_plan_id || item.plan_id || "",
            parameterSource: raw.parameterSource || item.parameter_source || "",
            plan,
          };
        });
        const trigger = context.trigger;
        $("#template-adjustment-dialog").close("continue");
        runStagedTemplateAdjustments(entries, { trigger, includeAllUnfinished: false });
        return;
      }
      ensurePlanIsValid(context.draft);
      const retainedProgress = itemCandidateProgress(context.item);
      if (context.draft.candidateCount < retainedProgress.found) {
        throw new Error(`当前成品项已保留 ${retainedProgress.found} 个候选；候选数量不能调低到 ${context.draft.candidateCount}。`);
      }
      const inspected = state.imported?.items?.find((item) => item.template_key === context.item.template_key) || context.item;
      ensurePlanFitsTemplate(context.draft, inspected);
      state.stagedTemplateAdjustments.set(context.key, {
        templateKey: context.item.template_key,
        planId: context.item.plan_id || "",
        planSignature: context.item.plan_signature || "",
        sourcePlanId: context.sourcePlanId,
        parameterSource: context.parameterSource,
        plan: clone(context.draft),
      });
      $("#template-adjustment-dialog").close("saved");
      renderCandidateBatchWarning();
      renderGenerationFailures(state.jobResult?.items || []);
      toast(`${context.item.template_key} 的独立参数已暂存；当前候选与选择保持不变。`);
    } catch (validationError) {
      error.textContent = validationError.message;
      error.classList.remove("is-hidden");
      error.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
  function unfinishedCandidateItems() {
    return (state.jobResult?.items || []).filter((item) => !item?.candidates?.length || itemNeedsMoreCandidates(item));
  }
  function retryScopeForItem(item, adjustment = null) {
    return {
      template_key: item?.template_key || "",
      plan_id: adjustment?.sourcePlanId || item?.source_plan_id || item?.plan_id || "",
    };
  }
  function derivedRetryScopeFields(retryScopes) {
    const scopes = [...(retryScopes || [])];
    const fields = { retry_scopes: scopes };
    // The service accepts the legacy singleton alias for compatibility, but
    // rejects it alongside a true multi-scope request because it is ambiguous.
    if (scopes.length === 1) fields.retry_scope = scopes[0];
    return fields;
  }
  async function runStagedTemplateAdjustments(entries, options = {}) {
    const selectedEntries = [...(entries || [])].filter(Boolean);
    if (!selectedEntries.length || !state.jobId) return;
    if (state.adjustmentRunPending) {
      toast("已在启动一次参数调整搜索，请等待当前请求完成。", "error");
      return;
    }
    const trigger = options.trigger instanceof HTMLElement ? options.trigger : document.activeElement?.closest?.("button");
    const parentJobId = state.jobId;
    const importId = state.jobSnapshot?.import_id || state.imported?.import_id;
    if (!importId) {
      toast("当前任务缺少可复用的导入记录，无法安全继续此模板的搜索。", "error");
      return;
    }
    const selectedKeys = new Set(selectedEntries.map((entry) => `${entry.templateKey}::${entry.planId || entry.planSignature || ""}`));
    const allUnfinished = unfinishedCandidateItems();
    const scopeItems = options.includeAllUnfinished
      ? allUnfinished
      : allUnfinished.filter((item) => selectedKeys.has(candidateAdjustmentKey(item)));
    if (!scopeItems.length) {
      toast("对应成品项已经完成，不需要再次运行。", "error");
      return;
    }
    const adjustmentByKey = new Map(selectedEntries.map((entry) => [
      `${entry.templateKey}::${entry.planId || entry.planSignature || ""}`,
      entry,
    ]));
    const retryScopes = scopeItems
      .map((item) => retryScopeForItem(item, adjustmentByKey.get(candidateAdjustmentKey(item))))
      .filter((scope) => scope.template_key);
    const inheritedResult = state.jobResult || availableResultForJob(state.jobSnapshot);
    const inheritedSelections = new Map([...state.selectedCandidates].map(([key, value]) => [key, [...value]]));
    const reviewWorkspaceVisible = !$("#candidate-workspace")?.classList.contains("is-hidden");
    const operation = beginAdjustmentRun(parentJobId);
    if (!operation) return;
    setBusy(trigger, true, selectedEntries.length === 1 ? "正在单独运行…" : "正在应用并继续…");
    try {
      const batchRequest = batchRequestWithAdjustments(selectedEntries);
      const preflight = await api("/plans/resolve", {
        method: "POST",
        body: { import_id: importId, batch_request: batchRequest },
      });
      if (!adjustmentRunContextIsCurrent(operation)) return;
      if (preflight?.valid === false) {
        const message = preflight?.error?.message || "独立参数仍有冲突，请继续调整后再运行。";
        throw new Error(message);
      }
      const createBody = {
        import_id: importId,
        parent_job_id: parentJobId,
        batch_request: batchRequest,
        ...derivedRetryScopeFields(retryScopes),
      };
      if (Number.isFinite(Number(options.multiplier))) {
        createBody.attempt_budget_multiplier = Math.max(1, Math.round(Number(options.multiplier)));
      }
      const job = await api("/jobs", { method: "POST", body: createBody });
      if (!adjustmentRunContextIsCurrent(operation)) {
        toast("调整后的搜索任务已在后台创建；当前工作区已切换，未自动打开该任务。");
        return;
      }
      if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
      state.jobId = job.job_id;
      state.jobSnapshot = job;
      state.jobResult = inheritedResult;
      state.jobStartedAt = job.started_at || job.created_at || new Date().toISOString();
      state.jobRecommendationPollAttempts = 0;
      state.jobPollFailures = 0;
      state.pendingUsePartialReview = false;
      state.isRecoveredJob = false;
      state.selectedCandidates = inheritedSelections;
      state.selectionsByJob.set(state.jobId, state.selectedCandidates);
      persistSelections(state.jobId);
      selectedEntries.forEach((entry) => {
        const key = `${entry.templateKey}::${entry.planId || entry.planSignature || ""}`;
        state.stagedTemplateAdjustments.delete(key);
        state.runningAdjustmentKeys.add(key);
      });
      state.completedSteps.add(2);
      unlockFlow(3, { preserveScroll: true });
      renderGenerationProgress(job);
      if (reviewWorkspaceVisible && inheritedResult) {
        $("#generation-state")?.classList.add("is-hidden");
        $("#candidate-workspace")?.classList.remove("is-hidden");
        renderCandidateWorkspace();
      } else {
        $("#generation-state")?.classList.remove("is-hidden");
      }
      pollJob(job.job_id);
      const scopeCopy = retryScopes.length === 1 ? "当前成品项" : `${retryScopes.length} 个未完成成品项`;
      toast(`已继续搜索${scopeCopy}；已有候选、进度与人工选择均已保留。`);
    } catch (error) {
      if (adjustmentRunContextIsCurrent(operation)) toast(error.message, "error");
    } finally {
      setBusy(trigger, false);
      endAdjustmentRun(operation);
    }
  }
  function runSingleStagedTemplate(key, trigger = null) {
    const entry = state.stagedTemplateAdjustments.get(key);
    if (!entry) {
      toast("请先保存此模板的独立参数。", "error");
      return;
    }
    runStagedTemplateAdjustments([entry], { trigger, includeAllUnfinished: false });
  }
  function adjustFailedTemplate(templateKey, planId = "") {
    const availableResult = state.jobResult || availableResultForJob(state.jobSnapshot);
    const item = (availableResult?.items || []).find((entry) => entry.template_key === templateKey && (!planId || entry.plan_id === planId))
      || (availableResult?.items || []).find((entry) => entry.template_key === templateKey);
    if (!item) { toast("未找到对应失败成品项，请从任务记录重新打开。", "error"); return; }
    openTemplateAdjustment(item, document.activeElement?.closest?.("button"));
  }
  function renderGenerationFailures(items) {
    const root = $("#generation-failure-details"); if (!root) return;
    const failures = (items || []).filter((item) => !item.candidates?.length || itemNeedsMoreCandidates(item));
    root.classList.toggle("is-hidden", failures.length === 0);
    if (!failures.length) { root.replaceChildren(); return; }
    const title = document.createElement("strong"); title.textContent = `${failures.length} 个成品项未完成`;
    const list = document.createElement("ul");
    const appendFailure = (target, item) => {
      const entry = document.createElement("li");
      const copy = document.createElement("span");
      const progress = itemCandidateProgress(item);
      const progressCopy = `已找到 ${progress.found}${progress.target !== null ? ` / ${progress.target}` : ""} 个候选`;
      const reasonCopy = itemNeedsMoreCandidates(item) ? "已有候选会保留，可继续搜索未完成数量。" : `${failureStatusCopy(item)}。${itemErrorCopy(item)}`;
      copy.textContent = `${item.template_key || "未知模板"} · ${item.plan_signature || item.plan_id || "未知方案"}：${progressCopy}；${reasonCopy}`;
      entry.append(copy);
      if (isDeterministicFailure(item)) {
        const adjust = document.createElement("button"); adjust.type = "button"; adjust.className = "text-button";
        adjust.dataset.adjustFailedTemplate = item.template_key; adjust.dataset.failedPlanId = item.plan_id || "";
        adjust.textContent = "调整此模板参数"; entry.append(adjust);
      } else if (isBudgetFailure(item) && jobCanContinueSearch(state.jobSnapshot)) {
        const status = document.createElement("span"); status.className = "status-chip";
        status.textContent = "可继续搜索未完成项"; entry.append(status);
      } else if (isBudgetFailure(item)) {
        const adjust = document.createElement("button"); adjust.type = "button"; adjust.className = "text-button";
        adjust.dataset.adjustFailedTemplate = item.template_key; adjust.dataset.failedPlanId = item.plan_id || "";
        adjust.textContent = "调整此模板参数"; entry.append(adjust);
      }
      target.append(entry);
    };
    failures.slice(0, 4).forEach((item) => appendFailure(list, item));
    root.replaceChildren(title, list);
    if (failures.length > 4) {
      const overflow = document.createElement("details"); overflow.className = "failure-overflow";
      const summary = document.createElement("summary"); summary.textContent = `展开其余 ${failures.length - 4} 项`;
      const overflowList = document.createElement("ul");
      failures.slice(4).forEach((item) => appendFailure(overflowList, item));
      overflow.append(summary, overflowList); root.append(overflow);
    }
    const suggestionStack = document.createElement("div"); suggestionStack.className = "candidate-suggestion-stack";
    failures.forEach((item) => {
      rankedApplicableParameterSuggestions(item).slice(0, 2)
        .forEach((suggestion) => suggestionStack.append(createParameterSuggestionCard(item, suggestion)));
    });
    if (suggestionStack.childElementCount) root.append(suggestionStack);
    if (jobRecommendationsPending(state.jobSnapshot)) {
      const pending = document.createElement("p"); pending.className = "parameter-suggestion-pending";
      pending.setAttribute("role", "status"); pending.textContent = "正在用小预算试算可行的参数修改，结果会自动更新…"; root.append(pending);
    }
  }
  async function pollJob(jobId) {
    try {
      const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
      if (state.jobId !== jobId) return;
      state.jobPollFailures = 0;
      renderGenerationProgress(job);
      if (["queued", "running", "pause_requested", "paused", "cancel_requested"].includes(job.status)) {
        const liveResult = availableResultForJob(job);
        if (liveResult) state.jobResult = liveResult;
        if (!$("#candidate-workspace")?.classList.contains("is-hidden") && state.jobResult) {
          initializeDefaultSelections();
          renderCandidateWorkspace();
        }
        state.jobPollTimer = window.setTimeout(() => pollJob(jobId), 900);
        return;
      }
      state.jobResult = availableResultForJob(job);
      state.runningAdjustmentKeys.clear();
      if (state.jobResult?.batch_seed !== undefined) {
        state.seed = String(state.jobResult.batch_seed); state.seedWasAutoResolved = true; $("#batch-seed").value = state.seed;
      }
      if (state.adjustmentContext?.parentJobId === jobId) {
        toast("原任务已结束；当前单模板参数编辑保留，生成后会继承其他候选。");
        return;
      }
      const usableCount = successfulWorkItems().reduce((sum, item) => sum + (item.candidates?.length || 0), 0);
      const finalizePartialReview = state.pendingUsePartialReview
        && ["completed", "partial"].includes(job.status)
        && successfulWorkItems().length > 0;
      const incompleteCandidateCount = successfulWorkItems().filter(itemNeedsMoreCandidates).length;
      const offerTerminalChoice = ["cancelled", "failed"].includes(job.status)
        || (job.status === "partial" && incompleteCandidateCount > 0);
      const isRecommendationRefresh = state.jobRecommendationPollAttempts > 0;
      const reviewWorkspaceVisible = ["#candidate-workspace", "#compare-workspace", "#focus-workspace"]
        .some((selector) => !$(selector).classList.contains("is-hidden"));
      if (reviewWorkspaceVisible && state.jobResult?.items?.length) {
        initializeDefaultSelections();
        renderCandidateWorkspace();
      }
      if (finalizePartialReview) {
        state.pendingUsePartialReview = false;
        enterAvailableCandidateReview();
      } else if (successfulWorkItems().length && !offerTerminalChoice) {
        initializeDefaultSelections();
        state.completedSteps.add(3);
        unlockFlow(4);
        renderCandidateWorkspace();
        if (!reviewWorkspaceVisible) {
          $("#generation-state").classList.add("is-hidden"); $("#candidate-workspace").classList.remove("is-hidden");
          setFlow(3);
        }
        const missingCount = (state.jobResult?.items || []).filter((item) => !item.candidates?.length).length;
        if (!isRecommendationRefresh) toast(`候选任务结束：已找到 ${usableCount} 个候选${missingCount ? `，仍有 ${missingCount} 个成品项未完成` : ""}。`);
      } else {
        if (state.pendingUsePartialReview && ["cancelled", "failed"].includes(job.status) && usableCount === 0) {
          state.pendingUsePartialReview = false;
        }
        renderGenerationProgress(job);
        if (!isRecommendationRefresh && usableCount > 0) toast(`已保留 ${usableCount} 个候选；可使用已有结果，或继续处理未完成数量。`);
        else if (!isRecommendationRefresh && job.error?.message) toast(job.error.message, "error");
      }
      const shouldPollRecommendations = jobRecommendationsPending(job);
      if (shouldPollRecommendations) {
        state.jobRecommendationPollAttempts += 1;
        const requestedDelay = clamp(Number(job.recommendation_poll_after_ms || 1000), 750, 3000);
        const delay = Math.min(10_000, Math.round(requestedDelay * (1.35 ** Math.min(state.jobRecommendationPollAttempts - 1, 8))));
        state.jobPollTimer = window.setTimeout(() => pollJob(jobId), delay);
      } else if (!jobRecommendationsPending(job)) {
        state.jobRecommendationPollAttempts = 0;
      }
    } catch (error) {
      if (state.jobId !== jobId) return;
      state.jobPollFailures += 1;
      const delay = Math.min(10_000, 900 * (2 ** Math.min(state.jobPollFailures, 4)));
      $("#generation-status-text").textContent = `连接暂时中断，后台任务可能仍在运行；${Math.ceil(delay / 1000)} 秒后自动重连。`;
      const health = $("#generation-health");
      health.className = "health-chip failed"; health.innerHTML = "<i></i>连接中断";
      if (state.jobPollFailures === 1) toast(error.message, "error");
      state.jobPollTimer = window.setTimeout(() => pollJob(jobId), delay);
    }
  }
  async function controlCurrentJob(action) {
    if (!state.jobId) return;
    try {
      const job = await api(`/jobs/${encodeURIComponent(state.jobId)}/control`, { method: "POST", body: { action } });
      if (action === "retry") {
        $("#candidate-workspace").classList.add("is-hidden");
        $("#compare-workspace").classList.add("is-hidden");
        $("#focus-workspace").classList.add("is-hidden");
        $("#generation-state").classList.remove("is-hidden");
      }
      renderGenerationProgress(job);
      if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
      pollJob(state.jobId);
    } catch (error) { toast(error.message, "error"); }
  }
  function isUnsupportedResumeActionError(error) {
    const code = String(error?.code || error?.detail?.code || "").toUpperCase();
    const message = String(error?.message || error?.detail?.message || "");
    if (["UNSUPPORTED_ACTION", "ACTION_NOT_SUPPORTED", "UNKNOWN_ACTION", "METHOD_NOT_ALLOWED"].includes(code)) return true;
    return /unsupported\s*action|unknown\s*action|不支持.{0,8}(?:action|操作|动作)|未知.{0,8}(?:action|操作|动作)/i.test(message);
  }
  async function continueCurrentJobSearch(multiplier = null, explicitTrigger = null) {
    if (!state.jobId) return;
    if (state.taskControlsPending.has(`generation:${state.jobId}`)) return;
    if (state.adjustmentRunPending) {
      toast("已在启动一次搜索，请等待当前请求完成。", "error");
      return;
    }
    const parentJobId = state.jobId;
    const trigger = explicitTrigger instanceof HTMLElement ? explicitTrigger : document.activeElement?.closest?.("button");
    const { recommended, limit } = jobSearchBudget(state.jobSnapshot);
    const requestedMultiplier = typeof multiplier === "number" && Number.isFinite(multiplier) ? multiplier : null;
    const chosenMultiplier = clamp(Math.round(Number(requestedMultiplier || state.resumeBudgetMultiplier || recommended)), 1, limit);
    state.resumeBudgetMultiplier = chosenMultiplier;
    const unfinishedKeys = new Set(unfinishedCandidateItems().map(candidateAdjustmentKey));
    [...state.stagedTemplateAdjustments.keys()].forEach((key) => {
      if (!unfinishedKeys.has(key)) state.stagedTemplateAdjustments.delete(key);
    });
    const stagedEntries = [...state.stagedTemplateAdjustments.values()];
    if (stagedEntries.length) {
      await runStagedTemplateAdjustments(stagedEntries, {
        trigger,
        multiplier: chosenMultiplier,
        includeAllUnfinished: true,
      });
      return;
    }
    const operation = beginAdjustmentRun(parentJobId);
    if (!operation) return;
    setBusy(trigger, true, "正在继续…");
    state.pendingUsePartialReview = false;
    let lastError = null;
    const actions = ["resume_unfinished", "continue_search", "retry_incomplete", "retry"];
    try {
      for (let index = 0; index < actions.length; index += 1) {
        const action = actions[index];
        try {
          const body = { action, attempt_budget_multiplier: chosenMultiplier };
          const job = await api(`/jobs/${encodeURIComponent(parentJobId)}/control`, { method: "POST", body });
          if (!adjustmentRunContextIsCurrent(operation)) {
            toast("原任务已在后台继续搜索；当前工作区已切换，未自动打开该任务。");
            return;
          }
          $("#candidate-workspace").classList.add("is-hidden");
          $("#compare-workspace").classList.add("is-hidden");
          $("#focus-workspace").classList.add("is-hidden");
          $("#generation-state").classList.remove("is-hidden");
          renderGenerationProgress(job);
          if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
          pollJob(parentJobId);
          const actualMultiplier = Number(job?.search?.attempt_budget_multiplier ?? job?.summary?.search?.attempt_budget_multiplier);
          const budgetCopy = Number.isFinite(actualMultiplier) ? `${Math.round(actualMultiplier)}× 预算` : "服务端确认的预算";
          toast(`已保留现有候选，并按${budgetCopy}继续搜索未完成项。`);
          return;
        } catch (error) {
          if (!adjustmentRunContextIsCurrent(operation)) return;
          lastError = error;
          if (!isUnsupportedResumeActionError(error) || index === actions.length - 1) break;
        }
      }
      if (adjustmentRunContextIsCurrent(operation)) {
        toast(lastError?.message || "当前任务无法继续运行。", "error");
      }
    } finally {
      setBusy(trigger, false);
      endAdjustmentRun(operation);
    }
  }
  function enterAvailableCandidateReview() {
    const result = availableResultForJob(state.jobSnapshot);
    const count = availableCandidatesForJob(state.jobSnapshot);
    if (!result || count <= 0) { toast("当前还没有可使用的候选。", "error"); return; }
    state.jobResult = result;
    state.activeWorkIndex = 0;
    state.candidatePage = 0;
    state.compareIds.clear();
    initializeDefaultSelections();
    if (!["queued", "running", "paused", "pause_requested", "cancel_requested"].includes(state.jobSnapshot?.status)) state.completedSteps.add(3);
    unlockFlow(4);
    renderCandidateWorkspace();
    $("#generation-state").classList.add("is-hidden");
    $("#candidate-workspace").classList.remove("is-hidden");
    setFlow(3);
    toast(`已进入 ${count} 个已找到候选的后续流程；未完成数量不会阻塞已有结果。`);
  }
  async function useAvailableCandidates() {
    const status = state.jobSnapshot?.status;
    if (["completed", "partial"].includes(status)) { enterAvailableCandidateReview(); return; }
    if (!state.jobId || !state.jobSnapshot?.actions?.can_use_partial_results) {
      toast("当前任务尚无可导出的结果，请等待候选生成后再试。", "error"); return;
    }
    const trigger = $("#generation-use-partial");
    setBusy(trigger, true, "正在保留…");
    state.pendingUsePartialReview = true;
    try {
      const job = await api(`/jobs/${encodeURIComponent(state.jobId)}/control`, { method: "POST", body: { action: "finish_partial" } });
      state.jobSnapshot = job;
      state.jobResult = availableResultForJob(job) || state.jobResult;
      renderGenerationProgress(job);
      if (["completed", "partial"].includes(job.status) && availableCandidatesForJob(job) > 0) {
        state.pendingUsePartialReview = false;
        enterAvailableCandidateReview();
      } else {
        if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
        pollJob(state.jobId);
        toast("正在安全结束搜索，已找到的候选会保留。");
      }
    } catch (error) {
      state.pendingUsePartialReview = false;
      toast(error.message, "error");
    } finally { setBusy(trigger, false); }
  }

  function successfulWorkItems() { return (state.jobResult?.items || []).filter((item) => item.candidates?.length); }
  function workKey(item) { return `${item.template_key}::${item.plan_id}`; }
  function activeWorkItem() {
    const items = successfulWorkItems(); state.activeWorkIndex = clamp(state.activeWorkIndex, 0, Math.max(0, items.length - 1)); return items[state.activeWorkIndex] || null;
  }
  function sourceTemplateMetrics(item, generated) {
    const inspection = item?.inspection || generated?.inspection || generated?.candidate?.inspection || {};
    const metric = (key) => {
      const raw = inspection[key];
      if (raw === null || raw === undefined || raw === "") return "—";
      const value = Number(raw);
      return Number.isFinite(value) ? value : "—";
    };
    return {
      tileCount: metric("tile_count"),
      nonTowerTileCount: metric("non_tower_tile_count"),
      maxLayer: metric("max_layer"),
      nonTowerMaxLayer: metric("non_tower_max_layer"),
    };
  }
  function candidateStructureLayerMetrics(generated) {
    const replacements = Array.isArray(generated?.candidate?.replacements)
      ? generated.candidate.replacements
      : [];
    const layers = replacements
      .map((replacement) => replacement?.z)
      .filter((value) => value !== null && value !== undefined && value !== "")
      .map((value) => Number(value) + 1)
      .filter(Number.isFinite);
    if (layers.length) {
      const minimum = Math.min(...layers);
      const maximum = Math.max(...layers);
      return { minimum, maximum, span: maximum - minimum };
    }

    const summary = generated?.board?.validation_summary || generated?.validation || {};
    const metrics = generated?.candidate?.quality_metrics || generated?.quality_metrics || summary.quality_metrics || {};
    const finiteMetric = (...values) => {
      const raw = values.find((value) => value !== null && value !== undefined && value !== "");
      if (raw === undefined) return null;
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    };
    const maximum = finiteMetric(metrics.structure_max_layer, summary.highest_structure_layer);
    const span = finiteMetric(metrics.structure_layer_span);
    const minimum = finiteMetric(metrics.structure_min_layer, maximum !== null && span !== null ? maximum - span : null);
    return {
      minimum: minimum ?? "—",
      maximum: maximum ?? "—",
      span: span ?? (minimum !== null && maximum !== null ? maximum - minimum : "—"),
    };
  }
  function candidateId(generated) { return generated?.candidate?.candidate_id || generated?.board?.candidate?.candidate_id || ""; }
  function candidateSummary(generated) { return generated?.board?.validation_summary || {}; }
  function candidateSteps(generated) { return (generated?.candidate?.units || []).map((unit) => unit.target_step).join(" + ") || "塔内"; }
  function displayCandidateIndex(generated) { return Math.max(1, Number(generated?.candidate?.candidate_index) || 1); }
  function qualityScore(generated) {
    const value = Number(generated?.candidate?.quality_score ?? generated?.quality_score ?? candidateSummary(generated).quality_score);
    return Number.isFinite(value) ? value : 0;
  }
  function qualityMetrics(generated) {
    return generated?.candidate?.quality_metrics || generated?.quality_metrics || candidateSummary(generated).quality_metrics || {};
  }
  function qualityMetricValue(generated, key) {
    const raw = qualityMetrics(generated)?.[key];
    if (raw === null || raw === undefined || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }
  function qualityMetricCopy(value, options = {}) {
    if (!Number.isFinite(value)) return "未返回";
    if (options.percent) return `${(value * 100).toFixed(options.digits ?? 1)}%`;
    return `${value.toFixed(options.digits ?? 3)}${options.suffix || ""}`;
  }
  function qualityFormulaMarkup(generated) {
    const score = qualityScore(generated);
    const layout = qualityMetricValue(generated, "layout_score");
    const layoutTieBreak = Number.isFinite(layout) ? layout : score;
    const formula = `<div class="quality-equation is-total-only" aria-label="布局打平分"><span class="quality-equation-total"><small>布局打平分</small><b>${qualityMetricCopy(layoutTieBreak)}</b></span><p>只在硬校验、原型、机制压力、难度距离和线上风格相同后用于排序。</p></div>`;
    const scoreRows = [
      ["布局打平分", layoutTieBreak],
    ].filter(([, value]) => Number.isFinite(value));
    const contextualLabel = (label, rawValue, options = {}) => Number.isFinite(rawValue)
      ? `${label}（原始值 ${qualityMetricCopy(rawValue, options)}）`
      : label;
    const layoutContributions = [
      [contextualLabel("居中贡献", qualityMetricValue(generated, "center_distance"), { digits: 3 }), qualityMetricValue(generated, "center_layout_contribution")],
      [contextualLabel("对称贡献", qualityMetricValue(generated, "symmetry_ratio"), { percent: true, digits: 1 }), qualityMetricValue(generated, "symmetry_layout_contribution")],
      [contextualLabel("疏密贡献", qualityMetricValue(generated, "average_pair_distance"), { digits: 3 }), qualityMetricValue(generated, "density_layout_contribution")],
    ].filter(([, value]) => Number.isFinite(value));
    const fallbackDimensions = [
      ["中心距离", qualityMetricValue(generated, "center_distance"), { digits: 3 }],
      ["对称比例", qualityMetricValue(generated, "symmetry_ratio"), { percent: true, digits: 1 }],
      ["平均平面间距", qualityMetricValue(generated, "average_pair_distance"), { digits: 3 }],
      ["结构块层间距", qualityMetricValue(generated, "structure_layer_span"), { digits: 0, suffix: " 层" }],
    ].filter(([, value]) => Number.isFinite(value));
    const rowsMarkup = (rows) => rows.map(([label, value, options]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(qualityMetricCopy(value, options))}</dd></div>`).join("");
    return `${formula}<section class="quality-score-section"><h4>分项总览</h4><dl>${rowsMarkup(scoreRows)}</dl></section>${layoutContributions.length ? `<section class="quality-score-section"><h4>布局得分组成</h4><dl>${rowsMarkup(layoutContributions)}</dl></section>` : ""}${!layoutContributions.length && fallbackDimensions.length ? `<section class="quality-score-section"><h4>原始布局维度</h4><dl>${rowsMarkup(fallbackDimensions)}</dl></section>` : ""}`;
  }
  function firstDifficultyValue(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }
  function difficultyNumber(...values) {
    const raw = firstDifficultyValue(...values);
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }
  function difficultyTargetCopy(target) {
    return ({ fidelity: "难度保真", standard: "标准关键结构", strong: "强卡点" })[target] || "难度保真";
  }
  function difficultyPercent(value) {
    if (!Number.isFinite(value)) return "—";
    const normalized = Math.abs(value) <= 1 ? value * 100 : value;
    return `${normalized.toFixed(normalized < 10 ? 1 : 0)}%`;
  }
  function difficultyPosition(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? `第 ${Math.max(0, Math.round(number))} 步` : String(value);
  }
  function candidateDifficultyReport(generated) {
    const candidate = generated?.candidate || {};
    const report = candidate.difficulty_simulation
      || generated?.difficulty_simulation
      || candidate.difficulty_report
      || generated?.difficulty_report
      || {};
    const duplicates = firstDifficultyValue(
      report.duplicates,
      report.duplicate_results,
      report.dup_para_results,
      report.per_duplicate,
    );
    const normalizedDuplicates = Array.isArray(duplicates) ? duplicates : [];
    const coverageRaw = String(firstDifficultyValue(
      report.coverage,
      report.coverage_status,
      report.parameter_coverage,
      report.source?.coverage,
      "",
    ) || "").toLowerCase();
    const coverageComplete = coverageRaw === "complete";
    const missingCoverage = !coverageComplete;
    const coverageLabel = coverageComplete
      ? "DupPara1–10 已完整评估"
      : coverageRaw === "ambiguous"
        ? "副本参数存在多条记录，使用结构参考（未核对副本难度）"
        : coverageRaw === "fallback"
          ? "未找到完整副本参数，使用结构参考（未核对副本难度）"
          : "未找到副本参数，使用结构参考";
    const modernDistance = report.difficulty_distance;
    const distance = difficultyNumber(
      modernDistance?.primary_distance,
      modernDistance?.total,
      typeof modernDistance === "number" ? modernDistance : null,
      report.source_distance,
      candidate.difficulty_distance,
      generated?.difficulty_distance,
      // Compatibility only: old records exposed a legacy mean score here.
      report.source_difficulty_distance,
    );
    const trendRaw = firstDifficultyValue(report.trend, report.duplicate_trend, candidate.duplicate_trend, generated?.duplicate_trend);
    const trendOk = typeof trendRaw === "object"
      ? firstDifficultyValue(trendRaw.safety_qualified, trendRaw.becomes_easier, trendRaw.passed, trendRaw.ok, trendRaw.monotonic, trendRaw.is_easier)
      : typeof trendRaw === "boolean" ? trendRaw : null;
    const trendLabel = trendOk === true
      ? "由前向后逐渐变易"
      : trendOk === false ? "存在后段变难" : typeof trendRaw === "string" && trendRaw ? trendRaw : "未评估";
    const downgradeReasonsRaw = firstDifficultyValue(report.downgrade_reasons, report.warnings, report.warning, report.source?.warning, []);
    const downgradeReasons = Array.isArray(downgradeReasonsRaw) ? downgradeReasonsRaw.map(String) : downgradeReasonsRaw ? [String(downgradeReasonsRaw)] : [];
    const trendViolations = Array.isArray(trendRaw?.violations) ? trendRaw.violations : [];
    trendViolations.forEach((violation) => {
      const copy = typeof violation === "string"
        ? violation
        : firstDifficultyValue(violation?.message, violation?.reason, violation?.copy, JSON.stringify(violation));
      if (copy && !downgradeReasons.includes(String(copy))) downgradeReasons.push(String(copy));
    });
    return {
      raw: report,
      target: firstDifficultyValue(report.target, report.difficulty_target, candidate.difficulty_target, generated?.difficulty_target, "fidelity"),
      distance,
      duplicates: normalizedDuplicates,
      missingCoverage,
      coverageLabel,
      trendLabel,
      trendOk,
      qualified: firstDifficultyValue(report.qualified, report.passed, report.target_qualified),
      downgradeReasons,
      configCoverage: String(report.evidence_coverage?.config_coverage || ""),
      materializationFidelity: String(report.evidence_coverage?.materialization_fidelity || ""),
    };
  }
  function hasReliableDifficultyDistance(report) {
    if (report?.distance === null || report?.distance === undefined || !Number.isFinite(Number(report.distance))) return false;
    const configCoverage = String(report.configCoverage || report.evidenceCoverage?.config_coverage || "");
    const materialization = String(report.materializationFidelity || report.evidenceCoverage?.materialization_fidelity || "");
    return Boolean(configCoverage)
      && Boolean(materialization)
      && !["missing", "fallback", "ambiguous"].includes(configCoverage)
      && !["synthetic", "unknown", "missing"].includes(materialization);
  }
  function difficultyDistanceCopy(report) {
    return hasReliableDifficultyDistance(report) ? Number(report.distance).toFixed(2) : "未形成可比较结果";
  }
  function duplicateDifficultyValue(entry, key) {
    const strategies = entry?.strategies || entry?.strategy_results || {};
    const aliases = {
      nakedPass: [entry?.naked_pass_rate, entry?.naked_clear_rate, entry?.naked_pass_probability, entry?.naked_pass, strategies?.naked_pass_rate],
      bagFailure: [entry?.capacity7_failure_path_ratio, entry?.capacity_7_failure_rate, entry?.seven_slot_failure_rate, entry?.bag7_failure_rate, entry?.failure_path_ratio, entry?.capacity7?.failure_rate],
      firstRecycle: [entry?.first_recycle_click, entry?.first_recycle_position, entry?.first_match_click],
      firstFullBag: [entry?.first_full_bag_click, entry?.first_full_bag_position, entry?.capacity7?.first_full_bag_click],
    };
    return firstDifficultyValue(...(aliases[key] || []));
  }
  function difficultyStrategyRows(report) {
    const direct = report.raw?.strategies || report.raw?.strategy_results || report.raw?.strategy_summary || {};
    const aliases = [
      ["random", "随机策略"],
      ["greedy", "贪心策略"],
      ["local_optimal", "局部最优"],
      ["unlock", "解锁优先"],
      ["adversarial", "压力测试"],
    ];
    return aliases.map(([key, label]) => {
      const value = direct[key] ?? direct[key === "local_optimal" ? "local" : key] ?? {};
      const pass = difficultyNumber(value?.pass_probability, value?.naked_pass_rate, value?.pass_rate, value?.clear_rate, typeof value === "number" ? value : null);
      const failure = difficultyNumber(value?.capacity7_failure_path_ratio, value?.capacity_7_failure_rate, value?.failure_rate, value?.bag7_failure_rate);
      if (pass !== null || failure !== null) return { label, pass, failure };
      const duplicateValues = report.duplicates
        .map((entry) => entry?.strategies?.[key] ?? entry?.strategy_results?.[key])
        .filter((entry) => entry !== undefined && entry !== null);
      const average = (fieldAliases) => {
        const values = duplicateValues.map((entry) => {
          if (typeof entry === "boolean") return entry ? 1 : 0;
          if (typeof entry === "number") return entry;
          return difficultyNumber(...fieldAliases.map((field) => entry?.[field]));
        }).filter(Number.isFinite);
        return values.length ? values.reduce((sum, item) => sum + item, 0) / values.length : null;
      };
      return { label, pass: average(["pass_probability", "naked_pass_rate", "pass_rate", "clear_rate", "completed", "passed", "success"]), failure: average(["capacity7_failure_path_ratio", "capacity_7_failure_rate", "failure_rate", "bag7_failure_rate"]) };
    });
  }
  function difficultyStrategyCell(entry) {
    const strategies = entry?.strategies || entry?.strategy_results || {};
    const outcome = (value) => {
      const raw = typeof value === "object" && value !== null
        ? firstDifficultyValue(value.pass_probability, value.completed, value.passed, value.success, value.clear, value.naked_pass_rate, value.pass_rate)
        : value;
      if (typeof raw === "boolean") return raw ? "通过" : "失败";
      const number = Number(raw);
      return Number.isFinite(number) ? difficultyPercent(number) : "—";
    };
    return `随机 ${outcome(strategies.random)} · 贪心 ${outcome(strategies.greedy)} · 局优 ${outcome(strategies.local_optimal ?? strategies.local)} · 解锁 ${outcome(strategies.unlock)} · 压力 ${outcome(strategies.adversarial)}`;
  }
  function difficultyCompactMarkup(generated) {
    const report = candidateDifficultyReport(generated);
    const distanceCopy = report.distance === null ? "难度距离待评估" : `与原模板距离 ${difficultyDistanceCopy(report)}`;
    const stateClass = report.missingCoverage ? " is-reference" : report.trendOk === false ? " is-warning" : "";
    return `<div class="candidate-difficulty-strip${stateClass}"><strong>${escapeHtml(difficultyTargetCopy(report.target))}</strong><span>${escapeHtml(report.missingCoverage ? report.coverageLabel : `${distanceCopy} · ${report.trendLabel}`)}</span></div>`;
  }
  function difficultyReportMarkup(generated) {
    const report = candidateDifficultyReport(generated);
    const strategies = difficultyStrategyRows(report);
    const duplicateRows = report.duplicates.map((entry, index) => {
      const duplicateIndex = firstDifficultyValue(entry?.duplicate_index, entry?.dup_index, entry?.index, entry?.name, index + 1);
      const naked = difficultyNumber(duplicateDifficultyValue(entry, "nakedPass"));
      const failure = difficultyNumber(duplicateDifficultyValue(entry, "bagFailure"));
      return `<tr><th scope="row">${escapeHtml(String(duplicateIndex).startsWith("Dup") ? duplicateIndex : `DupPara${duplicateIndex}`)}</th><td>${escapeHtml(difficultyPercent(naked))}</td><td>${escapeHtml(difficultyPercent(failure))}</td><td>${escapeHtml(difficultyPosition(duplicateDifficultyValue(entry, "firstRecycle")))}</td><td>${escapeHtml(difficultyPosition(duplicateDifficultyValue(entry, "firstFullBag")))}</td><td>${escapeHtml(difficultyStrategyCell(entry))}</td></tr>`;
    }).join("");
    const strategyRows = strategies.map((entry) => {
      const metrics = [];
      if (entry.pass !== null) metrics.push(`样本通过率 ${difficultyPercent(entry.pass)}`);
      if (entry.failure !== null) metrics.push(`7 格失败率 ${difficultyPercent(entry.failure)}`);
      return `<div><dt>${escapeHtml(entry.label)}</dt><dd>${escapeHtml(metrics.join(" · ") || "暂无独立策略样本")}</dd></div>`;
    }).join("");
    const statusCopy = report.qualified === false ? "未达到当前难度目标" : report.qualified === true ? "已达到当前难度目标" : "当前难度目标待评估";
    const distanceCopy = difficultyDistanceCopy(report);
    const warnings = report.downgradeReasons.length
      ? `<ul class="difficulty-warning-list">${report.downgradeReasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
      : "";
    const table = duplicateRows
      ? `<div class="difficulty-table-wrap"><table class="difficulty-table"><thead><tr><th>副本</th><th>样本通过率</th><th>7 格样本失败率</th><th>首次回收</th><th>首次满包</th><th>五类模拟策略</th></tr></thead><tbody>${duplicateRows}</tbody></table></div>`
      : `<p class="difficulty-empty-copy">${escapeHtml(report.coverageLabel)}</p>`;
    return `<div class="difficulty-report-summary"><div><span>难度目标</span><strong>${escapeHtml(difficultyTargetCopy(report.target))}</strong></div><div><span>与原模板难度距离</span><strong>${escapeHtml(distanceCopy)}</strong></div><div><span>DupPara1–10 走势</span><strong>${escapeHtml(report.trendLabel)}</strong></div></div><p class="difficulty-coverage-copy${report.missingCoverage ? " is-reference" : ""}">${escapeHtml(report.coverageLabel)} · ${escapeHtml(statusCopy)}</p><section class="difficulty-strategy-summary"><h4>策略模拟</h4><dl>${strategyRows}</dl></section>${table}${warnings}`;
  }
  const EXPERIENCE_ARCHETYPE_COPY = {
    auto: "自动判断",
    intrinsic_gate: "内生门槛型",
    board_reveal: "整板揭露型",
    hybrid: "混合型",
    all_types_long_range: "全类型长程型",
    serial_mechanism: "串行机关型",
    uncertain: "未形成可靠分类",
  };
  const EXPERIENCE_FIELD_COPY = {
    status: "状态",
    passed: "是否通过",
    reasons: "依据",
    reason: "原因",
    requested: "设置原型",
    primary: "判定原型",
    automatic_primary: "自动判定",
    scores: "原型得分",
    classification_confidence: "分类可信度",
    classification_status: "分类状态",
    minimum_confidence: "最低分类置信度",
    top2: "候选原型 Top 2",
    archetype: "候选原型",
    confidence: "可信度",
    confidence_status: "可信程度",
    explicitly_requested: "是否手动指定",
    intrinsic_strength: "内生门槛强度",
    reveal_strength: "整板揭露强度",
    baseline: "原模板基线",
    distance: "距离",
    total: "总距离",
    components: "分项",
    profile_version: "风格版本",
    reference_cohort: "参考样本集",
    reference_group: "参考样本",
    requested_reference_group: "请求的参考分层",
    sample_size: "原型子样本数",
    effective_sample_size: "有效参考样本数",
    reference_confidence: "参考可信度",
    shrinkage_weight: "小样本调整权重",
    fallback_reason: "改用其他计算方式的原因",
    calibration: "标定状态",
    level: "机制压力",
    signals: "压力信号",
    signal: "信号",
    static_geometry: "静态几何",
    tower_members_excluded: "塔内结构排除",
    dynamic_reveal: "动态揭露",
    difficulty_paths: "难度路径",
    risk_calibration: "玩家风险标定",
    overall: "整体覆盖",
    config: "配置覆盖",
    materialization: "牌面生成情况",
    materialization_fidelity: "牌面模拟方式",
    config_coverage: "配置匹配情况",
    path: "路径覆盖",
    statistical: "统计标定",
    stages: "揭露阶段",
    stage: "阶段",
    exposed: "已暴露 C–N",
    exposed_count: "已暴露 C–N",
    completed_groups: "已凑齐类型",
    singletons: "只露 1 张类型",
    pairs: "只露 2 张类型",
    released_ab: "释放 A/B",
    earliest: "最早",
    median: "中位",
    latest: "最晚",
    intrinsic_gate: "内生门槛型",
    board_reveal: "整板揭露型",
    hybrid: "混合型",
    all_types_long_range: "全类型长程型",
    serial_mechanism: "串行机关型",
    requested_match_score: "目标原型匹配度",
    requested_matches_automatic: "与独立判定一致",
    primary_distance: "保守难度距离",
    stable_tiebreak: "平均分项距离",
    solver_status: "采样路径证据状态",
    overall_status: "证据总体状态",
    trial_count: "模拟路径数",
    strategies: "策略类型",
    per_duplicate_path_spread: "各副本策略差异",
    sample_failure_rate: "最差副本 / 策略失败率",
    worst_strategy_failure_rate: "最差副本 / 策略失败率",
    path_spread: "同一副本的策略差异",
    claim_scope: "结果适用范围",
    solver_evidence_kind: "采样路径证据类型",
  };
  function experienceAnalysis(generated) {
    const candidate = generated?.candidate || {};
    const raw = candidate.experience_analysis || generated?.experience_analysis || candidate.experience || generated?.experience || {};
    const archetype = raw.archetype && typeof raw.archetype === "object" ? raw.archetype : {};
    const difficultySource = raw.difficulty_distance ?? raw.difficulty_simulation?.difficulty_distance ?? candidate.difficulty_distance;
    const difficultyFallback = candidateDifficultyReport(generated).distance;
    const difficultyDistance = difficultyNumber(
      difficultySource?.primary_distance,
      difficultySource?.total,
      difficultySource?.distance,
      typeof difficultySource === "number" ? difficultySource : null,
      difficultyFallback,
    );
    const onlineStyle = raw.online_style_distance && typeof raw.online_style_distance === "object" ? raw.online_style_distance : {};
    const onlineStyleStatus = String(onlineStyle.status || "").toLowerCase();
    const onlineStyleEnabled = onlineStyleStatus !== "off";
    const onlineStyleDistance = onlineStyleEnabled
      ? difficultyNumber(onlineStyle.total, onlineStyle.distance, raw.online_style_distance_total)
      : null;
    const pressure = raw.simulation_pressure && typeof raw.simulation_pressure === "object" ? raw.simulation_pressure : {};
    const pressureLevelRaw = String(firstDifficultyValue(pressure.level, pressure.pressure_level, "") || "").toLowerCase();
    const pressureLevel = ["low", "medium", "high"].includes(pressureLevelRaw) ? pressureLevelRaw : "unassessed";
    const hardValidation = raw.hard_validation && typeof raw.hard_validation === "object" ? raw.hard_validation : {};
    const hardStatusRaw = String(hardValidation.status || "").toLowerCase();
    const hardStatus = ["passed", "pass", "proved", "valid"].includes(hardStatusRaw)
      ? "proved"
      : ["failed", "fail", "disproved", "invalid", "rejected"].includes(hardStatusRaw)
        ? "disproved"
        : !hardStatusRaw && typeof hardValidation.passed === "boolean"
          ? hardValidation.passed ? "proved" : "disproved"
          : "inconclusive";
    const hardPassed = hardStatus === "proved" ? true : hardStatus === "disproved" ? false : null;
    const observedRisk = raw.observed_risk && typeof raw.observed_risk === "object" ? raw.observed_risk : {};
    const specialPresetRaw = raw.special_preset ?? candidate.special_preset ?? "none";
    const specialPreset = specialPresetRaw && typeof specialPresetRaw === "object"
      ? String(specialPresetRaw.requested || specialPresetRaw.preset || "none")
      : String(specialPresetRaw || "none");
    const classificationStatus = String(archetype.classification_status || (archetype.primary ? "classified" : "uncertain")).toLowerCase();
    const archetypeKey = classificationStatus === "classified"
      ? String(archetype.primary || "")
      : "uncertain";
    return {
      raw,
      available: Boolean(raw && typeof raw === "object" && Object.keys(raw).length),
      signature: String(candidate.experience_signature || generated?.experience_signature || ""),
      archetype,
      archetypeKey,
      classificationStatus,
      hardValidation,
      hardStatus,
      hardPassed,
      difficultySource,
      difficultyDistance,
      onlineStyle,
      onlineStyleStatus,
      onlineStyleEnabled,
      onlineStyleDistance,
      pressure,
      pressureLevel,
      observedRisk,
      evidenceCoverage: raw.evidence_coverage && typeof raw.evidence_coverage === "object" ? raw.evidence_coverage : {},
      revealCurve: raw.reveal_curve && typeof raw.reveal_curve === "object" ? raw.reveal_curve : {},
      specialPreset,
    };
  }
  function candidateExportEligibility(generated) {
    const experience = experienceAnalysis(generated);
    const summary = candidateSummary(generated);
    const difficulty = candidateDifficultyReport(generated);
    const evidenceOverall = String(experience.evidenceCoverage?.overall_status ?? experience.evidenceCoverage?.overall ?? "").toLowerCase();
    const materializationFidelity = String(experience.evidenceCoverage?.materialization_fidelity ?? experience.evidenceCoverage?.materialization ?? "").toLowerCase();
    const difficultyEvidenceInconclusive = evidenceOverall === "inconclusive" || materializationFidelity === "synthetic";
    if (summary.passed === false || experience.hardStatus === "disproved") {
      return {
        eligible: true,
        autoEligible: false,
        manualOverrideRequired: true,
        status: "disproved",
        label: "规则结论未通过 · 可人工导出",
        shortLabel: "规则未通过",
        className: "warning",
        reason: "该候选的结构或整盘规则结论未通过，不参与默认推荐；查看失败原因并人工确认后仍可导出。",
      };
    }
    if (difficulty.qualified === false || difficulty.trendOk === false) {
      return {
        eligible: true,
        autoEligible: false,
        manualOverrideRequired: true,
        status: "difficulty_not_met",
        label: "未达难度目标 · 可人工导出",
        shortLabel: "难度未达标",
        className: "warning",
        reason: `未达到「${difficultyTargetCopy(difficulty.target)}」或副本由前向后变易要求；不参与默认推荐，查看结论并人工确认后仍可导出。`,
      };
    }
    if (experience.hardStatus === "proved") {
      if (difficulty.qualified !== true || difficulty.trendOk !== true) {
        const evidenceMissing = difficulty.qualified === null || difficulty.qualified === undefined || difficulty.trendOk === null || difficulty.trendOk === undefined;
        return {
          eligible: true,
          autoEligible: false,
          manualOverrideRequired: true,
          status: evidenceMissing ? "inconclusive" : "difficulty_not_met",
          label: evidenceMissing ? "非推荐 · 需人工确认" : "未达难度目标 · 可人工导出",
          shortLabel: evidenceMissing ? "需人工确认" : "难度未达标",
          className: "warning",
          reason: evidenceMissing
            ? "尚未补齐 DupPara1–10 难度目标与梯度安全证据；不参与默认推荐，人工确认风险后仍可导出。"
            : `未达到「${difficultyTargetCopy(difficulty.target)}」或副本由前向后变易要求；不参与默认推荐，人工确认后仍可导出。`,
        };
      }
      if (difficultyEvidenceInconclusive) {
        return {
          eligible: true,
          autoEligible: false,
          manualOverrideRequired: true,
          status: "inconclusive",
          label: "非推荐 · 需人工确认",
          shortLabel: "需人工确认",
          className: "warning",
          reason: "当前难度结论来自简化模拟牌面或未完成客户端牌面生成规则核验；不参与默认推荐，人工确认风险后仍可导出。",
        };
      }
      return {
        eligible: true,
        autoEligible: true,
        manualOverrideRequired: false,
        status: "proved",
        label: "硬校验已证明",
        shortLabel: "已证明",
        className: "success",
        reason: "整盘可完成路径与结构硬规则均已独立复验。",
      };
    }
    return {
      eligible: true,
      autoEligible: false,
      manualOverrideRequired: true,
      status: "inconclusive",
      label: "非推荐 · 需人工确认",
      shortLabel: "需人工确认",
      className: "warning",
      reason: "尚未补齐可复验的整盘可完成路径；不参与默认推荐，人工确认风险后仍可导出。",
    };
  }
  function experienceArchetypeCopy(value) {
    const key = String(value || "");
    return EXPERIENCE_ARCHETYPE_COPY[key] || (key ? key.replaceAll("_", " ") : "未评估");
  }
  function experienceStatusCopy(value) {
    const raw = String(value ?? "").toLowerCase();
    return ({
      passed: "通过", pass: "通过", proved: "已证明", valid: "通过",
      failed: "未通过", fail: "未通过", rejected: "未通过", invalid: "未通过",
      complete: "完整", available: "可用", directional: "仅供参考", partial: "部分覆盖",
      inconclusive: "未评估", unavailable: "未标定", missing: "未评估",
      synthetic: "简化模拟牌面", reconstructed: "按配置重建牌面", exact_unique: "唯一精确配置",
      matched: "匹配", not_applicable: "不适用", off: "已关闭",
      classified: "已分类", uncertain: "未形成可靠分类",
      exact_size_archetype_reference: "参考同规模、同原型样本",
      shrunk_size_archetype_reference: "样本较少，已结合总体样本",
      size_reference_fallback: "改用同规模样本作参考",
      archetype_classification_uncertain: "原型暂不确定，参考同规模样本",
      archetype_stratum_unavailable: "该原型分层暂无足够样本",
      archetype_stratum_below_minimum_sample: "该原型样本不足，改用同规模样本",
      acceptable: "证据可用", limit_exceeded: "采样内未见反例，不等于所有路径安全",
      disproved: "已观测到失败路径（不等于结构校验失败）", reject: "不通过",
      sampled_path_counterexample: "已发现失败路径",
      sampled_paths_not_exhaustive_proof: "样本路径证据，不声明穷举所有玩法",
      many_structures_initially_clickable: "开局可点结构过多",
      few_entries_after_clearing_ab: "清除 A/B 后结构入口过少",
      dynamic_dangling_exposure_observed: "观测到悬置 1 / 2 张路径",
      sampled_strategy_failures_observed: "观测到策略失败路径",
      strategy_path_dispersion_observed: "同一副本的玩法结果差异较大",
      low: "低", medium: "中", high: "高",
    })[raw] || String(value || "未评估");
  }
  function experienceFieldLabel(key) {
    return EXPERIENCE_FIELD_COPY[key] || String(key || "分项").replaceAll("_", " ");
  }
  function experienceValueCopy(value, key = "") {
    if (value === undefined || value === null || value === "") return "未评估";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "number") {
      if (/ratio|rate|coverage|confidence|share/i.test(key) && Math.abs(value) <= 1) return difficultyPercent(value);
      return Number.isInteger(value) ? String(value) : value.toFixed(3);
    }
    if (Array.isArray(value)) return value.length ? value.map((entry) => experienceValueCopy(entry, key)).join("；") : "无";
    if (typeof value === "object") return "见下方分项";
    const raw = String(value);
    if (EXPERIENCE_ARCHETYPE_COPY[raw]) return experienceArchetypeCopy(raw);
    return experienceStatusCopy(raw);
  }
  function experienceFlatRows(value, prefix = "", depth = 0) {
    if (value === undefined || value === null || value === "") return [];
    if (typeof value !== "object") return [[prefix || "结果", experienceValueCopy(value, prefix)]];
    if (Array.isArray(value)) {
      if (!value.length) return [[prefix || "结果", "无"]];
      if (value.every((entry) => typeof entry !== "object" || entry === null)) return [[prefix || "结果", experienceValueCopy(value, prefix)]];
      return value.slice(0, 12).flatMap((entry, index) => experienceFlatRows(entry, `${prefix || "项目"} ${index + 1}`, depth + 1));
    }
    const entries = Object.entries(value);
    if (!entries.length) return [];
    return entries.slice(0, 36).flatMap(([key, entry]) => {
      const label = prefix ? `${prefix} · ${experienceFieldLabel(key)}` : experienceFieldLabel(key);
      if (entry && typeof entry === "object" && depth < 2) return experienceFlatRows(entry, label, depth + 1);
      return [[label, experienceValueCopy(entry, key)]];
    });
  }
  function experienceEvidenceSection(title, value, options = {}) {
    const rows = experienceFlatRows(value).filter(([label]) => !options.omit?.includes(label));
    const rawRows = experienceFlatRows(options.rawValue ?? value).filter(([label]) => !options.omit?.includes(label));
    const visibleLimit = Math.max(1, Number(options.limit || 6));
    const visibleRows = rows.filter(([, item]) => item !== "未评估").slice(0, visibleLimit);
    const copy = visibleRows.length
      ? `<dl class="experience-evidence-list">${visibleRows.map(([label, item]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(item)}</dd></div>`).join("")}</dl>`
      : `<p class="experience-evidence-empty">${escapeHtml(options.empty || "未评估")}</p>`;
    const rawCopy = options.rawValue !== undefined && rawRows.length
      ? `<details class="experience-technical-details"><summary>查看全部技术明细（${rawRows.length} 项）</summary><dl class="experience-evidence-list">${rawRows.map(([label, item]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(item)}</dd></div>`).join("")}</dl></details>`
      : "";
    return `<section class="experience-evidence-section"><header><h4>${escapeHtml(title)}</h4>${options.badge ? `<span class="experience-evidence-badge">${escapeHtml(options.badge)}</span>` : ""}</header>${options.lead ? `<p class="experience-evidence-lead">${escapeHtml(options.lead)}</p>` : ""}${copy}${rawCopy}</section>`;
  }
  const REVEAL_STAGE_LABELS = ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"];
  function experienceDistributionValue(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return difficultyNumber(value.median, value.q50, value.mean, value.value);
  }
  function experienceSampleSummary(values) {
    const usable = values
      .map((value) => ({
        median: experienceDistributionValue(value),
        minimum: value && typeof value === "object" && !Array.isArray(value)
          ? difficultyNumber(value.minimum, value.min, experienceDistributionValue(value))
          : experienceDistributionValue(value),
        maximum: value && typeof value === "object" && !Array.isArray(value)
          ? difficultyNumber(value.maximum, value.max, experienceDistributionValue(value))
          : experienceDistributionValue(value),
      }))
      .filter((value) => Number.isFinite(value.median));
    const samples = usable
      .map((value) => value.median)
      .sort((left, right) => left - right);
    if (!samples.length) return null;
    const middle = Math.floor(samples.length / 2);
    const median = samples.length % 2 ? samples[middle] : (samples[middle - 1] + samples[middle]) / 2;
    return {
      median,
      minimum: Math.min(...usable.map((value) => Number.isFinite(value.minimum) ? value.minimum : value.median)),
      maximum: Math.max(...usable.map((value) => Number.isFinite(value.maximum) ? value.maximum : value.median)),
      count: samples.length,
    };
  }
  function experienceCompactNumber(value) {
    if (!Number.isFinite(value)) return "—";
    if (Math.abs(value - Math.round(value)) < 0.005) return String(Math.round(value));
    return value.toFixed(1);
  }
  function revealMetricMarkup(summary, options = {}) {
    if (!summary) return `<span class="reveal-metric-value is-empty">—</span>`;
    const format = options.percent
      ? (value) => `${Math.round(Math.abs(value) <= 1 ? value * 100 : value)}%`
      : experienceCompactNumber;
    const median = format(summary.median);
    const range = summary.maximum - summary.minimum > 0.005
      ? `<small>${escapeHtml(`${format(summary.minimum)}–${format(summary.maximum)}`)}</small>`
      : "";
    return `<span class="reveal-metric-value">${escapeHtml(median)}</span>${range}`;
  }
  function revealStrategyCurves(revealCurve) {
    const result = [];
    const strategyDuplicates = Array.isArray(revealCurve?.per_duplicate_strategy_reveal)
      ? revealCurve.per_duplicate_strategy_reveal
      : [];
    strategyDuplicates.forEach((duplicate) => {
      const strategies = duplicate?.strategies && typeof duplicate.strategies === "object" ? duplicate.strategies : {};
      Object.values(strategies).forEach((strategy) => {
        if (strategy?.reveal_curve && typeof strategy.reveal_curve === "object") result.push(strategy.reveal_curve);
      });
    });
    if (result.length) return result;
    const duplicateCurves = Array.isArray(revealCurve?.per_duplicate) ? revealCurve.per_duplicate : [];
    duplicateCurves.forEach((curve) => {
      if (curve && typeof curve === "object") result.push(curve.reveal_curve && typeof curve.reveal_curve === "object" ? curve.reveal_curve : curve);
    });
    if (result.length) return result;
    if (Array.isArray(revealCurve?.curve) || Array.isArray(revealCurve?.stages)) return [revealCurve];
    return [];
  }
  function revealStageEvidence(revealCurve) {
    const metricAliases = {
      exposed: ["cumulative_exposed_structure_count", "exposed_count", "exposed"],
      completed: ["complete_structure_group_type_count", "completed_groups"],
      danglingOne: ["dangling_one_type_count", "singletons"],
      danglingTwo: ["dangling_two_type_count", "pairs"],
    };
    const stages = REVEAL_STAGE_LABELS.map(() => ({ exposed: [], completed: [], danglingOne: [], danglingTwo: [] }));
    const curves = revealStrategyCurves(revealCurve);
    curves.forEach((curve) => {
      const rows = Array.isArray(curve.curve) ? curve.curve : Array.isArray(curve.stages) ? curve.stages : [];
      rows.slice(0, REVEAL_STAGE_LABELS.length).forEach((row, rowIndex) => {
        if (!row || typeof row !== "object") return;
        const progressFrom = difficultyNumber(row.progress_from);
        const stageIndex = progressFrom === null
          ? rowIndex
          : Math.max(0, Math.min(REVEAL_STAGE_LABELS.length - 1, Math.floor(progressFrom * REVEAL_STAGE_LABELS.length + 0.0001)));
        Object.entries(metricAliases).forEach(([metric, aliases]) => {
          const value = aliases.map((key) => row[key]).find((entry) => experienceDistributionValue(entry) !== null);
          if (value !== undefined) stages[stageIndex][metric].push(value);
        });
      });
    });
    return {
      curves,
      stages: stages.map((stage, index) => ({
        label: REVEAL_STAGE_LABELS[index],
        exposed: experienceSampleSummary(stage.exposed),
        completed: experienceSampleSummary(stage.completed),
        danglingOne: experienceSampleSummary(stage.danglingOne),
        danglingTwo: experienceSampleSummary(stage.danglingTwo),
      })),
      hasStageEvidence: stages.some((stage) => Object.values(stage).some((values) => values.length)),
    };
  }
  function revealReleaseEvidence(curves) {
    const samples = (key) => experienceSampleSummary(curves.map((curve) => curve?.[key]));
    const firstMatch = experienceSampleSummary(curves.map((curve) => curve?.first_three_structure_matches?.["1"]?.progress));
    return {
      perClick: samples("released_ab_per_structure_click"),
      completionClick: samples("released_ab_on_match_completion_click"),
      total: samples("released_ab_total_per_path"),
      firstMatch,
    };
  }
  function revealReferenceEvidence(report) {
    const baseline = report.difficultySource?.whole_board_reveal_baseline;
    const reference = report.difficultySource?.reveal_reference_distance
      ?? baseline?.dynamic_reference;
    if (reference && typeof reference === "object") {
      return {
        status: String(reference.status || "inconclusive").toLowerCase(),
        selectionStatus: String(reference.reference_selection_status || "").toLowerCase(),
        group: typeof reference.effective_group === "string" ? reference.effective_group : "",
        sampleSize: difficultyNumber(reference.reference_sample_size),
        memberCount: difficultyNumber(reference.reference_member_count),
        distance: difficultyNumber(reference.primary_distance),
        coverage: difficultyNumber(reference.coverage),
      };
    }
    if (baseline?.mode === "source_template" || baseline?.source_has_cn === true) {
      return { status: "source_template", selectionStatus: "", group: "", sampleSize: null, memberCount: null, distance: null, coverage: null };
    }
    return { status: "inconclusive", selectionStatus: "", group: "", sampleSize: null, memberCount: null, distance: null, coverage: null };
  }
  function revealReferenceStatusCopy(reference) {
    if (reference.status === "compared") return "已比对";
    if (reference.status === "source_template") return "与源模板直接比对";
    return "参考样本不足";
  }
  function revealReferenceMarkup(reference) {
    const status = revealReferenceStatusCopy(reference);
    const stateClass = reference.status === "compared" ? "is-compared" : reference.status === "source_template" ? "is-source" : "is-inconclusive";
    const label = reference.status === "source_template" ? "动态基线" : "线上动态参考";
    const groupCopy = reference.group
      ? `${reference.group}${reference.sampleSize !== null ? ` · ${Math.round(reference.sampleSize)} 个同组模板` : ""}`
      : reference.status === "source_template" ? "当前源模板" : "未找到同规模参考样本";
    const distanceCopy = reference.distance === null ? "机制距离暂不展示" : `机制距离 ${reference.distance.toFixed(3)}`;
    const coverageCopy = reference.coverage === null ? "" : ` · 覆盖 ${difficultyPercent(reference.coverage)}`;
    return `<div class="reveal-reference-strip ${stateClass}"><div><span>${escapeHtml(label)}</span><strong>${escapeHtml(status)}</strong><small>${escapeHtml(groupCopy)}</small></div><p>${escapeHtml(`${distanceCopy}${coverageCopy}`)}</p></div>`;
  }
  function experienceRevealEvidenceMarkup(report) {
    const stageEvidence = revealStageEvidence(report.revealCurve);
    const releaseEvidence = revealReleaseEvidence(stageEvidence.curves);
    const reference = revealReferenceEvidence(report);
    const stageRows = stageEvidence.stages.map((stage) => `<tr class="reveal-stage-row"><th scope="row">${escapeHtml(stage.label)}</th><td>${revealMetricMarkup(stage.exposed)}</td><td>${revealMetricMarkup(stage.completed)}</td><td><span class="reveal-dangling-pair"><span class="reveal-dangling-unit">${revealMetricMarkup(stage.danglingOne)}</span><i>/</i><span class="reveal-dangling-unit">${revealMetricMarkup(stage.danglingTwo)}</span></span></td></tr>`).join("");
    const stageTable = stageEvidence.hasStageEvidence
      ? `<div class="reveal-stage-table-wrap"><table class="reveal-stage-table"><thead><tr><th>整盘进度</th><th>累计暴露 C–N</th><th>凑齐类型</th><th>悬置 1 / 2 张</th></tr></thead><tbody>${stageRows}</tbody></table></div>`
      : `<p class="experience-evidence-empty">未返回可汇总的 5 阶段路径。</p>`;
    const releaseRows = [
      ["每次结构点击释放 A/B", releaseEvidence.perClick, {}],
      ["三消点击释放 A/B", releaseEvidence.completionClick, {}],
      ["单路径累计释放 A/B", releaseEvidence.total, {}],
      ["首组三消位置", releaseEvidence.firstMatch, { percent: true }],
    ];
    const releaseMarkup = releaseRows.some(([, summary]) => summary)
      ? `<dl class="reveal-release-summary">${releaseRows.map(([label, summary, options]) => `<div><dt>${escapeHtml(label)}</dt><dd>${revealMetricMarkup(summary, options)}</dd></div>`).join("")}</dl>`
      : "";
    const rawRows = experienceFlatRows(report.revealCurve);
    const rawCopy = rawRows.length
      ? `<details class="experience-technical-details"><summary>查看全部技术明细（${rawRows.length} 项）</summary><dl class="experience-evidence-list">${rawRows.map(([label, item]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(item)}</dd></div>`).join("")}</dl></details>`
      : "";
    return `<section class="experience-evidence-section experience-reveal-section"><header><h4>整板揭露曲线</h4><span class="experience-evidence-badge">5 阶段</span></header><p class="experience-evidence-lead">按副本与策略等权汇总；中位值下方显示观测范围，动态参考只描述相似度，不换算胜率。</p>${revealReferenceMarkup(reference)}${stageTable}${releaseMarkup}${rawCopy}</section>`;
  }
  function experienceOnlineStyleCopy(report) {
    if (!report.onlineStyleEnabled) return "未启用";
    return report.onlineStyleDistance === null ? "未评估" : report.onlineStyleDistance.toFixed(3);
  }
  function hasReliableArchetype(report) {
    return report.classificationStatus !== "uncertain"
      && Boolean(report.archetypeKey)
      && !["auto", "uncertain"].includes(report.archetypeKey);
  }
  function mechanismPressureMetricMarkup(generated) {
    const report = experienceAnalysis(generated);
    if (report.pressureLevel === "unassessed") return "";
    const pressureCopy = experienceStatusCopy(report.pressureLevel);
    const pressureSuffix = report.pressureLevel === "high" ? " · 不优先推荐" : "";
    return `<div class="is-${escapeHtml(report.pressureLevel)}"><span>机制压力</span><strong>${escapeHtml(`${pressureCopy}${pressureSuffix}`)}</strong></div>`;
  }
  function experienceArchetypeMetricMarkup(generated) {
    const report = experienceAnalysis(generated);
    return hasReliableArchetype(report)
      ? `<div><span>体验原型</span><strong>${escapeHtml(experienceArchetypeCopy(report.archetypeKey))}</strong></div>`
      : "";
  }
  function focusExperienceSummaryMarkup(generated) {
    const metrics = experienceArchetypeMetricMarkup(generated) + mechanismPressureMetricMarkup(generated);
    return metrics ? `<div class="candidate-experience-summary focus-experience-signals" aria-label="候选体验原型与机制压力">${metrics}</div>` : "";
  }
  function experienceSummaryMarkup(generated, options = {}) {
    const report = experienceAnalysis(generated);
    const distanceReport = {
      distance: report.difficultyDistance,
      evidenceCoverage: report.evidenceCoverage,
    };
    const metrics = [];
    if (options.includeArchetype !== false) {
      const archetypeMarkup = experienceArchetypeMetricMarkup(generated);
      if (archetypeMarkup) metrics.push(archetypeMarkup);
    }
    if (hasReliableDifficultyDistance(distanceReport)) {
      metrics.push(`<div><span>与原模板难度距离</span><strong>${escapeHtml(difficultyDistanceCopy(distanceReport))}</strong></div>`);
    }
    if (options.includePressure !== false) {
      const pressureMarkup = mechanismPressureMetricMarkup(generated);
      if (pressureMarkup) metrics.push(pressureMarkup);
    }
    if (!metrics.length) {
      if (options.showEmptyGuidance === false) return "";
      return `<div class="candidate-experience-guidance" role="note">暂无可用于排序的体验指标，请结合棋盘并在详情中确认依据。</div>`;
    }
    return `<div class="candidate-experience-summary" aria-label="候选整板体验摘要">${metrics.join("")}</div>`;
  }
  function experienceEvidenceMarkup(generated) {
    const report = experienceAnalysis(generated);
    const hardBadge = report.hardStatus === "proved" ? "已证明" : report.hardStatus === "disproved" ? "未通过" : "未形成完整证明";
    const archetypeReliable = hasReliableArchetype(report);
    const archetypeBadge = archetypeReliable ? experienceArchetypeCopy(report.archetypeKey) : "未参与推荐";
    const pressureBadge = report.pressureLevel === "unassessed" ? "未评估" : experienceStatusCopy(report.pressureLevel);
    const styleBadge = experienceOnlineStyleCopy(report);
    const observedStatus = String(report.observedRisk.status || "").toLowerCase();
    const observedLead = observedStatus === "unavailable" || !Object.keys(report.observedRisk).length
      ? "未用真实玩家数据标定；机制压力不等于玩家风险。"
      : "仅展示已返回的观测依据。";
    const special = report.specialPreset;
    const context = [report.signature ? `结构标识 ${report.signature}` : "", special !== "none" ? `特殊预设 ${experienceFieldLabel(special)}` : ""].filter(Boolean).join(" · ");
    const difficultySummary = report.difficultySource && typeof report.difficultySource === "object" ? {
      version: report.difficultySource.version,
      primary_distance: report.difficultySource.primary_distance ?? report.difficultySource.total,
      stable_tiebreak: report.difficultySource.stable_tiebreak,
      whole_board_reveal_baseline: report.difficultySource.whole_board_reveal_baseline,
    } : report.difficultyDistance;
    const archetypeSummary = {
      requested: report.archetype.requested,
      primary: report.archetype.primary,
      classification_status: report.archetype.classification_status,
      top2: report.archetype.top2,
      requested_match_score: report.archetype.requested_match_score ?? report.archetype.match_score,
      requested_matches_automatic: report.archetype.requested_matches_automatic,
      classification_confidence: report.archetype.classification_confidence,
      minimum_confidence: report.archetype.minimum_confidence,
      baseline: report.archetype.baseline,
    };
    const styleSummary = report.onlineStyleEnabled ? {
      status: report.onlineStyle.status,
      reference_cohort: report.onlineStyle.reference_cohort,
      reference_group: report.onlineStyle.reference_group,
      requested_reference_group: report.onlineStyle.requested_reference_group,
      sample_size: report.onlineStyle.sample_size,
      effective_sample_size: report.onlineStyle.effective_sample_size,
      reference_confidence: report.onlineStyle.reference_confidence,
      shrinkage_weight: report.onlineStyle.shrinkage_weight,
      fallback_reason: report.onlineStyle.fallback_reason,
      distance: report.onlineStyle.distance ?? report.onlineStyle.total,
      calibration: report.onlineStyle.calibration,
    } : { status: "off" };
    const pressureSummary = {
      level: report.pressure.level,
      sample_failure_rate: report.pressure.sample_failure_rate,
      path_spread: report.pressure.path_spread,
      signals: report.pressure.signals,
      calibration: report.pressure.calibration,
    };
    const coveragePath = report.evidenceCoverage.path_coverage || report.evidenceCoverage.path || {};
    const coverageSummary = {
      overall_status: report.evidenceCoverage.overall_status ?? report.evidenceCoverage.overall,
      config_coverage: report.evidenceCoverage.config_coverage ?? report.evidenceCoverage.config,
      materialization_fidelity: report.evidenceCoverage.materialization_fidelity ?? report.evidenceCoverage.materialization,
      solver_status: report.evidenceCoverage.solver_status,
      path_coverage: {
        mode: coveragePath.mode,
        trial_count: coveragePath.trial_count,
        strategies: coveragePath.strategies,
      },
    };
    const distanceReport = { distance: report.difficultyDistance, evidenceCoverage: report.evidenceCoverage };
    const difficultyReliable = hasReliableDifficultyDistance(distanceReport);
    const archetypeSection = archetypeReliable
      ? experienceEvidenceSection("体验原型", archetypeSummary, { rawValue: report.archetype, badge: archetypeBadge, lead: "仅将达到分类置信度的原型用于推荐。", empty: "未评估原型匹配。" })
      : "";
    const difficultySection = difficultyReliable
      ? experienceEvidenceSection("与原模板难度距离", difficultySummary, { rawValue: report.difficultySource ?? report.difficultyDistance, badge: difficultyDistanceCopy(distanceReport), lead: "数值越小表示与原模板的模型距离越近。", empty: "未评估难度距离。" })
      : "";
    return `<div class="experience-evidence-report">${!report.available ? `<p class="experience-directional-note">这条旧记录没有整板体验分析；请以棋盘与硬校验结果为准。</p>` : context ? `<p class="experience-context-copy">${escapeHtml(context)}</p>` : ""}${experienceEvidenceSection("结构规则与可完成路径", report.hardValidation, { badge: hardBadge, lead: "精确校验结构规则，并要求每个建模副本至少有一条可复验完成路径；不声明所有玩家路径安全，也不等同客户端实际生成的牌面。", empty: "旧记录未返回独立结构校验明细。" })}${archetypeSection}${difficultySection}${experienceEvidenceSection("线上风格距离", styleSummary, { rawValue: report.onlineStyle, badge: styleBadge, empty: "未评估线上风格距离。" })}${experienceEvidenceSection("机制压力", pressureSummary, { rawValue: report.pressure, badge: pressureBadge, lead: "基于固定策略样本与揭露路径，未标定时不解释为玩家风险。", empty: "未评估机制压力。" })}${experienceEvidenceSection("已观测玩家风险", report.observedRisk, { badge: observedStatus ? experienceStatusCopy(observedStatus) : "未标定", lead: observedLead, empty: "未标定。" })}${experienceEvidenceSection("证据覆盖", coverageSummary, { rawValue: report.evidenceCoverage, empty: "旧记录未返回证据覆盖。" })}${experienceRevealEvidenceMarkup(report)}</div>`;
  }
  function candidateSamplePassModel(generated) {
    const rows = difficultyStrategyRows(candidateDifficultyReport(generated));
    const rates = rows.map(({ pass }) => typeof pass === "number" && Number.isFinite(pass) && pass >= 0 && pass <= 100
      ? (pass > 1 ? pass / 100 : pass) : null);
    const complete = rates.length === 5 && rates.every(value => value !== null);
    return { rows, rates, score: complete ? Math.round(rates.reduce((sum, value) => sum + value, 0) * 1e12) / 1e12 : null };
  }
  function samplePassSummaryMarkup(generated) {
    const { rows } = candidateSamplePassModel(generated);
    return `<section class="sample-pass-summary" aria-label="策略样本通过率"><dl class="sample-pass-grid">${rows.map(({ label, pass }) => `<div class="sample-pass-metric" title="${escapeHtml(label)}样本通过率"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(difficultyPercent(pass))}</dd></div>`).join("")}</dl></section>`;
  }
  function candidateRecommendation(item, generated) {
    // This is a sample preference, never a replacement for export validation.
    const pool = (item?.candidates || []).map(candidate => ({ candidate, model: candidateSamplePassModel(candidate) }))
      .filter(({ candidate, model }) => model.score > 0 && candidateSummary(candidate).passed !== false && experienceAnalysis(candidate).hardStatus !== "disproved");
    const winner = pool.reduce((best, value) => !best || value.model.score > best.model.score ? value : best, null);
    const recommended = Boolean(winner && candidateId(winner.candidate) === candidateId(generated));
    return { recommended, rank: recommended ? 1 : 0, reasons: recommended ? ["五项策略样本通过率合计最高；并列取首项，导出规则仍单独校验。"] : [] };
  }
  function orderedCandidates(item) {
    return [...(item?.candidates || [])]
      .map((candidate, order) => ({ candidate, order, recommendation: candidateRecommendation(item, candidate) }))
      .sort((left, right) => {
        if (left.recommendation.recommended !== right.recommendation.recommended) return left.recommendation.recommended ? -1 : 1;
        if (left.recommendation.recommended && left.recommendation.rank !== right.recommendation.rank) {
          return left.recommendation.rank - right.recommendation.rank;
        }
        return displayCandidateIndex(left.candidate) - displayCandidateIndex(right.candidate) || left.order - right.order;
      })
      .map(({ candidate }) => candidate);
  }
  function candidateRiskWarnings(generated) {
    const candidate = generated?.candidate || {};
    const summary = candidateSummary(generated);
    const metrics = candidate.quality_metrics || {};
    const supplied = generated?.risk_warnings || candidate.risk_warnings || candidate.quality_summary?.risk_warnings || [];
    const warnings = (Array.isArray(supplied) ? supplied : [supplied]).map(String).filter(Boolean);
    if (warnings.length) return dedupeRiskCopy(warnings);
    const structureCount = Math.max(0, Number(summary.replacement_count || 0));
    const uniqueNested = Math.max(0, Number(summary.nested_a_count || 0) + Number(summary.nested_b_count || 0));
    const participation = Number(metrics.structure_participation_rate);
    if (structureCount && uniqueNested && Number.isFinite(participation) && participation >= .5) {
      warnings.push(`${structureCount} 个结构块形成 ${uniqueNested} 处 A/B 嵌套；约 ${Math.round(participation * structureCount)}/${structureCount} 个结构块参与压层。`);
    }
    const verticalGap = Number(metrics.vertical_mean_layer_gap);
    const crossLayerShare = Number(metrics.vertical_cross_layer_share);
    if (Number.isFinite(verticalGap) && Number.isFinite(crossLayerShare) && crossLayerShare > .2) {
      warnings.push(`跨层压叠较多，重叠结构块平均相隔 ${verticalGap.toFixed(1)} 层，解锁链可能更长。`);
    }
    return dedupeRiskCopy(warnings);
  }
  function dedupeRiskCopy(values) {
    const seen = [];
    return (values || []).map((value) => String(value || "").trim()).filter((value) => {
      if (!value) return false;
      const normalized = value.replace(/[\s，。；：、,.;:]+/gu, "").toLowerCase();
      if (!normalized || seen.some((known) => known === normalized || known.includes(normalized) || normalized.includes(known))) return false;
      seen.push(normalized); return true;
    });
  }
  function candidateRiskMetric(generated) {
    const candidate = generated?.candidate || {};
    const metrics = candidate.quality_metrics || generated?.quality_metrics || {};
    const value = Number(
      metrics.experience_risk_penalty
      ?? candidate.experience_risk_penalty
      ?? generated?.experience_risk_penalty,
    );
    return Number.isFinite(value) ? value : null;
  }
  function candidateRiskProfile(item, generated) {
    const directLevel = String(generated?.risk_level || generated?.candidate?.risk_level || "").toLowerCase();
    const suppliedPercentile = Number(generated?.risk_percentile ?? generated?.candidate?.risk_percentile);
    const values = (item?.candidates || [])
      .map((candidate) => ({ candidate, value: candidateRiskMetric(candidate) }))
      .filter((entry) => Number.isFinite(entry.value))
      .sort((left, right) => left.value - right.value || displayCandidateIndex(left.candidate) - displayCandidateIndex(right.candidate));
    const currentId = candidateId(generated);
    const rankIndex = values.findIndex((entry) => candidateId(entry.candidate) === currentId);
    const total = values.length;
    const distinctValues = [...new Set(values.map((entry) => entry.value))];
    const currentValue = rankIndex >= 0 ? values[rankIndex].value : null;
    const valueIndex = currentValue === null ? -1 : distinctValues.indexOf(currentValue);
    const relativePosition = valueIndex >= 0 && distinctValues.length > 1
      ? valueIndex / (distinctValues.length - 1)
      : null;
    const level = ["low", "medium", "high"].includes(directLevel)
      ? directLevel
      : relativePosition === null ? "unknown" : relativePosition <= .33 ? "low" : relativePosition <= .67 ? "medium" : "high";
    const levelCopy = { low: "低", medium: "中", high: "高", unknown: "待观察" }[level];
    const percentile = Number.isFinite(suppliedPercentile)
      ? clamp(Math.round(suppliedPercentile <= 1 ? suppliedPercentile * 100 : suppliedPercentile), 0, 100)
      : rankIndex >= 0 && total ? Math.round(((rankIndex + .5) / total) * 100) : null;
    const direction = ({
      low: "嵌套与跨层压叠较轻，结构风险偏低。",
      medium: "嵌套与跨层压叠处于同组中段，建议结合轨迹复核。",
      high: "嵌套或跨层压叠偏高，可能增加中后段操作压力。",
      unknown: "样本不足，仅展示结构关系，暂不判断难度。",
    })[level];
    return {
      level,
      levelCopy,
      direction,
    };
  }
  function qualityGroupCopy(generated) {
    const candidate = generated?.candidate || {};
    const metrics = candidate.quality_metrics || {};
    const supplied = candidate.quality_summary || generated?.quality_summary || {};
    const span = Number(metrics.structure_layer_span ?? candidateSummary(generated).structure_layer_span);
    const layoutMetrics = [
      ["中心距离", metrics.center_distance],
      ["对称度", metrics.symmetry_ratio],
      ["平均间距", metrics.average_pair_distance],
      ["局部密度", metrics.local_density],
    ].filter(([, value]) => Number.isFinite(Number(value)));
    const layout = typeof supplied === "object" && !Array.isArray(supplied) && (supplied.layout || supplied.layout_performance || supplied.layout_summary)
      ? String(supplied.layout || supplied.layout_performance || supplied.layout_summary)
      : layoutMetrics.length
        ? layoutMetrics.map(([label, value]) => `${label} ${Number(value).toFixed(3)}`).join(" · ")
        : "旧记录未返回布局分项，当前布局打平分仅作兼容展示。";
    const tupleRisk = Array.isArray(supplied) ? supplied.map(String).filter(Boolean)[1] : "";
    const warnings = dedupeRiskCopy(candidateRiskWarnings(generated));
    const riskIntro = typeof supplied === "object" && !Array.isArray(supplied) && (supplied.risk || supplied.experience_risk || supplied.risk_summary)
      ? String(supplied.risk || supplied.experience_risk || supplied.risk_summary)
      : tupleRisk || (warnings.length ? "以下为当前候选可复核的嵌套与立体压叠关系。" : "当前候选未返回可解释的嵌套或立体压叠关系。");
    const spanCopy = Number.isFinite(span) ? `结构块跨 ${Math.max(0, Math.round(span))} 层。` : "";
    const riskParts = dedupeRiskCopy([riskIntro, spanCopy]);
    return { layout, risk: riskParts.join(" "), warnings: warnings.filter((warning) => !riskParts.some((part) => part.includes(warning) || warning.includes(part))) };
  }
  function initializeDefaultSelections() {
    if (!state.jobId) return;
    state.selectedCandidates = selectionsForJob(state.jobId);
    let changed = false;
    const availableByWork = new Map(successfulWorkItems().map((item) => [
      workKey(item),
      new Set((item.candidates || [])
        .filter((candidate) => candidateExportEligibility(candidate).eligible)
        .map(candidateId)
        .filter(Boolean)),
    ]));
    [...state.selectedCandidates.entries()].forEach(([key, selectedIds]) => {
      const availableIds = availableByWork.get(key);
      if (!availableIds) {
        const snapshotIncomplete = ["queued", "running", "paused", "pause_requested", "cancel_requested"].includes(state.jobSnapshot?.status)
          && (state.jobResult?.items?.length || 0) < Number(state.jobSnapshot?.work_item_count || 0);
        if (snapshotIncomplete) return;
        state.selectedCandidates.delete(key);
        changed = true;
        return;
      }
      const originalIds = Array.isArray(selectedIds) ? selectedIds : [];
      const validIds = [...new Set(originalIds.filter((id) => availableIds.has(id)))];
      if (validIds.length === originalIds.length) return;
      // If proof regresses from proved to inconclusive/disproved, clear the
      // stale ID without silently substituting a different recommendation.
      state.selectedCandidates.set(key, validIds);
      changed = true;
    });
    successfulWorkItems().forEach((item) => {
      const key = workKey(item);
      // A present key, including an empty list, is an explicit user choice.
      // Only a newly successful work item receives the top recommendation.
      if (state.selectedCandidates.has(key)) return;
      if (["running", "pending"].includes(item.status)) return;
      const candidates = [...(item.candidates || [])];
      const recommendedFirst = candidates
        .map((candidate) => ({ candidate, recommendation: candidateRecommendation(item, candidate) }))
        .filter(({ recommendation }) => recommendation.recommended && recommendation.rank === 1)
        .sort((left, right) => qualityScore(right.candidate) - qualityScore(left.candidate))[0]?.candidate;
      const initial = recommendedFirst;
      if (initial) { state.selectedCandidates.set(key, [candidateId(initial)]); changed = true; }
    });
    if (changed) persistSelections(state.jobId);
  }

  function ensureReviewButton() {
    const existing = $("#review-selections");
    if (existing) return existing;
    const actions = $(".candidate-heading .heading-actions");
    if (!actions) return null;
    const button = document.createElement("button"); button.id = "review-selections"; button.type = "button"; button.className = "primary-button"; button.textContent = "进入验证导出";
    actions.append(button);
    return button;
  }
  function renderCandidateWorkspace() {
    const reviewButton = ensureReviewButton();
    const items = successfulWorkItems();
    if (items.length) initializeDefaultSelections();
    const tabs = $("#work-item-tabs"); tabs.replaceChildren();
    items.forEach((item, index) => {
      const button = document.createElement("button"); button.type = "button"; button.dataset.workIndex = String(index);
      button.id = `work-item-tab-${index}`;
      button.classList.toggle("is-selected", index === state.activeWorkIndex);
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "candidate-grid");
      button.setAttribute("aria-selected", String(index === state.activeWorkIndex));
      button.tabIndex = index === state.activeWorkIndex ? 0 : -1;
      const pickedCount = selectedIdsForWork(item).length;
      const picked = pickedCount ? ` · 已加入 ${pickedCount}` : " · 不导出";
      button.textContent = `${item.template_key} · ${item.plan_signature || item.candidate?.plan_signature || item.plan_id} · ${item.candidates.length} 个候选${picked}`; tabs.append(button);
    });
    const activeTab = $(`#work-item-tab-${state.activeWorkIndex}`);
    if (activeTab) $("#candidate-grid").setAttribute("aria-labelledby", activeTab.id);
    else $("#candidate-grid").removeAttribute("aria-labelledby");
    if (!items.length) {
      $("#candidate-grid").innerHTML = '<div class="empty-state">本批次没有成功生成可审核的候选，请返回配置后调整限制。</div>';
      if (reviewButton) reviewButton.disabled = true;
      return;
    }
    renderCandidateBatchWarning(); renderCandidateTaskOverview();
    const active = activeWorkItem();
    $("#active-work-label").textContent = `成品项 ${state.activeWorkIndex + 1}/${items.length} · ${active.template_key} · ${active.plan_signature || active.plan_id}`;
    const activeSelectionCount = selectedIdsForWork(active).length;
    const activeExportableCount = (active.candidates || []).filter((candidate) => candidateExportEligibility(candidate).eligible).length;
    $("#candidate-selection-copy").textContent = activeSelectionCount
      ? `本项已加入 ${activeSelectionCount} 个候选；可继续加入或移除，完成后再查看下一项。`
      : activeExportableCount
        ? "本项不会导出；已证明候选可直接加入，非推荐候选也可人工选择。"
        : "本项没有可用的候选记录，请返回配置后重新生成。";
    $("#clear-work-selection").disabled = activeSelectionCount === 0;
    $("#complete-work-selection").disabled = state.activeWorkIndex >= items.length - 1;
    $("#complete-work-selection").textContent = state.activeWorkIndex >= items.length - 1 ? "已是最后一项" : "完成本项，查看下一项";
    $("#work-prev").disabled = state.activeWorkIndex <= 0;
    $("#work-next").disabled = state.activeWorkIndex >= items.length - 1;
    $("#work-navigation-label").textContent = `当前成品项 ${state.activeWorkIndex + 1} / ${items.length}`;
    $("#candidate-scope-copy").textContent = state.isRecoveredJob
      ? "已从任务记录恢复已锁定候选；未完成项可在本页单独调整，已有候选与选择会保留。"
      : "候选共享同一方案，结构类型数、数量、位置与评分可以不同。";
    $("#regenerate").disabled = state.isRecoveredJob;
    $("#regenerate").title = state.isRecoveredJob ? "请在未完成项中使用“调整此模板参数”，避免改变其他已锁定结果" : "";
    renderCandidateCards();
  }
  function renderCandidateTaskOverview() {
    const job = state.jobSnapshot; const root = $("#candidate-task-overview");
    if (!root || !job || job.status === "completed") { root?.classList.add("is-hidden"); return; }
    root.classList.remove("is-hidden");
    const { total, finished } = jobProgressMetrics(job);
    const candidateCount = successfulWorkItems().reduce((sum, item) => sum + (item.candidates?.length || 0), 0);
    const endAt = job.completed_at ? new Date(job.completed_at).getTime() : Date.now();
    const startAt = new Date(job.started_at || state.jobStartedAt || job.created_at || Date.now()).getTime();
    const events = job.events || [];
    const lastEvent = [...events].sort((left, right) => Number(right.sequence ?? right.seq ?? 0) - Number(left.sequence ?? left.seq ?? 0))[0];
    const resultItems = availableResultForJob(job)?.items || [];
    const unresolvedCount = resultItems.filter((item) => !item.candidates?.length).length;
    const incompleteCount = resultItems.filter(itemNeedsMoreCandidates).length;
    const hasWarnings = unresolvedCount > 0 || incompleteCount > 0;
    const attentionCopy = [
      unresolvedCount ? `${unresolvedCount} 项未找到` : "",
      incompleteCount ? `${incompleteCount} 项候选待补齐` : "",
    ].filter(Boolean).join(" · ");
    $("#candidate-task-health").textContent = hasWarnings
      ? `任务${statusLabel(job.status)} · ${attentionCopy}`
      : job.status === "completed" ? "候选生成任务已完成" : `任务${statusLabel(job.status)} · 已有候选可继续使用`;
    $("#candidate-task-last").textContent = lastEvent ? `最近：${eventCopy(lastEvent)}` : `最近进展 ${formatClock(job.last_progress_at)}`;
    $("#candidate-task-progress").textContent = `${finished} / ${total} 项`;
    $("#candidate-task-candidates").textContent = `${candidateCount} 个候选`;
    $("#candidate-task-duration").textContent = `用时 ${formatDuration(Math.max(0, endAt - startAt))}`;
    root.classList.toggle("has-warning", hasWarnings);
    root.open = Boolean(state.candidateOverviewOpened);
    $("summary", root)?.setAttribute("aria-expanded", String(root.open));
    renderActivityTimeline($("#candidate-task-events"), events, { limit: 10 });
  }
  function renderCandidateBatchWarning() {
    const root = $("#candidate-batch-warning"); if (!root) return;
    const missing = (state.jobResult?.items || []).filter((item) => !item.candidates?.length);
    const incomplete = (state.jobResult?.items || []).filter(itemNeedsMoreCandidates);
    const attentionItems = [...incomplete, ...missing];
    root.classList.toggle("is-hidden", attentionItems.length === 0);
    if (!attentionItems.length) { root.classList.remove("has-suggestions"); root.replaceChildren(); return; }
    const heading = document.createElement("div"); heading.className = "candidate-warning-heading";
    const strong = document.createElement("strong");
    strong.textContent = missing.length && incomplete.length
      ? `${missing.length} 项未找到 · ${incomplete.length} 项候选未补齐`
      : incomplete.length
        ? `${incomplete.length} 个成品项的候选未补齐`
        : `${missing.length} 个成品项未进入候选`;
    const deterministicCount = missing.filter(isDeterministicFailure).length;
    const budgetCount = missing.filter(isBudgetFailure).length;
    const interruptedCount = missing.length - deterministicCount - budgetCount;
    const canContinueSearch = jobCanContinueSearch(state.jobSnapshot);
    const running = ["queued", "running", "paused", "pause_requested", "cancel_requested"].includes(state.jobSnapshot?.status);
    const guidance = document.createElement("span"); guidance.textContent = running ? "搜索尚未结束，已有候选可先查看；未完成项会继续更新。" : incomplete.length && missing.length
      ? `已找到的候选可直接使用${canContinueSearch ? "，也可继续搜索未完成项" : ""}；未找到的项请按下方原因处理。`
      : incomplete.length
        ? canContinueSearch
          ? "已找到的候选可直接使用；也可继续搜索未完成项，已有候选会保留。"
          : "已找到的候选可直接使用；当前已达搜索上限，如需更多候选请调整参数。"
      : deterministicCount && (budgetCount || interruptedCount)
      ? "已证冲突项请逐项调参；预算未找到不代表无解，可保留现有结果继续搜索未完成项。"
      : deterministicCount
        ? "当前组合已证明存在参数冲突，请单独调整对应模板参数。"
        : budgetCount
          ? canContinueSearch
            ? "当前预算未找到不等于无解；建议保留现有候选并继续搜索未完成项。"
            : "当前预算未找到不等于无解，但已达搜索上限；请调整该模板参数。"
          : "任务被中断或遇到暂时性错误，可继续搜索未完成项；已有候选会保留。";
    heading.append(strong, guidance);
    const list = document.createElement("div"); list.className = "candidate-warning-list";
    const buildRow = (item) => {
      const row = document.createElement("div");
      const progress = itemCandidateProgress(item);
      const isIncomplete = itemNeedsMoreCandidates(item);
      const adjustmentKey = candidateAdjustmentKey(item);
      const staged = state.stagedTemplateAdjustments.has(adjustmentKey);
      const running = state.runningAdjustmentKeys.has(adjustmentKey);
      row.classList.toggle("is-staged", staged);
      row.classList.toggle("is-running-adjustment", running);
      const copy = document.createElement("span");
      const itemScope = [item?.template_key, item?.plan_signature || item?.plan_id].filter(Boolean).join(" · ") || "未知成品项";
      copy.innerHTML = isIncomplete
        ? `<b>${escapeHtml(itemScope)}：已找到 ${escapeHtml(progress.found)}${progress.target !== null ? ` / ${escapeHtml(progress.target)}` : ""} 个候选</b><small>当前结果可用；继续搜索只补齐未完成数量。</small>`
        : `<b>${escapeHtml(itemScope)}：已找到 ${escapeHtml(progress.found)}${progress.target !== null ? ` / ${escapeHtml(progress.target)}` : ""} 个候选</b><small>${escapeHtml(failureStatusCopy(item))}：${escapeHtml(itemErrorCopy(item))}；调整此模板不会清空其他已找到的候选。</small>`;
      row.append(copy);
      const actions = document.createElement("div"); actions.className = "candidate-warning-actions";
      if (running) {
        const status = document.createElement("span"); status.className = "status-chip success";
        status.textContent = "正在使用单独参数搜索"; actions.append(status);
      } else {
        if (isIncomplete && canContinueSearch && !staged) {
          const keep = document.createElement("span"); keep.className = "status-chip success";
          keep.textContent = "续跑保留已有候选"; actions.append(keep);
        }
        const adjust = document.createElement("button"); adjust.type = "button";
        adjust.className = staged ? "secondary-button staged-adjustment-button" : "secondary-button";
        adjust.dataset.adjustFailedTemplate = item.template_key || "";
        adjust.dataset.failedPlanId = item.plan_id || "";
        adjust.textContent = staged ? "已单独修改参数，可继续运行搜索" : "调整此模板参数";
        adjust.title = "在当前候选页只调整此模板；已有候选、进度与人工选择都会保留";
        actions.append(adjust);
        if (staged) {
          const run = document.createElement("button"); run.type = "button"; run.className = "primary-button";
          run.dataset.runStagedTemplate = adjustmentKey;
          run.dataset.adjustmentRunControl = "";
          run.disabled = state.adjustmentRunPending;
          run.textContent = "单独运行此模板";
          run.title = "仅运行这个成品项，其他已暂存修改继续留在候选页";
          actions.append(run);
        }
      }
      row.append(actions);
      return row;
    };
    attentionItems.slice(0, 6).forEach((item) => list.append(buildRow(item)));
    if (attentionItems.length > 6) {
      const overflow = document.createElement("details"); overflow.className = "failure-overflow candidate-warning-overflow";
      const summary = document.createElement("summary"); summary.textContent = `展开其余 ${attentionItems.length - 6} 项`;
      const rows = document.createElement("div"); rows.className = "candidate-warning-overflow-list";
      attentionItems.slice(6).forEach((item) => rows.append(buildRow(item)));
      overflow.append(summary, rows); list.append(overflow);
    }
    root.replaceChildren(heading, list);
    const suggestionStack = document.createElement("div"); suggestionStack.className = "candidate-suggestion-stack";
    attentionItems.forEach((item) => rankedApplicableParameterSuggestions(item).slice(0, 2)
      .forEach((suggestion) => suggestionStack.append(createParameterSuggestionCard(item, suggestion))));
    if (jobRecommendationsPending(state.jobSnapshot)) {
      const pending = document.createElement("p"); pending.className = "parameter-suggestion-pending";
      pending.setAttribute("role", "status"); pending.textContent = "正在用小预算试算可行的参数修改，结果会自动更新…"; suggestionStack.append(pending);
    }
    root.classList.toggle("has-suggestions", suggestionStack.childElementCount > 0);
    if (suggestionStack.childElementCount) root.append(suggestionStack);
    const canRetryInterrupted = state.jobSnapshot?.actions?.can_retry && interruptedCount > 0;
    if (canContinueSearch || canRetryInterrupted || state.stagedTemplateAdjustments.size) {
      root.append(createSearchBudgetPanel(state.jobSnapshot, { compact: true }));
    }
  }
  function renderCandidateCards() {
    const item = activeWorkItem(); if (!item) return;
    const candidates = orderedCandidates(item);
    const pageSize = 3, pages = Math.max(1, Math.ceil(candidates.length / pageSize));
    state.candidatePage = clamp(state.candidatePage, 0, pages - 1);
    const visible = candidates.slice(state.candidatePage * pageSize, state.candidatePage * pageSize + pageSize);
    const root = $("#candidate-grid"); root.replaceChildren();
    visible.forEach((generated) => {
      const id = candidateId(generated); const summary = candidateSummary(generated); const candidate = generated.candidate || {};
      const selected = isCandidateSelected(item, id);
      const recommendation = candidateRecommendation(item, generated);
      const experience = experienceAnalysis(generated);
      const exportEligibility = candidateExportEligibility(generated);
      const structureCounts = summary.structure_counts || {};
      const card = document.createElement("article"); card.className = `candidate-card${selected ? " is-selected" : ""}${recommendation.recommended ? " is-recommended" : ""}`; card.dataset.candidateId = id;
      card.setAttribute("aria-label", `候选 ${displayCandidateIndex(generated)}${recommendation.recommended ? `，推荐第 ${recommendation.rank} 名` : ""}${selected ? "，已加入本次导出" : ""}`);
      const selectedMark = selected ? `<span class="selected-candidate-mark">${icon("check")}已选</span>` : "";
      const recommendationMark = recommendation.recommended ? `<span class="status-chip recommendation-chip">样本优选</span>` : "";
      const hardCopy = exportEligibility.label;
      const hardClass = exportEligibility.className;
      const provenance = generated.parameter_provenance || {};
      const parameterMarkup = provenance.non_uniform
        ? `<span class="candidate-parameter-info"><span>&lt;非统一参数&gt;</span><button class="icon-button" type="button" data-candidate-parameter-info="${escapeHtml(id)}" aria-label="查看此候选的参数详情" title="查看参数详情">${icon("info")}</button></span>`
        : "";
      const chooseMarkup = selected
        ? `<button class="selected-candidate-button" type="button" aria-pressed="true" data-select-candidate="${escapeHtml(id)}">${icon("check")}已加入，点击移除</button>`
        : exportEligibility.manualOverrideRequired
          ? `<button class="secondary-button candidate-pick-button candidate-export-manual" type="button" aria-pressed="false" data-select-candidate="${escapeHtml(id)}" title="${escapeHtml(exportEligibility.reason)}">选择并导出</button>`
          : `<button class="primary-button candidate-pick-button" type="button" aria-pressed="false" data-select-candidate="${escapeHtml(id)}">加入本次导出</button>`;
      card.innerHTML = `<div class="candidate-card-header"><span class="candidate-title-meta"><span class="candidate-number">候选 ${String(displayCandidateIndex(generated)).padStart(2, "0")}</span>${parameterMarkup}</span><span class="candidate-header-status">${recommendationMark}${selectedMark}<span class="status-chip ${hardClass}">${escapeHtml(hardCopy)}</span></span></div><div class="board-canvas"></div><div class="candidate-card-body"><div class="candidate-summary-line"><h3>${escapeHtml(candidate.plan_signature || item.plan_signature || item.plan_id)}</h3><p>${escapeHtml(Object.keys(structureCounts).sort().join(" · ") || "塔内结构")} · ${escapeHtml(summary.replacement_count ?? 0)} 格结构块</p></div>${experienceSummaryMarkup(generated)}<div class="candidate-actions"><button class="secondary-button" type="button" data-focus-candidate="${escapeHtml(id)}">查看与播放</button><button class="secondary-button" type="button" data-compare-candidate="${escapeHtml(id)}">${state.compareIds.has(id) ? "移出对比" : "加入对比"}</button>${chooseMarkup}</div></div>`;
      root.append(card); renderBoard($(".board-canvas", card), generated.board, { maxLayer: generated.board?.bounds?.layer?.max });
    });
    $("#candidate-page-label").textContent = `第 ${state.candidatePage + 1} / ${pages} 组`;
    $("#candidate-prev").disabled = state.candidatePage === 0; $("#candidate-next").disabled = state.candidatePage >= pages - 1;
    $("#compare-count").textContent = `${state.compareIds.size}/3`; $("#open-compare").disabled = state.compareIds.size < 2;
    const reviewButton = $("#review-selections");
    if (reviewButton) {
      reviewButton.disabled = selectedCandidateCount() === 0;
      reviewButton.title = reviewButton.disabled ? "请先加入至少一个候选；非推荐候选确认提示后也可导出" : "";
    }
  }

  function candidateParameterRows(snapshot = {}) {
    const rows = [];
    const add = (key, label, raw, value) => rows.push({ key, label, raw, value });
    const choice = (raw, prefix = "", suffix = "") => !raw ? "—" : raw.kind === "exact"
      ? `${prefix}${raw.value}${suffix}` : `${prefix}${raw.minimum}–${raw.maximum}${suffix}`;
    const enums = {
      tower_mode: { disallow: "禁止", allow: "允许", only: "仅塔内" },
      layout: { single: "单谜题", x: "左右", y: "前后", z: "上下" },
      serial_difficulty: { harder_low: "下层更难", easier_low: "下层更易", unrestricted: "不限制" },
      position: { center: "居中", balanced: "均衡", unrestricted: "不限制" },
      symmetry: { strong: "强", medium: "中", off: "关闭" },
      density: { compact: "紧凑", medium: "中等", dispersed: "分散" },
      difficulty_target: { fidelity: "贴近原模板", standard: "标准", strong: "更高难度" },
      experience_archetype: { auto: "自动判断", intrinsic_gate: "结构门槛型", board_reveal: "整板揭露型", hybrid: "混合型" },
      special_preset: { none: "不使用特殊预设", all_types_long_range: "全类型长程型", serial_mechanism: "串行机关型" },
      online_style_reference: { level_200_300_v1: "线上 200–300 关", off: "关闭" },
    };
    for (const [key, label] of [["tower_mode", "塔内生成"], ["layout", "谜题布局"], ["serial_difficulty", "上下难度关系"]]) {
      if (snapshot[key] != null) add(key, label, snapshot[key], enums[key][snapshot[key]] || snapshot[key]);
    }
    (snapshot.units || []).forEach((unit, index) => add(`units.${index}.step`, `谜题 ${index + 1} · 步数`, unit.step, choice(unit.step, "s")));
    const generation = snapshot.generation || {};
    if ((snapshot.unit_settings || []).some(unit => unit.uniform_groups_per_type === 0)) add("generation.automatic_group_max_difference", "自动组数最大差值", generation.automatic_group_max_difference ?? 3, `${generation.automatic_group_max_difference ?? 3} 组`);
    add("generation.min_same_type_layer_span", "同种结构块最小间隔层", generation.min_same_type_layer_span ?? 0, `${generation.min_same_type_layer_span ?? 0} 层`);
    if (generation.total_percent_minimum != null) add("generation.total_count_minimum", "结构块数量下限", Number(generation.total_percent_minimum) === 0 ? 0 : `${generation.total_percent_minimum}%`, `${generation.total_percent_minimum}%`);
    else add("generation.total_count_minimum", "结构块数量下限", generation.total_count_minimum ?? 0, `${generation.total_count_minimum ?? 0} 格`);
    for (const [key, label, suffix] of [["candidate_count", "候选数量目标", " 个"], ["attempt_budget", "单槽搜索预算", " 次"], ["assignment_attempts", "类型分配尝试上限", " 次"], ["required_pre_gate_recycles", "门槛前回收次数", " 次"], ["first_recycle_lead_tiles", "首次回收铺垫块数", " 格"], ["max_structure_layer_span", "结构块最大层间距", " 层"]]) {
      if (key in generation) add(`generation.${key}`, label, generation[key], generation[key] == null ? "不限制" : `${generation[key]}${suffix}`);
    }
    for (const [key, label] of [["total", "结构块数量上限"], ["max_layer", "结构块最高层限制"]]) {
      const absolute = key === "total" ? generation.total_count_limit : generation.max_layer;
      const percent = key === "total" ? generation.total_percent_limit : generation.max_layer_percent;
      const raw = absolute != null ? ["absolute", absolute] : percent != null ? ["percent", percent] : null;
      add(`generation.${key}_limit`, label, raw, absolute != null ? `${absolute} ${key === "total" ? "格" : "层"}` : percent != null ? `${percent}%` : "不限制");
    }
    if (generation.replacement_sources) add("generation.replacement_sources", "替换来源", generation.replacement_sources, generation.replacement_sources.map(value => ({majority_a_on_tie:"A/B 多数优先（相同选 A）",prefer_a_else_b:"优先 A（兼容旧任务）"})[value] || value).join(" / "));
    if (generation.allowed_types) add("generation.allowed_types", "结构块可用类型", generation.allowed_types, generation.allowed_types.join(" · "));
    if ("precision_mode" in generation) add("generation.precision_mode", "精准模式", generation.precision_mode, generation.precision_mode ? "开启" : "关闭");
    const qualityLabels = { position:"结构块位置", symmetry:"对称偏好", density:"分布方式", difficulty_target:"难度目标", experience_archetype:"体验原型", special_preset:"特殊机制预设", long_range_wave_count:"长程波次数", online_style_reference:"线上风格参考" };
    Object.entries(generation.quality || {}).forEach(([key, value]) => add(`generation.quality.${key}`, qualityLabels[key] || key, value, enums[key]?.[value] || (value === "auto" ? "自动" : String(value))));
    const units = [...(snapshot.unit_settings || []).map((unit, index) => ({ unit, prefix:`unit_settings.${index}`, label:`谜题 ${index + 1}` })), ...(snapshot.tower_settings ? [{ unit:snapshot.tower_settings, prefix:"tower_settings", label:"塔内结构" }] : [])];
    units.forEach(({unit, prefix, label}) => {
      add(`${prefix}.type_count`, `${label} · 结构块种类数`, unit.type_count, choice(unit.type_count, "", " 种"));
      const custom = unit.groups_per_type?.length > 0;
      const groups = custom ? unit.groups_per_type : unit.uniform_groups_per_type;
      add(`${prefix}.groups`, `${label} · 每类组数`, groups, custom ? groups.join(" / ") : groups === 0 ? `自动均衡（每类至少 1 组，差值≤${generation.automatic_group_max_difference ?? 3}）` : `每类 ${groups ?? 1} 组`);
      if (unit.allowed_types) add(`${prefix}.allowed_types`, `${label} · 可用类型`, unit.allowed_types, unit.allowed_types.join(" · "));
      for (const [key, name, suffix] of [["threshold_type", "门槛类型", ""], ["count_limit", "单元数量上限", " 格"], ["max_layer", "单元最高层", " 层"], ["required_pre_gate_recycles", "单元门槛前回收", " 次"], ["first_recycle_lead_tiles", "单元回收铺垫", " 格"]]) {
        if (unit[key] != null) add(`${prefix}.${key}`, `${label} · ${name}`, unit[key], `${unit[key]}${suffix}`);
      }
      if (unit.preferred_coords?.length) add(`${prefix}.preferred_coords`, `${label} · 指定坐标`, unit.preferred_coords, unit.preferred_coords.map(coord => coord.join(", ")).join("；"));
    });
    Object.entries(generation.solver_limits || {}).forEach(([key, value]) => add(`generation.solver_limits.${key}`, ({max_states:"求解状态上限",max_path_count:"求解路径上限"})[key] || key, value, String(value)));
    return rows;
  }
  function candidateParameterComparison(generated, candidates = []) {
    const current = generated.configured_parameters;
    if (!current) return { rows: [], references: [] };
    const canonical = value => JSON.stringify(value, (_, entry) => entry && !Array.isArray(entry) && typeof entry === "object" ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);
    const references = [];
    const seen = new Set([canonical(current)]);
    const addReference = (snapshot, label) => {
      if (!snapshot || seen.has(canonical(snapshot))) return;
      seen.add(canonical(snapshot));
      references.push({ label, rows:new Map(candidateParameterRows(snapshot).map(row => [row.key,row])) });
    };
    if (generated.uses_independent_parameters) addReference(generated.uniform_parameters, "统一方案");
    candidates.forEach((candidate, index) => addReference(candidate.configured_parameters, `候选 ${String(candidate.candidate?.candidate_index || index + 1).padStart(2, "0")}`));
    const rowMap = new Map(candidateParameterRows(current).map(row => [row.key,row]));
    references.forEach(reference => reference.rows.forEach((row,key) => { if (!rowMap.has(key)) rowMap.set(key, {...row,raw:null,value:"未单独设置"}); }));
    const rows = [...rowMap.values()].map(row => {
      const comparisons = references.flatMap(reference => {
        const other = reference.rows.get(row.key);
        return canonical(row.raw ?? null) === canonical(other?.raw ?? null) ? [] : [{label:reference.label,value:other?.value || "未单独设置"}];
      });
      return {...row,changed:comparisons.length > 0,comparisons};
    });
    return { rows:rows.sort((a,b) => Number(b.changed)-Number(a.changed)), references:references.map(reference => reference.label) };
  }

  function openCandidateParameterDialog(candidateIdValue) {
    const item = activeWorkItem();
    const generated = orderedCandidates(item).find((entry) => candidateId(entry) === candidateIdValue);
    if (!generated) return;
    const provenance = generated.parameter_provenance || {};
    const options = generated.resolved_generation_options || {};
    const plan = generated.resolved_plan || {};
    const sourceCopy = provenance.parameter_source === "batch" ? "批次统一方案" : "模板独立方案";
    const units = [...(options.unit_options || []), ...(options.tower_options ? [options.tower_options] : [])];
    const groupCopy = units.length
      ? units.map((unit, index) => `${index + 1}：${(unit.groups_per_type || []).join(" / ") || "默认"}`).join("；")
      : "—";
    const stepCopy = (plan.units || []).map((unit) => `s${unit.step}`).join(" · ") || generated.candidate?.plan_signature || item?.plan_signature || "—";
    $("#candidate-parameter-title").textContent = `候选 ${String(displayCandidateIndex(generated)).padStart(2, "0")} · 参数详情`;
    const comparison = candidateParameterComparison(generated, orderedCandidates(item));
    const rows = comparison.rows.length ? comparison.rows : [
      ["参数来源", sourceCopy], ["谜题方案", stepCopy], ["候选数量目标", options.candidate_count ?? "—"],
      ["单槽搜索预算", options.attempt_budget ?? "—"], ["结构块种类数", units.map(unit => unit.type_count).join(" / ") || "—"],
      ["每类组数", groupCopy], ["结构块数量上限", options.total_count_limit ?? (options.total_percent_limit != null ? `${options.total_percent_limit}%` : "不限制")],
      ["结构块数量下限", options.total_percent_minimum != null ? `${options.total_percent_minimum}%` : `${options.total_count_minimum ?? 0} 格`],
      ["结构块最高层", options.max_layer ?? (options.max_layer_percent != null ? `${options.max_layer_percent}%` : "不限制")],
      ["门槛前回收", `${units.map(unit => unit.required_pre_gate_recycles ?? 0).join(" / ") || "0"} 次`],
    ].map(([label,value]) => ({label,value,changed:false,comparisons:[]}));
    $("#candidate-parameter-body").innerHTML = `<dl class="candidate-parameter-grid">${rows.map(row => `<div${row.changed ? ' class="is-parameter-different"' : ""}><dt>${escapeHtml(row.label)}${row.changed ? '<span class="parameter-difference-label">不同</span>' : ""}</dt><dd><span class="parameter-current-value">${escapeHtml(row.value)}</span>${row.comparisons.map(reference => `<small class="parameter-comparison-value">${escapeHtml(reference.label)}：${escapeHtml(reference.value)}</small>`).join("")}</dd></div>`).join("")}</dl>`;
    $("#candidate-parameter-dialog").showModal();
  }

  function renderBoard(container, board, options = {}) {
    if (!container) return;
    if (!board?.tiles?.length) { container.innerHTML = '<div class="empty-state">暂无可显示的棋盘数据</div>'; return; }
    const maxAvailable = board.bounds?.layer?.max || Math.max(...board.tiles.map((tile) => tile.layer || tile.z + 1));
    const maxLayer = clamp(Number(options.maxLayer || maxAvailable), 1, maxAvailable);
    const minLayer = clamp(Number(options.minLayer || 1), 1, maxLayer);
    const allTiles = board.tiles.filter((tile) => (tile.layer || tile.z + 1) <= maxLayer && (tile.layer || tile.z + 1) >= minLayer);
    if (!allTiles.length) { container.innerHTML = '<div class="empty-state">当前层范围内没有 Tile</div>'; return; }
    const grid = 24;
    const faceWidth = grid * 2;
    const faceHeight = grid * 2;
    const towerShift = grid * (16 / 85);
    const towerMap = new Map((board.towers || []).map((tower) => [Number(tower.tower_index), tower]));
    const towerVector = (direction) => ({
      0: [0, -1],
      1: [-1, 0],
      2: [0, 1],
      3: [1, 0],
    })[Number(direction)] || [0, 0];
    const projected = (options.source ? board.tiles : allTiles).map((tile) => {
      const tower = tile.is_tower_member ? towerMap.get(Number(tile.tower_index)) : null;
      const [shiftX, shiftY] = tower ? towerVector(tower.direction) : [0, 0];
      const rawZ = Number(tile.z || 0);
      return {
        tile,
        cx: Number(tile.x) * grid + shiftX * towerShift * rawZ,
        cy: Number(tile.y) * grid + shiftY * towerShift * rawZ,
      };
    }).sort((left, right) => (Number(left.tile.z) - Number(right.tile.z)) || (Number(left.tile.y) - Number(right.tile.y)) || (Number(left.tile.x) - Number(right.tile.x)) || (Number(left.tile.tile_index) - Number(right.tile.tile_index)));
    const projectedTowers = (board.towers || []).map((tower) => ({
      tower,
      cx: Number(tower.x) * grid,
      cy: Number(tower.y) * grid,
    }));
    const extents = [...projected, ...projectedTowers];
    const xs = extents.flatMap((item) => [item.cx - faceWidth / 2, item.cx + faceWidth / 2]);
    const ys = extents.flatMap((item) => [item.cy - faceHeight / 2, item.cy + faceHeight / 2]);
    const minX = Math.min(...xs) - grid * 0.9, maxX = Math.max(...xs) + grid * 0.9;
    const minY = Math.min(...ys) - grid * 1.15, maxY = Math.max(...ys) + grid * 0.9;
    let zones = "";
    const plan = options.plan;
    if (plan && plan.unitCount === 2 && plan.towerMode !== "only") {
      const width = maxX - minX, height = maxY - minY;
      if (plan.layout === "x") {
        zones = `<rect class="zone alt" x="${minX + 2}" y="${minY + 3}" width="${width / 2 - 5}" height="${height - 7}" rx="8"/><rect class="zone" x="${minX + width / 2 + 3}" y="${minY + 3}" width="${width / 2 - 5}" height="${height - 7}" rx="8"/><text class="zone-label" x="${minX + 9}" y="${minY + 15}">谜题 1 · 左区</text><text class="zone-label" x="${minX + width / 2 + 10}" y="${minY + 15}">谜题 2 · 右区</text>`;
      } else if (plan.layout === "y") {
        zones = `<rect class="zone alt" x="${minX + 2}" y="${minY + 3}" width="${width - 4}" height="${height / 2 - 5}" rx="8"/><rect class="zone" x="${minX + 2}" y="${minY + height / 2 + 3}" width="${width - 4}" height="${height / 2 - 6}" rx="8"/><text class="zone-label" x="${minX + 9}" y="${minY + 15}">谜题 1 · 上区</text><text class="zone-label" x="${minX + 9}" y="${minY + height / 2 + 15}">谜题 2 · 下区</text>`;
      } else {
        zones = `<rect class="zone" x="${minX + 2}" y="${minY + 3}" width="${width - 4}" height="${height - 7}" rx="8"/><text class="zone-label" x="${minX + 9}" y="${minY + 15}">双谜题 · 垂直层级</text>`;
      }
    }
    const removed = new Set(options.removedIndices || []); const current = options.currentIndex;
    const mode = options.mode || (options.source ? "source" : "output");
    const staticallyCoveredIndices = new Set((board.static_relations || []).map((relation) => Number(relation.lower_index)));
    const towerMarkup = projectedTowers.map(({ tower, cx, cy }) => {
      const [vectorX, vectorY] = towerVector(tower.direction);
      const lineX = cx + vectorX * grid * 0.68;
      const lineY = cy + vectorY * grid * 0.68;
      return `<g class="tower-base"><title>塔模具 ${Number(tower.tower_index) + 1} · ${Number(tower.member_count || 0)} 格</title><rect x="${cx - faceWidth / 2}" y="${cy - faceHeight / 2}" width="${faceWidth}" height="${faceHeight}" rx="7"/><path d="M ${cx} ${cy} L ${lineX} ${lineY}"/><circle cx="${lineX}" cy="${lineY}" r="2.4"/></g>`;
    }).join("");
    const visibleTiles = new Set(allTiles);
    const tileMarkup = projected.filter(({tile}) => visibleTiles.has(tile)).map(({ tile, cx, cy }) => {
      const type = mode === "source" ? tile.source_type : tile.effective_type;
      const displayType = String(type || "");
      const color = TYPE_COLORS[displayType] || "#c9cecb";
      const isBase = /^[AB]$/.test(displayType);
      const isInitiallyVisible = tile.is_initially_visible ?? !staticallyCoveredIndices.has(Number(tile.tile_index));
      const visibilityClass = options.highlightInitialVisibility
        ? (isInitiallyVisible ? "is-visible-layer" : "is-covered-layer")
        : "";
      const classes = ["tile", isBase ? "is-base" : "", visibilityClass, isBase && options.fadeBaseTiles ? "is-detail-base" : "", tile.is_replacement ? "is-replaced" : "", removed.has(tile.tile_index) ? "is-removed" : "", current === tile.tile_index ? "is-current" : ""].filter(Boolean).join(" ");
      const opacity = mode === "difference" && !tile.is_replacement ? " opacity=\"0.1\"" : "";
      const label = /^[A-N]$/.test(displayType) ? displayType : "";
      const layer = Number(tile.layer || Number(tile.z) + 1);
      return `<g class="${classes}" data-tile-index="${Number(tile.tile_index)}" data-layer="${layer}"${opacity}><title>${escapeHtml(label || displayType)} · 第 ${layer} 层${tile.is_tower_member ? " · 塔内" : ""}</title><rect class="tile-face" x="${cx - faceWidth / 2}" y="${cy - faceHeight / 2}" width="${faceWidth}" height="${faceHeight}" rx="6" fill="${color}"/><path class="tile-highlight" d="M ${cx - faceWidth / 2 + 6} ${cy - faceHeight / 2 + 2.8} H ${cx + faceWidth / 2 - 6}"/><text class="tile-layer-label" x="${cx - faceWidth / 2 + 5}" y="${cy - faceHeight / 2 + 13}">${layer}</text><text class="tile-label" x="${cx}" y="${cy + 4}">${escapeHtml(label)}</text></g>`;
    }).join("");
    const baseWidth = maxX - minX, baseHeight = maxY - minY;
    const zoom = clamp(Number(options.zoom || 100), 75, 150) / 100;
    const viewWidth = baseWidth / zoom, viewHeight = baseHeight / zoom;
    const viewX = minX + (baseWidth - viewWidth) / 2, viewY = minY + (baseHeight - viewHeight) / 2;
    container.innerHTML = `<svg viewBox="${viewX} ${viewY} ${viewWidth} ${viewHeight}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${escapeHtml(board.source?.template_id || "关卡")} 俯视棋盘，显示第 ${minLayer} 至 ${maxLayer} 层"><defs><filter id="tile-shadow-${escapeHtml(String(board.board_id || "board").replace(/[^a-zA-Z0-9_-]/g, ""))}" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="1.6" stdDeviation="1.5" flood-color="#11251d" flood-opacity=".2"/></filter></defs>${zones}${towerMarkup}${tileMarkup}</svg>`;
  }

  function setActiveWork(index) {
    const items = successfulWorkItems();
    state.activeWorkIndex = clamp(index, 0, Math.max(0, items.length - 1)); state.candidatePage = 0; state.compareIds.clear(); stopPlayer(); state.focusCandidate = null;
    $("#compare-workspace").classList.add("is-hidden"); $("#focus-workspace").classList.add("is-hidden"); $("#candidate-workspace").classList.remove("is-hidden");
    renderCandidateWorkspace();
  }
  function toggleCompare(id) {
    if (state.compareIds.has(id)) state.compareIds.delete(id);
    else if (state.compareIds.size >= 3) { toast("一次最多横向比较 3 个候选。", "error"); return; }
    else state.compareIds.add(id);
    renderCandidateCards();
  }
  function openComparison() {
    const item = activeWorkItem(); if (!item || state.compareIds.size < 2) return;
    $("#candidate-workspace").classList.add("is-hidden"); $("#focus-workspace").classList.add("is-hidden"); $("#compare-workspace").classList.remove("is-hidden");
    const candidates = item.candidates.filter((candidate) => state.compareIds.has(candidateId(candidate)));
    const maxLayer = Math.max(...candidates.map((candidate) => candidate.board?.bounds?.layer?.max || 1));
    $("#compare-layer").max = String(maxLayer); $("#compare-layer").value = String(maxLayer); $("#compare-layer-value").textContent = String(maxLayer);
    renderComparison();
    window.scrollTo({ top: 0, behavior: "smooth" });
    window.setTimeout(() => $("#compare-title")?.focus(), 0);
  }
  function renderComparison() {
    const item = activeWorkItem(); if (!item) return;
    const candidates = item.candidates.filter((candidate) => state.compareIds.has(candidateId(candidate)));
    const maxLayer = Number($("#compare-layer").value); const mode = $("#compare-mode").value; const zoom = Number($("#compare-zoom").value);
    const canvases = $("#compare-canvases"); canvases.replaceChildren();
    candidates.forEach((generated) => {
      const card = document.createElement("article"); card.className = "compare-board-card porcelain-card";
      const summary = candidateSummary(generated); const candidate = generated.candidate || {};
      card.innerHTML = `<div class="compare-board-heading"><strong>候选 ${displayCandidateIndex(generated)}</strong><span class="status-chip success">${escapeHtml(summary.replacement_count || 0)} 格</span></div><div class="board-canvas"></div>`;
      canvases.append(card); renderBoard($(".board-canvas", card), generated.board, { maxLayer, mode, zoom });
    });
    const head = $("#comparison-head"), body = $("#comparison-body"); head.replaceChildren(); body.replaceChildren();
    const header = document.createElement("tr"); ["决策字段", ...candidates.map((candidate) => `候选 ${displayCandidateIndex(candidate)}`)].forEach((copy) => { const th = document.createElement("th"); th.textContent = copy; header.append(th); }); head.append(header);
    const baselineId = candidateId(candidates[0]);
    const layoutDifference = (candidate) => {
      const id = candidateId(candidate); if (id === baselineId) return "基准";
      const pair = (item.comparison?.pairwise || []).find((entry) => new Set([entry.left_candidate_id, entry.right_candidate_id]).has(baselineId) && new Set([entry.left_candidate_id, entry.right_candidate_id]).has(id));
      if (!pair) return "无差异摘要";
      return `${Math.round(Number(pair.position_overlap_ratio || 0) * 100)}% 位置重合 · ${(pair.brief || []).join("；")}`;
    };
    const reports = candidates.map((candidate) => experienceAnalysis(candidate));
    const showArchetype = reports.some((report) => hasReliableArchetype(report));
    const showDifficultyDistance = reports.some((report) => hasReliableDifficultyDistance({
      distance: report.difficultyDistance,
      evidenceCoverage: report.evidenceCoverage,
    }));
    const rows = [
      ["结构步数", (candidate) => candidateSteps(candidate)],
      ["结构类型", (candidate) => Object.keys(candidateSummary(candidate).structure_counts || {}).sort().join(" ") || "塔内"],
      ["结构块数量", (candidate) => candidateSummary(candidate).replacement_count ?? 0],
      ["替换 A / B", (candidate) => `${candidateSummary(candidate).replaced_a_count ?? 0} / ${candidateSummary(candidate).replaced_b_count ?? 0}`],
      ["结构块最高层", (candidate) => candidateSummary(candidate).highest_structure_layer ?? "—"],
      ["夹入 A / B", (candidate) => `${candidateSummary(candidate).nested_a_count ?? 0} / ${candidateSummary(candidate).nested_b_count ?? 0}`],
      ...(showArchetype ? [["体验原型", (candidate) => {
        const report = experienceAnalysis(candidate);
        return hasReliableArchetype(report) ? experienceArchetypeCopy(report.archetypeKey) : "—";
      }]] : []),
      ...(showDifficultyDistance ? [["与原模板难度距离", (candidate) => {
        const report = experienceAnalysis(candidate);
        const distanceReport = { distance: report.difficultyDistance, evidenceCoverage: report.evidenceCoverage };
        return hasReliableDifficultyDistance(distanceReport) ? difficultyDistanceCopy(distanceReport) : "—";
      }]] : []),
      ["线上风格距离", (candidate) => experienceOnlineStyleCopy(experienceAnalysis(candidate))],
      ["机制压力", (candidate) => experienceAnalysis(candidate).pressureLevel === "unassessed" ? "未评估" : experienceStatusCopy(experienceAnalysis(candidate).pressureLevel)],
      ["布局打平分", (candidate) => qualityScore(candidate).toFixed(3)],
      ["相对首个候选", layoutDifference],
      ["验证范围", () => "结构内在 + 硬规则"],
    ];
    rows.forEach(([label, getter]) => {
      const values = candidates.map(getter); const different = new Set(values.map((value) => JSON.stringify(value))).size > 1;
      const row = document.createElement("tr"); const labelCell = document.createElement("td"); labelCell.textContent = label; row.append(labelCell);
      values.forEach((value) => { const cell = document.createElement("td"); cell.textContent = String(value); cell.classList.toggle("is-different", different); row.append(cell); }); body.append(row);
    });
  }

  function chooseCandidate(id) {
    const item = activeWorkItem(); if (!item) return;
    const key = workKey(item); const ids = selectedIdsForWork(item); const selected = ids.includes(id);
    const generated = (item.candidates || []).find((candidate) => candidateId(candidate) === id);
    const exportEligibility = candidateExportEligibility(generated);
    if (!selected && !exportEligibility.eligible) {
      toast(exportEligibility.reason, "error");
      renderCandidateWorkspace(); renderExportSelection(); syncFocusSelectionAction();
      return;
    }
    const nextIds = selected ? ids.filter((value) => value !== id) : [...ids, id];
    state.selectedCandidates.set(key, nextIds);
    state.selectionsByJob.set(state.jobId, state.selectedCandidates); persistSelections();
    if (selectedCandidateCount() > 0) unlockFlow(4, { preserveScroll: true });
    renderCandidateWorkspace(); renderExportSelection(); syncFocusSelectionAction();
    const index = String(displayCandidateIndex(findCandidate(id))).padStart(2, "0");
    toast(
      selected
        ? `候选 ${index} 已从本次导出移除。`
        : exportEligibility.manualOverrideRequired
          ? `候选 ${index} 已加入；导出前会再次确认规则、难度或证据提示。`
          : `候选 ${index} 已加入本次导出；本项可继续多选。`,
    );
  }
  function completeActiveWorkSelection() {
    const items = successfulWorkItems();
    if (state.activeWorkIndex >= items.length - 1) return;
    setActiveWork(state.activeWorkIndex + 1);
    window.setTimeout(() => $("#active-work-label")?.focus(), 0);
  }
  function clearActiveWorkSelection() {
    const item = activeWorkItem(); if (!item) return;
    state.selectedCandidates.set(workKey(item), []); state.selectionsByJob.set(state.jobId, state.selectedCandidates); persistSelections();
    renderCandidateWorkspace(); renderExportSelection(); toast("本项候选已清空，其他成品项的选择保持不变。" );
  }
  function findCandidate(id) { return activeWorkItem()?.candidates?.find((candidate) => candidateId(candidate) === id) || null; }
  function focusedCandidateNavigation(item = activeWorkItem(), focusedCandidate = state.focusCandidate, candidates = orderedCandidates(item)) {
    const focusedId = candidateId(focusedCandidate);
    const index = candidates.findIndex((candidate) => candidateId(candidate) === focusedId);
    const targetAt = (targetIndex) => targetIndex >= 0 && targetIndex < candidates.length
      ? { candidate: candidates[targetIndex], page: Math.floor(targetIndex / 3) }
      : null;
    return {
      candidates,
      index,
      page: index >= 0 ? Math.floor(index / 3) : state.candidatePage,
      previous: index >= 0 ? targetAt(index - 1) : null,
      next: index >= 0 ? targetAt(index + 1) : null,
    };
  }
  function focusedWorkItemNavigation(items = successfulWorkItems(), item = activeWorkItem()) {
    const index = item ? items.findIndex((entry) => workKey(entry) === workKey(item)) : -1;
    const targetAt = (targetIndex) => targetIndex >= 0 && targetIndex < items.length
      ? { item: items[targetIndex], index: targetIndex }
      : null;
    return {
      items,
      index,
      previous: index >= 0 ? targetAt(index - 1) : null,
      next: index >= 0 ? targetAt(index + 1) : null,
    };
  }
  function syncFocusSelectionAction() {
    const button = $("#focus-select-candidate"); const count = $("#focus-selection-count");
    if (!button || !count) return;
    const item = activeWorkItem(); const generated = state.focusCandidate;
    const id = candidateId(generated); const total = selectedCandidateCount();
    const currentTemplateTotal = item ? selectedCandidateCountForTemplate(item.template_key) : 0;
    count.textContent = `本模板已加入 ${currentTemplateTotal} 个 · 全部模板已加入 ${total} 个`;
    count.classList.toggle("success", total > 0);
    if (!item || !generated || !id) {
      button.disabled = true;
      button.removeAttribute("data-select-candidate");
      button.setAttribute("aria-pressed", "false");
      button.textContent = "加入本次导出";
      return;
    }
    const selected = isCandidateSelected(item, id);
    const exportEligibility = candidateExportEligibility(generated);
    button.dataset.selectCandidate = id;
    button.disabled = !selected && !exportEligibility.eligible;
    button.setAttribute("aria-pressed", String(selected));
    button.className = selected
      ? "selected-candidate-button focus-select-candidate"
      : `secondary-button focus-select-candidate${exportEligibility.manualOverrideRequired ? " candidate-export-manual" : ""}`;
    button.innerHTML = selected
      ? `${icon("check")}已加入，点击移除`
      : exportEligibility.manualOverrideRequired ? "选择并导出" : "加入本次导出";
    button.title = selected ? "从本次导出列表移除当前候选" : exportEligibility.reason || "将当前候选加入本次导出";
  }
  function syncFocusCandidateNavigation() {
    const navigation = focusedCandidateNavigation();
    const previous = $("#focus-candidate-prev"); const next = $("#focus-candidate-next");
    if (previous) previous.disabled = !navigation.previous;
    if (next) next.disabled = !navigation.next;
    const candidatePosition = $("#focus-candidate-position");
    if (candidatePosition) candidatePosition.textContent = `${navigation.index >= 0 ? navigation.index + 1 : 0} / ${navigation.candidates.length}`;
    if (previous) previous.title = navigation.previous ? "上一个候选" : "已是第一个候选";
    if (next) next.title = navigation.next ? "下一个候选" : "已是最后一个候选";
    const workNavigation = focusedWorkItemNavigation();
    const previousWork = $("#focus-work-prev"); const nextWork = $("#focus-work-next");
    if (previousWork) previousWork.disabled = !workNavigation.previous;
    if (nextWork) nextWork.disabled = !workNavigation.next;
    const workPosition = $("#focus-work-position");
    if (workPosition) {
      workPosition.textContent = `${workNavigation.index >= 0 ? workNavigation.index + 1 : 0} / ${workNavigation.items.length}`;
      const skipped = Math.max(0, (state.jobResult?.items?.length || 0) - workNavigation.items.length);
      workPosition.title = `仅切换已有候选的成品项${skipped ? `；跳过 ${skipped} 个无候选成品项，可返回列表处理` : ""}`;
    }
    const targetCopy = (target) => `${target.item.template_key} · ${target.item.plan_signature || target.item.plan_id}`;
    if (previousWork) previousWork.title = workNavigation.previous ? `上一成品项：${targetCopy(workNavigation.previous)}` : "已是第一个有候选的成品项";
    if (nextWork) nextWork.title = workNavigation.next ? `下一成品项：${targetCopy(workNavigation.next)}` : "已是最后一个有候选的成品项";
  }
  function moveFocusedCandidate(offset) {
    const navigation = focusedCandidateNavigation();
    const next = Number(offset) < 0 ? navigation.previous : navigation.next;
    if (!next) return;
    openFocus(candidateId(next.candidate), { preserveReturnContext: true, scrollBehavior: "auto" });
  }
  function moveFocusedWorkItem(offset) {
    const navigation = focusedWorkItemNavigation();
    const next = Number(offset) < 0 ? navigation.previous : navigation.next;
    const candidate = next ? orderedCandidates(next.item)[0] : null;
    if (!candidate) return;
    state.activeWorkIndex = next.index; state.candidatePage = 0; state.compareIds.clear();
    openFocus(candidateId(candidate), { preserveReturnContext: true, scrollBehavior: "auto" });
  }
  function closeFocus() {
    const originIndex = state.focusReturnWorkKey
      ? successfulWorkItems().findIndex((item) => workKey(item) === state.focusReturnWorkKey)
      : -1;
    if (originIndex >= 0) state.activeWorkIndex = originIndex;
    if (Number.isInteger(state.focusReturnPage)) state.candidatePage = state.focusReturnPage;
    stopPlayer();
    $("#focus-workspace").classList.add("is-hidden");
    $("#candidate-workspace").classList.remove("is-hidden");
    renderCandidateWorkspace();
    const returnId = state.focusReturnId;
    state.focusReturnId = null; state.focusReturnPage = null; state.focusReturnWorkKey = null;
    window.setTimeout(() => ($(`[data-focus-candidate="${returnId}"]`) || $("#active-work-label"))?.focus(), 0);
  }
  function openFocus(id, options = {}) {
    const candidate = findCandidate(id); if (!candidate) return;
    if (!options.preserveReturnContext) {
      state.focusReturnId = id;
      state.focusReturnPage = state.candidatePage;
      state.focusReturnWorkKey = workKey(activeWorkItem());
    }
    state.focusCandidate = candidate; state.player.unitIndex = 0; state.player.routeKey = "threshold"; state.player.step = 0; stopPlayer();
    $("#candidate-workspace").classList.add("is-hidden"); $("#compare-workspace").classList.add("is-hidden"); $("#focus-workspace").classList.remove("is-hidden");
    const maxLayer = candidate.board?.bounds?.layer?.max || 1;
    $("#focus-layer").max = String(maxLayer); setFocusLayer(maxLayer, false);
    const index = displayCandidateIndex(candidate);
    const item = activeWorkItem();
    $("#focus-title").textContent = `${item.template_key} · ${candidate.candidate?.plan_signature || item.plan_signature || item.plan_id} · 候选 ${index}`;
    $("#focus-board-label").textContent = candidateId(candidate);
    renderFocusSummary(); setupPlayer(); renderPlayer(); syncFocusCandidateNavigation(); syncFocusSelectionAction();
    window.scrollTo({ top: 0, behavior: options.scrollBehavior || "smooth" });
    window.setTimeout(() => $("#focus-title")?.focus(), 0);
  }
  function setFocusLayer(value, rerender = true) {
    const input = $("#focus-layer"); if (!input) return;
    const next = clamp(Math.round(Number(value) || 1), Number(input.min || 1), Number(input.max || 1));
    input.value = String(next); $("#focus-layer-value").textContent = `${next} 层`;
    $("#focus-layer-down").disabled = next <= Number(input.min || 1);
    $("#focus-layer-up").disabled = next >= Number(input.max || 1);
    $("#focus-layer-all").disabled = next >= Number(input.max || 1);
    if (rerender) renderPlayer();
  }
  function renderFocusSummary() {
    const generated = state.focusCandidate; if (!generated) return;
    const summary = candidateSummary(generated); const candidate = generated.candidate || {};
    const source = sourceTemplateMetrics(activeWorkItem(), generated);
    const structureLayers = candidateStructureLayerMetrics(generated);
    const types = Object.entries(summary.structure_counts || {}).map(([type, count]) => `${type}×${count}`).join(" · ") || "塔内结构";
    const hardCopy = candidateExportEligibility(generated).shortLabel;
    const pressurePanel = focusExperienceSummaryMarkup(generated);
    const supportingExperience = experienceSummaryMarkup(generated, { includeArchetype: false, includePressure: false, showEmptyGuidance: false });
    const samplePassMarkup = samplePassSummaryMarkup(generated);
    $("#focus-summary").innerHTML = `<div class="focus-summary-heading"><div class="focus-summary-identity"><span class="soft-label">候选摘要</span><h3>候选 ${escapeHtml(displayCandidateIndex(generated))} · ${escapeHtml(candidate.plan_signature || "tower")}</h3><p>${escapeHtml(types)}</p></div>${pressurePanel}</div>${supportingExperience}<dl class="summary-list"><dt>原模板 Tile 总数</dt><dd>${escapeHtml(source.tileCount)}</dd><dt>原模板非塔 Tile 总数</dt><dd>${escapeHtml(source.nonTowerTileCount)}</dd><dt>原模板总层数</dt><dd>${escapeHtml(source.maxLayer)}</dd><dt>原模板非塔层数</dt><dd>${escapeHtml(source.nonTowerMaxLayer)}</dd><dt>目标结构步数</dt><dd>${escapeHtml(candidateSteps(generated))}</dd><dt>结构块</dt><dd>${escapeHtml(summary.replacement_count ?? 0)} 格</dd><dt>A / B 替换</dt><dd>${escapeHtml(summary.replaced_a_count ?? 0)} / ${escapeHtml(summary.replaced_b_count ?? 0)}</dd><dt>结构内夹 A / B</dt><dd>${escapeHtml(summary.nested_a_count ?? 0)} / ${escapeHtml(summary.nested_b_count ?? 0)}</dd><dt>结构块最低层</dt><dd>${escapeHtml(structureLayers.minimum)}</dd><dt>结构块最高层</dt><dd>${escapeHtml(structureLayers.maximum)}</dd><dt>结构块层间距</dt><dd>${escapeHtml(structureLayers.span)}</dd><dt>布局打平分</dt><dd>${qualityScore(generated).toFixed(3)}</dd><dt>硬校验</dt><dd>${escapeHtml(hardCopy)}</dd></dl><details class="experience-breakdown"><summary aria-label="展开本候选整板体验依据">查看本候选整板体验依据</summary><div>${experienceEvidenceMarkup(generated)}</div></details><details class="difficulty-breakdown"><summary aria-label="展开本候选难度策略样本">查看难度策略样本</summary><div>${difficultyReportMarkup(generated)}</div></details><details class="quality-breakdown"><summary aria-label="展开本候选布局打平分">查看布局打平分项</summary><div>${qualityFormulaMarkup(generated)}</div></details>`;
    $("#focus-summary > .summary-list").insertAdjacentHTML("beforebegin", samplePassMarkup);
    const experienceBreakdown = $(".experience-breakdown", $("#focus-summary"));
    experienceBreakdown?.addEventListener("toggle", () => $("summary", experienceBreakdown)?.setAttribute("aria-expanded", String(experienceBreakdown.open)));
    const difficultyBreakdown = $(".difficulty-breakdown", $("#focus-summary"));
    difficultyBreakdown?.addEventListener("toggle", () => $("summary", difficultyBreakdown)?.setAttribute("aria-expanded", String(difficultyBreakdown.open)));
    const breakdown = $(".quality-breakdown", $("#focus-summary"));
    breakdown?.addEventListener("toggle", () => $("summary", breakdown)?.setAttribute("aria-expanded", String(breakdown.open)));
  }
  function playerUnits() { return state.focusCandidate?.board?.solver_traces?.units || []; }
  function currentUnit() { return playerUnits()[state.player.unitIndex] || null; }
  function currentTrace() {
    const unit = currentUnit(); if (!unit) return null;
    if (state.player.routeKey === "threshold") return unit.threshold_trace;
    const capacity = Number(state.player.routeKey.replace("capacity_", ""));
    return unit.capacity_traces?.find((item) => item.capacity === capacity)?.trace || null;
  }
  function setupPlayer() {
    const units = playerUnits(); const unitSelect = $("#trace-unit"); const routeSelect = $("#trace-route");
    unitSelect.replaceChildren(); routeSelect.replaceChildren();
    if (!units.length) {
      const option = document.createElement("option"); option.textContent = "塔内结构无固定轨迹"; unitSelect.append(option); unitSelect.disabled = true;
      const routeOption = document.createElement("option"); routeOption.textContent = "不计算解密顺序"; routeSelect.append(routeOption); routeSelect.disabled = true; return;
    }
    unitSelect.disabled = false;
    units.forEach((unit, index) => { const option = document.createElement("option"); option.value = String(index); option.textContent = `谜题 ${index + 1} · ${unitRoleLabel(unit.role)} · s${unit.target_step}`; unitSelect.append(option); });
    unitSelect.value = String(state.player.unitIndex); renderTraceRoutes();
  }
  function unitRoleLabel(role) {
    return ({ single: "单谜题", left: "左区", right: "右区", top: "上区", bottom: "下区", upper: "高层", lower: "低层" })[role] || "结构单元";
  }
  function renderTraceRoutes() {
    const unit = currentUnit(); const select = $("#trace-route"); select.replaceChildren(); if (!unit) return;
    const choices = [["threshold", `门槛轨迹 · s${unit.target_step}`]];
    (unit.capacity_traces || []).forEach((item) => { if (item.proof_available && item.trace?.available) choices.push([`capacity_${item.capacity}`, `${item.capacity} 格背包安全轨迹`]); });
    choices.forEach(([value, label]) => { const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option); });
    if (!choices.some(([value]) => value === state.player.routeKey)) state.player.routeKey = "threshold";
    select.value = state.player.routeKey; select.disabled = false;
  }
  function stopPlayer() {
    if (state.player.timer) window.clearInterval(state.player.timer); state.player.timer = null;
    const use = $("#trace-play use"); if (use) use.setAttribute("href", "#i-play");
    $("#trace-play")?.setAttribute("aria-label", "播放");
  }
  function renderPlayer() {
    const generated = state.focusCandidate; if (!generated) return;
    const trace = currentTrace(); const steps = trace?.steps || [];
    state.player.step = clamp(state.player.step, 0, steps.length);
    $("#trace-timeline").max = String(steps.length); $("#trace-timeline").value = String(state.player.step);
    $("#trace-step-readout").textContent = `${state.player.step} / ${steps.length}`;
    $("#trace-prev").disabled = state.player.step <= 0;
    $("#trace-next").disabled = state.player.step >= steps.length;
    $("#trace-title").textContent = currentUnit() ? `谜题 ${state.player.unitIndex + 1} · s${currentUnit().target_step}` : "塔内结构无固定轨迹";
    const currentStep = state.player.step > 0 ? steps[state.player.step - 1] : null;
    const removed = steps.slice(0, Math.max(0, state.player.step - 1)).map((step) => step.tile_index);
    renderBoard($("#focus-board"), generated.board, { maxLayer: Number($("#focus-layer").value), currentIndex: currentStep?.tile_index, removedIndices: removed, fadeBaseTiles: true });
    const priorStep = state.player.step > 1 ? steps[state.player.step - 2] : null;
    const bagBefore = currentStep?.bag_before || priorStep?.bag_after_settlement || [];
    const bagInserted = currentStep?.bag_after_insert || [];
    const bagSettled = currentStep?.bag_after_settlement || [];
    const routeCapacity = steps.reduce((largest, step) => Math.max(
      largest,
      step?.bag_before?.length || 0,
      step?.bag_after_insert?.length || 0,
      step?.bag_after_settlement?.length || 0,
    ), 0);
    const capacity = trace?.capacity || Math.max(7, routeCapacity);
    const renderBag = (selector, bag) => {
      const root = $(selector); root.replaceChildren();
      for (let index = 0; index < capacity; index += 1) {
        const slot = document.createElement("span"); const value = bag[index] || "";
        slot.className = `bag-slot${value ? " filled" : ""}`; slot.textContent = value;
        if (value) slot.style.setProperty("--slot-color", TYPE_COLORS[value] || "#d8dcd9");
        root.append(slot);
      }
    };
    renderBag("#bag-before-slots", bagBefore); renderBag("#bag-insert-slots", bagInserted); renderBag("#bag-slots", bagSettled);
    $("#trace-result").classList.remove("is-match");
    $("#trace-gate").classList.remove("is-gate");
    if (!trace) {
      $("#trace-click-tile").textContent = "塔内结构"; $("#trace-click-layer").textContent = "没有固定点击顺序";
      $("#trace-result").textContent = "不计算轨迹"; $("#trace-gate").textContent = "塔内 Tile 会实时变化";
      $("#trace-event").textContent = "塔内 Tile 实时变化，不声明固定解密顺序。";
    } else if (!currentStep) {
      $("#trace-click-tile").textContent = "等待开始"; $("#trace-click-layer").textContent = `共 ${steps.length} 次点击`;
      $("#trace-result").textContent = "背包为空"; $("#trace-gate").textContent = `目标 s${currentUnit()?.target_step ?? "—"}`;
      $("#trace-event").textContent = `从空背包开始；这条轨迹共 ${steps.length} 次点击。`;
    } else {
      const layer = currentStep.coordinate?.layer ?? "—";
      $("#trace-click-tile").textContent = currentStep.tile_type || "—"; $("#trace-click-layer").textContent = `第 ${layer} 层`;
      $("#trace-result").textContent = currentStep.matched_type ? `消除 ${currentStep.matched_type} × 3` : "本步未消除";
      $("#trace-result").classList.toggle("is-match", Boolean(currentStep.matched_type));
      $("#trace-gate").textContent = currentStep.is_gate_step ? `已到达 s${currentStep.gate_structure_step} 门槛` : `继续向 s${currentUnit()?.target_step ?? "—"} 推进`;
      $("#trace-gate").classList.toggle("is-gate", Boolean(currentStep.is_gate_step));
      const bagCopy = (bag) => bag.length ? `[${bag.join(" ")}]` : "空";
      $("#trace-event").textContent = `第 ${state.player.step} 步，点击 ${currentStep.tile_type}，第 ${layer} 层。结算前背包 ${bagCopy(bagBefore)}，插入后 ${bagCopy(bagInserted)}，三消结算后 ${bagCopy(bagSettled)}。${currentStep.matched_type ? `消除 ${currentStep.matched_type} 三枚` : "本步未消除"}${currentStep.is_gate_step ? `，到达 s${currentStep.gate_structure_step} 门槛` : ""}。`;
    }
  }
  function stepPlayer(delta) { stopPlayer(); const trace = currentTrace(); state.player.step = clamp(state.player.step + delta, 0, trace?.steps?.length || 0); renderPlayer(); }
  function togglePlayer() {
    const trace = currentTrace(); if (!trace?.steps?.length) return;
    if (state.player.timer) { stopPlayer(); return; }
    if (state.player.step >= trace.steps.length) state.player.step = 0;
    $("#trace-play use")?.setAttribute("href", "#i-pause");
    $("#trace-play")?.setAttribute("aria-label", "暂停");
    state.player.timer = window.setInterval(() => {
      state.player.step += 1; renderPlayer(); if (state.player.step >= trace.steps.length) stopPlayer();
    }, Number($("#trace-speed").value));
  }

  function renderExportSelection(lockedSelections = null) {
    const root = $("#export-selection-list"); root.replaceChildren();
    const locked = Array.isArray(lockedSelections) ? lockedSelections.reduce((map, choice) => {
      const key = `${choice.template_key}::${choice.resolved_plan_id}`;
      map.set(key, [...(map.get(key) || []), choice.candidate_id]); return map;
    }, new Map()) : null;
    const items = successfulWorkItems(); let count = 0; let manualCount = 0;
    items.forEach((item) => {
      const ids = locked ? normalizeSelectedCandidateIds(locked.get(workKey(item))) : selectedIdsForWork(item);
      ids.forEach((id) => {
        const generated = item.candidates.find((candidate) => candidateId(candidate) === id); if (!generated) return;
        const exportEligibility = candidateExportEligibility(generated);
        count += 1; const row = document.createElement("div"); row.className = "export-candidate-row";
        const details = document.createElement("div"); const title = document.createElement("strong"); title.textContent = `${item.template_key} · ${item.plan_signature || item.plan_id} · 候选 ${String(displayCandidateIndex(generated)).padStart(2, "0")}`;
        const small = document.createElement("small"); small.textContent = `${id} · ${candidateSummary(generated).replacement_count ?? 0} 格结构块`;
        details.append(title, small); const status = document.createElement("span"); status.className = `status-chip ${exportEligibility.className}`;
        if (exportEligibility.manualOverrideRequired) manualCount += 1;
        status.textContent = locked && exportEligibility.eligible
          ? exportEligibility.manualOverrideRequired ? "已锁定 · 人工确认" : "已锁定 · 硬校验已证明"
          : exportEligibility.label;
        row.append(details, status); root.append(row);
      });
    });
    $("#export-selection-count").textContent = locked
      ? `${count} 项 · 本次导出锁定选择${manualCount ? ` · ${manualCount} 项已人工确认` : ""}`
      : `${count} 项${manualCount ? ` · ${manualCount} 项需人工确认` : ""}`;
    const selectionStatus = $("#export-selection-status");
    if (selectionStatus) {
      selectionStatus.className = `status-chip ${manualCount ? "warning" : "success"}`;
      selectionStatus.textContent = manualCount
          ? "含待人工确认候选"
          : "候选硬校验通过";
    }
    const launch = $("#export-selected");
    launch.disabled = count === 0 || Boolean(locked);
    launch.innerHTML = locked
      ? `${icon("lock")}本次导出已锁定`
      : manualCount
        ? `确认风险并导出${icon("arrow")}`
        : `预检并导出到本地${icon("arrow")}`;
  }
  function resetExportViewContext() {
    state.exportLoadToken += 1;
    if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
    state.exportPollTimer = null; state.exportPollFailures = 0;
    state.exportId = null; state.exportSnapshot = null; state.exportStartedAt = null;
    $(".export-panel")?.classList.remove("is-complete");
    $("#export-progress")?.classList.add("is-hidden");
    $("#export-success")?.classList.add("is-hidden");
    $("#exported-output-list")?.replaceChildren();
    $("#export-selection-list")?.replaceChildren();
    renderActivityTimeline($("#export-activity-log"), [], { emptyCopy: "导出尚未开始" });
    if ($("#export-selection-count")) $("#export-selection-count").textContent = "0 项";
    if ($("#export-selection-status")) {
      $("#export-selection-status").className = "status-chip success";
      $("#export-selection-status").textContent = "候选硬校验通过";
    }
    const launch = $("#export-selected");
    if (launch) {
      delete launch.dataset.isBusy;
      delete launch.dataset.originalCopy;
      delete launch.dataset.originalMarkup;
      launch.disabled = true;
      launch.innerHTML = `${icon("lock")}请先确认当前任务候选`;
    }
  }
  function openExportStep() {
    if (!state.jobId || !state.jobResult) return;
    const locked = state.exportSnapshot?.job_id === state.jobId && state.exportSnapshot?.locked_selections?.length
      ? state.exportSnapshot.locked_selections
      : null;
    renderExportSelection(locked);
    setFlow(4);
  }
  async function exportSelected(options = {}) {
    const button = $("#export-selected");
    const exportingJobId = state.jobId;
    const exportLoadToken = ++state.exportLoadToken;
    try {
      if (state.exportSnapshot?.job_id === state.jobId && state.exportSnapshot?.locked_selections?.length) throw new Error("当前导出已锁定；请先等待完成或终止，再返回候选创建新的导出。");
      const selections = [];
      const staleSelections = [];
      const manualEvidenceSelections = [];
      successfulWorkItems().forEach((item) => {
        const candidateById = new Map((item.candidates || []).map((candidate) => [candidateId(candidate), candidate]));
        selectedIdsForWork(item).forEach((id) => {
          const candidate = candidateById.get(id);
          const eligibility = candidateExportEligibility(candidate);
          if (!candidate || !eligibility.eligible) {
            staleSelections.push({ key: workKey(item), id });
            return;
          }
          const selection = {
            template_key: item.template_key,
            resolved_plan_id: item.plan_id,
            candidate_id: id,
            manual_evidence_override: eligibility.manualOverrideRequired,
          };
          selections.push(selection);
          if (eligibility.manualOverrideRequired) manualEvidenceSelections.push(selection);
        });
      });
      if (staleSelections.length) {
        staleSelections.forEach(({ key, id }) => {
          state.selectedCandidates.set(key, selectedIdsForWork(key).filter((value) => value !== id));
        });
        state.selectionsByJob.set(state.jobId, state.selectedCandidates); persistSelections();
        renderCandidateWorkspace(); renderExportSelection();
        throw new Error("候选已不存在或生成记录已失效，已清除陈旧选择，请重新确认。");
      }
      if (!selections.length) throw new Error("请至少选择一个候选。");
      if (manualEvidenceSelections.length && options.manualEvidenceConfirmed !== true) {
        requestConfirmation(
          `确认导出 ${manualEvidenceSelections.length} 个非推荐候选？`,
          "其中可能包含结构规则未通过、未达难度目标或动态依据不足的候选。它们不属于默认推荐；继续后，具体结论与人工确认状态会写入 manifest 和验证报告。",
          () => exportSelected({ manualEvidenceConfirmed: true }),
          {
            safeLabel: "返回检查",
            confirmLabel: "已了解，继续导出",
            danger: false,
          },
        );
        return;
      }
      setBusy(button, true, "正在启动导出…");
      const record = await api(`/jobs/${encodeURIComponent(exportingJobId)}/exports`, { method: "POST", body: { selections, idempotency_key: uuid() } });
      if (state.exportLoadToken !== exportLoadToken || state.jobId !== exportingJobId || record.job_id !== exportingJobId) return;
      if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
      state.exportId = record.export_id; state.exportSnapshot = record;
      state.exportStartedAt = record.started_at || record.created_at || new Date().toISOString();
      renderExportSelection(record.locked_selections);
      $(".export-panel")?.classList.remove("is-complete");
      $("#export-success").classList.add("is-hidden"); $("#export-progress").classList.remove("is-hidden");
      renderExportProgress(record); pollExport(record.export_id);
    } catch (error) { toast(error.message, "error"); }
    finally {
      if (state.jobId === exportingJobId) {
        setBusy(button, false);
        if (state.exportSnapshot?.job_id === state.jobId && state.exportSnapshot?.locked_selections?.length) renderExportSelection(state.exportSnapshot.locked_selections);
      }
    }
  }
  function exportPhase(record) {
    const phase = record?.control?.phase || "";
    if (record?.status === "completed") return { index: 3, copy: "" };
    if (record?.status === "cancelled") return { index: Math.max(0, Number(record?.progress?.completed || 0) ? 1 : 0), copy: "已在安全检查点终止" };
    if (record?.status === "failed") return { index: Math.max(0, Number(record?.progress?.completed || 0) ? 1 : 0), copy: "导出失败，可查看原因后重试" };
    if (["export_prepare_selection", "export_validate_request"].includes(phase)) return { index: 1, copy: "正在独立复验所选候选" };
    if (["export_initialize_workspace", "export_workspace_audited"].includes(phase)) return { index: 2, copy: "正在审计输出工作区" };
    if (record?.status === "committing" || phase === "export_commit") return { index: 3, copy: "正在执行不可中断的安全写入" };
    return { index: 0, copy: "正在按锁定配置重建所选候选" };
  }
  function renderExportProgress(record) {
    state.exportSnapshot = record;
    state.exportStartedAt = record.started_at || state.exportStartedAt || record.created_at || new Date().toISOString();
    const phase = exportPhase(record);
    const active = ["queued", "running", "pause_requested", "paused", "cancel_requested", "committing"].includes(record.status);
    const pauseRequested = record.status === "pause_requested";
    const paused = record.status === "paused";
    $("#export-progress")?.classList.toggle("is-terminal", ["completed", "failed", "cancelled"].includes(record.status));
    const progress = record.progress || {};
    const completed = Math.max(0, Number(progress.completed || 0));
    const total = Math.max(0, Number(progress.total || 0));
    const rebuildDetails = latestEvent(record.events, "rebuild_progress")?.details || {};
    const rebuilt = Math.max(0, Number(rebuildDetails.completed_work_items || 0));
    const rebuildTotal = Math.max(0, Number(rebuildDetails.work_item_count || 0));
    const rebuildRatio = rebuildTotal ? Math.min(rebuilt, rebuildTotal) / rebuildTotal : 0;
    let flowPercent = phase.index === 0 ? (active ? 5 + Math.round(rebuildRatio * 20) : 0) : phase.index === 1
      ? 25 + (total ? Math.round((Math.min(completed, total) / total) * 45) : 0)
      : phase.index === 2 ? 78 : 92;
    if (record.status === "completed") flowPercent = 100;
    if (["failed", "cancelled"].includes(record.status)) flowPercent = Math.min(flowPercent, 99);
    $("#export-progress-title").textContent = ({
      queued: "导出任务已排队", running: "正在复验并导出", pause_requested: "正在安全暂停",
      paused: "导出任务已暂停", cancel_requested: "正在安全终止", committing: "正在安全写入",
      cancelled: "导出任务已终止", failed: "导出失败", completed: "导出完成",
    })[record.status] || "导出任务";
    $("#export-progress-copy").textContent = record.error?.message || phase.copy;
    const verified = record.status === "completed" ? total : Math.min(completed, total);
    $("#export-progress-count").textContent = phase.index === 0
      ? `${Math.min(rebuilt, rebuildTotal)} / ${rebuildTotal} 份成品方案已重建`
      : `${verified} / ${total} 项已复验`;
    $("#export-progress-percent").textContent = `${flowPercent}%`;
    $("#export-progress-track").setAttribute("aria-valuenow", String(flowPercent));
    $("#export-progress-fill").style.width = `${flowPercent}%`;
    const rebuildItem = [rebuildDetails.template_key, rebuildDetails.plan_id].filter(Boolean).join(" · ");
    $("#export-current-item").textContent = record.status === "completed"
      ? "所选候选已完成复验并安全写入"
      : phase.index === 0
      ? rebuildItem ? `当前重建：${rebuildItem}` : "正在建立可复现任务"
      : progress.current_item
        ? `${completed === 0 ? "正在复验" : "当前"}：${progress.current_item}`
        : phase.index >= 2 ? "所选候选复验完成，正在收尾" : "正在准备独立复验";
    const elapsed = effectiveTaskElapsed(record);
    const measuredCompleted = phase.index === 0 ? rebuilt : completed;
    const measuredTotal = phase.index === 0 ? rebuildTotal : total;
    const remaining = measuredCompleted > 0 && measuredCompleted < measuredTotal
      ? Math.max(0, (elapsed / measuredCompleted) * (measuredTotal - measuredCompleted))
      : null;
    const timePrefix = `${record.status === "completed" ? "总用时" : "已用时"} ${formatDuration(elapsed)}`;
    $("#export-elapsed").textContent = record.status === "completed"
      ? `${timePrefix} · 已完成`
      : paused
      ? `有效计算 ${formatDuration(elapsed)} · 已暂停，继续后恢复估算`
      : pauseRequested
      ? `有效计算 ${formatDuration(elapsed)} · 正在前往安全暂停点`
      : remaining !== null
      ? `${timePrefix} · ${phase.index === 0 ? "重建" : "复验"}剩余 ${formatApproxDuration(remaining)}（粗估）`
      : phase.index >= 2 && active ? `${timePrefix} · 复验完成，正在收尾` : `${timePrefix} · 正在测算剩余时间`;
    $("#export-last-activity").textContent = `服务端最近进展 ${formatClock(record.last_progress_at || record.started_at || record.created_at)}`;
    announce(
      "#export-live",
      `${$("#export-progress-title").textContent}，${$("#export-progress-count").textContent}，${flowPercent}%：${$("#export-current-item").textContent}`,
    );
    renderActivityTimeline($("#export-activity-log"), record.events, { limit: 6, emptyCopy: "正在等待后台导出事件" });
    $("#export-control-note").textContent = record.status === "completed"
      ? "输出与运行记录已保留"
      : record.status === "committing"
      ? "已进入事务提交，完成前不可暂停或终止"
      : paused ? "已完成结果与事务状态均已保留" : pauseRequested ? "已收到暂停请求，当前原子步骤完成后暂停" : "提交前可在安全检查点暂停或终止";
    $$("#export-phase-list li").forEach((item) => {
      const index = Number(item.dataset.exportPhase);
      item.classList.toggle("is-done", index < phase.index || record.status === "completed");
      item.classList.toggle("is-current", active && index === phase.index);
    });
    $("#export-spinner").classList.toggle("is-hidden", !active || paused);
    $("#export-pause").disabled = !(record.actions?.can_pause || record.actions?.can_resume);
    $("#export-pause").dataset.action = record.actions?.can_resume ? "resume" : "pause";
    $("#export-pause").innerHTML = record.actions?.can_resume
      ? `${icon("play")}<span>继续</span>`
      : `${icon("pause")}<span>暂停</span>`;
    $("#export-cancel").disabled = !record.actions?.can_cancel;
    $("#export-retry").classList.toggle("is-hidden", !record.actions?.can_retry);
  }
  async function pollExport(exportId) {
    try {
      const record = await api(`/exports/${encodeURIComponent(exportId)}`);
      if (state.exportId !== exportId) return;
      state.exportPollFailures = 0;
      renderExportProgress(record);
      if (["queued", "running", "pause_requested", "paused", "cancel_requested", "committing"].includes(record.status)) {
        state.exportPollTimer = window.setTimeout(() => pollExport(exportId), 900); return;
      }
      if (record.status === "completed") {
        showCompletedExport(record, true);
      } else if (record.error?.message) toast(record.error.message, "error");
    } catch (error) {
      if (state.exportId !== exportId) return;
      state.exportPollFailures += 1;
      const delay = Math.min(10_000, 900 * (2 ** Math.min(state.exportPollFailures, 4)));
      $("#export-progress-copy").textContent = `连接暂时中断，后台导出可能仍在运行；${Math.ceil(delay / 1000)} 秒后自动重连。`;
      $("#export-last-activity").textContent = "状态待确认 · 正在自动重连";
      announce("#export-live", `连接暂时中断，${Math.ceil(delay / 1000)} 秒后自动重连`);
      if (state.exportPollFailures === 1) toast(error.message, "error");
      state.exportPollTimer = window.setTimeout(() => pollExport(exportId), delay);
    }
  }
  function updateExportSuccessActionPriority() {
    const configured = Boolean(state.recentExport?.parameterConfigurationCompleted);
    const newTask = $("#export-new-task");
    const configure = $("#configure-exported-templates");
    if (!newTask || !configure) return;
    newTask.classList.toggle("primary-button", configured);
    newTask.classList.toggle("secondary-button", !configured);
    configure.classList.toggle("primary-button", !configured);
    configure.classList.toggle("secondary-button", configured);
    newTask.title = configured ? "新模板参数已计算，可开始下一批任务" : "也可跳过参数配置，直接开始下一批任务";
    configure.title = configured ? "已完成本次参数计算，可再次查看或重新计算" : "推荐下一步：沿用刚导出的模板计算新参数";
  }
  function rememberCompletedExport(record) {
    const result = record.result || {};
    const exportId = record.export_id || state.exportId || "";
    if (!exportId) return null;
    const sameExport = state.recentExport?.exportId === exportId;
    const hasServerMarker = Object.hasOwn(record, "parameter_configured")
      || Object.hasOwn(record, "parameter_configured_at");
    const alreadyConfigured = hasServerMarker
      ? Boolean(record.parameter_configured || record.parameter_configured_at)
      : sameExport && Boolean(state.recentExport?.parameterConfigurationCompleted);
    if (!sameExport) {
      state.parameterResolveToken += 1;
      state.parameterCalculationToken += 1;
      state.parameterSelectionId = null;
      state.parameterSelectionContext = null;
    }
    state.recentExport = {
      exportId,
      jobId: record.job_id || state.jobId,
      result,
      parameterConfigurationCompleted: alreadyConfigured,
      parameterConfiguredAt: record.parameter_configured_at || null,
    };
    return state.recentExport;
  }
  function showCompletedExport(record, notify = false) {
    const recentExport = rememberCompletedExport(record);
    if (!recentExport) return;
    const result = recentExport.result || {};
    state.completedSteps.add(4);
    renderExportProgress(record);
    $(".export-panel")?.classList.add("is-complete");
    $("#export-progress").classList.remove("is-hidden");
    $("#export-success").classList.remove("is-hidden");
    $("#exported-output-list").replaceChildren(...(result.outputs || []).map((output) => { const div = document.createElement("div"); div.className = "exported-output"; div.textContent = `${output.output_template_id}_m.bytes`; return div; }));
    updateExportSuccessActionPriority();
    $("#recent-export-tab").disabled = false;
    $("#calibration-recent-export-tab").disabled = false;
    setFlow(4);
    updateParameterResolveState();
    if (notify) toast(`已安全导出 ${result.outputs?.length || 0} 个新模板。`);
  }
  async function openCompletedExportDirectory() {
    const exportId = state.recentExport?.exportId;
    if (!exportId) { toast("请先完成一次验证导出。", "error"); return; }
    const button = $("#open-export-directory");
    setBusy(button, true, "正在打开…");
    try {
      await api(`/exports/${encodeURIComponent(exportId)}/open-directory`, { method: "POST", body: {} });
      toast("已打开本次导出的输出目录。");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }
  async function controlCurrentExport(action) {
    if (!state.exportId) return;
    const exportId = state.exportId;
    const jobId = state.jobId;
    try {
      const record = await api(`/exports/${encodeURIComponent(exportId)}/control`, { method: "POST", body: { action } });
      if (state.exportId !== exportId || state.jobId !== jobId || record.export_id !== exportId) return;
      renderExportProgress(record);
      if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
      pollExport(exportId);
    } catch (error) { toast(error.message, "error"); }
  }

  async function refreshTasks() {
    const root = $("#task-list");
    try {
      const [jobPayload, exportPayload] = await Promise.all([api("/jobs"), api("/exports")]);
      const jobSummaries = jobPayload.jobs || [];
      const retryDetails = await Promise.all(jobSummaries
        .filter((item) => item.actions?.can_retry || item.actions?.can_continue_search || item.actions?.can_use_partial_results)
        .map(async (item) => {
          try { return [item.job_id, await api(`/jobs/${encodeURIComponent(item.job_id)}`)]; }
          catch { return [item.job_id, null]; }
        }));
      const detailByJob = new Map(retryDetails);
      const jobs = jobSummaries.map((item) => ({ ...item, ...(detailByJob.get(item.job_id) || {}), kind: "generation", id: item.job_id }));
      const exports = (exportPayload.exports || []).map((item) => ({ ...item, kind: "export", id: item.export_id }));
      const tasks = [...jobs, ...exports].sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
      if (!tasks.length) { root.innerHTML = '<div class="empty-state">当前本地服务会话还没有生成或导出任务。</div>'; return; }
      root.innerHTML = '<div class="data-list-row task-list-head"><span>任务</span><span>范围 / 阶段</span><span>状态</span><span>最近进展</span><span>操作</span></div>';
      tasks.forEach((task) => {
        const row = document.createElement("div"); row.className = "data-list-row task-row";
        const title = document.createElement("div"); title.className = "task-name";
        title.innerHTML = `<strong>${task.kind === "generation" ? "候选生成" : "验证导出"}</strong><small>${escapeHtml(task.id.replace(/^(job|export)_/, "#").slice(0, 14))}</small>`;
        const scope = document.createElement("span");
        const readyWorkItems = Number(task.result_summary?.success_count || 0) + Number(task.result_summary?.partial_success_count || 0);
        scope.textContent = task.kind === "generation"
          ? `${readyWorkItems}/${task.work_item_count || 0} 份已有候选`
          : task.status === "completed"
            ? `已导出 ${task.result?.outputs?.length || task.progress?.total || 0} 个模板`
            : exportPhase(task).copy;
        const status = document.createElement("span"); status.className = `status-chip task-status ${["completed"].includes(task.status) ? "success" : ["failed", "cancelled"].includes(task.status) ? "danger" : ""}`; status.textContent = statusLabel(task.status);
        const date = document.createElement("span"); date.textContent = formatDate(task.last_progress_at || task.completed_at || task.started_at || task.created_at);
        const actions = document.createElement("div"); actions.className = "task-row-actions";
        if (task.kind === "generation") {
          const open = document.createElement("button"); open.type = "button"; open.className = "text-button"; open.dataset.loadJob = task.job_id; open.textContent = "查看任务与候选"; actions.append(open);
        } else {
          const open = document.createElement("button"); open.type = "button"; open.className = "text-button"; open.dataset.loadExport = task.export_id; open.dataset.jobId = task.job_id; open.textContent = "查看"; actions.append(open);
        }
        if (task.actions?.can_pause) actions.insertAdjacentHTML("beforeend", `<button type="button" class="text-button" data-task-action="pause" data-task-kind="${task.kind}" data-task-id="${escapeHtml(task.id)}">暂停</button>`);
        if (task.actions?.can_resume) actions.insertAdjacentHTML("beforeend", `<button type="button" class="text-button" data-task-action="resume" data-task-kind="${task.kind}" data-task-id="${escapeHtml(task.id)}">继续</button>`);
        if (task.actions?.can_cancel) actions.insertAdjacentHTML("beforeend", `<button type="button" class="text-button danger-text" data-task-action="cancel" data-task-kind="${task.kind}" data-task-id="${escapeHtml(task.id)}">终止</button>`);
        const hasBudgetFailure = task.kind === "generation" && (task?.result?.items || []).some(isBudgetFailure);
        const canContinueSearch = task.kind === "generation" && jobCanContinueSearch(task, hasBudgetFailure ? task.result.items : []);
        if (canContinueSearch) {
          actions.insertAdjacentHTML("beforeend", `<button type="button" class="text-button" data-task-action="resume_unfinished" data-task-kind="${task.kind}" data-task-id="${escapeHtml(task.id)}">继续搜索未完成项</button>`);
        } else if (task.actions?.can_retry && (task.kind !== "generation" || taskHasTransientFailure(task))) {
          const action = "retry";
          const label = task.kind === "generation" ? "继续搜索未完成项" : "重试导出";
          actions.insertAdjacentHTML("beforeend", `<button type="button" class="text-button" data-task-action="${action}" data-task-kind="${task.kind}" data-task-id="${escapeHtml(task.id)}">${label}</button>`);
        }
        row.append(title, scope, status, date, actions); root.append(row);
      });
      syncTaskControlButtons();
    } catch (error) { root.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
  }
  async function loadJob(jobId) {
    state.sourcePreviewToken += 1;
    state.taskSourceItem = null;
    $("#task-source-preview")?.close();
    const loadToken = ++state.jobLoadToken;
    invalidateAdjustmentRun();
    try {
      resetExportViewContext();
      const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
      if (loadToken !== state.jobLoadToken) return null;
      if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
      const adjustmentDialog = $("#template-adjustment-dialog");
      if (adjustmentDialog?.open) adjustmentDialog.close("context_changed");
      else if (state.adjustmentDialog) restoreAdjustmentForm();
      // Candidate-page drafts intentionally belong to one loaded job only.
      // Never let a same-named template in another history record inherit them.
      state.stagedTemplateAdjustments = new Map();
      state.runningAdjustmentKeys = new Set();
      state.jobId = jobId; state.jobSnapshot = job; state.jobStartedAt = job.started_at || job.created_at; state.isRecoveredJob = true; state.jobRecommendationPollAttempts = 0;
      state.pendingUsePartialReview = false; state.lastParameterSuggestionUndo = null;
      state.jobResult = null;
      state.jobResult = availableResultForJob(job); state.selectedCandidates = selectionsForJob(jobId);
      state.candidateOverviewOpened = false;
      if (state.jobResult?.batch_seed !== undefined) {
        state.seed = String(state.jobResult.batch_seed); state.seedWasAutoResolved = true; $("#batch-seed").value = state.seed;
      }
      state.completedSteps = new Set([1, 2]);
      state.compareIds.clear(); state.activeWorkIndex = 0; state.unlockedFlow = 3; navigate("create"); setFlow(3);
      renderGenerationProgress(job);
      const activeJob = ["queued", "running", "pause_requested", "paused", "cancel_requested"].includes(job.status);
      const incompleteCandidateCount = successfulWorkItems().filter(itemNeedsMoreCandidates).length;
      const offerTerminalChoice = ["cancelled", "failed"].includes(job.status)
        || (job.status === "partial" && incompleteCandidateCount > 0);
      if (state.jobResult && successfulWorkItems().length && !activeJob && !offerTerminalChoice) {
        initializeDefaultSelections(); state.completedSteps.add(3); if (job.exported) state.completedSteps.add(4); unlockFlow(4);
        $("#generation-state").classList.add("is-hidden"); $("#candidate-workspace").classList.remove("is-hidden"); renderCandidateWorkspace();
      } else {
        $("#generation-state").classList.remove("is-hidden"); $("#candidate-workspace").classList.add("is-hidden");
      }
      if (activeJob || jobRecommendationsPending(job)) pollJob(jobId);
      return job;
    } catch (error) { toast(error.message, "error"); return null; }
  }
  async function loadExportRecord(exportId, jobId) {
    try {
      const job = await loadJob(jobId); if (!job) return;
      const exportLoadToken = ++state.exportLoadToken;
      const record = await api(`/exports/${encodeURIComponent(exportId)}`);
      if (state.exportLoadToken !== exportLoadToken || state.jobId !== jobId || record.job_id !== jobId || record.export_id !== exportId) return;
      if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
      state.exportId = exportId; state.exportSnapshot = record;
      state.exportStartedAt = record.started_at || record.created_at || new Date().toISOString();
      state.unlockedFlow = 4; renderExportSelection(record.locked_selections); setFlow(4);
      if (record.status === "completed") {
        showCompletedExport(record);
      } else {
        $("#export-success").classList.add("is-hidden"); $("#export-progress").classList.remove("is-hidden"); renderExportProgress(record);
        if (["queued", "running", "pause_requested", "paused", "cancel_requested", "committing"].includes(record.status)) pollExport(exportId);
      }
    } catch (error) { toast(error.message, "error"); }
  }
  function syncTaskControlButtons() {
    document.querySelectorAll("[data-task-action]").forEach(button => {
      const pending = state.taskControlsPending.has(`${button.dataset.taskKind}:${button.dataset.taskId}`);
      button.disabled = pending;
      button.setAttribute("aria-busy", String(pending));
    });
  }
  async function controlTask(kind, id, action) {
    const key = `${kind}:${id}`;
    if (state.taskControlsPending.has(key) || (kind === "generation" && state.jobId === id && state.adjustmentRunPending)) return;
    state.taskControlsPending.add(key);
    syncTaskControlButtons();
    try {
      const base = kind === "export" ? `/exports/${encodeURIComponent(id)}` : `/jobs/${encodeURIComponent(id)}`;
      if (kind === "generation" && ["resume_unfinished", "continue_search"].includes(action)) {
        const current = await api(base);
        if (["queued", "running", "paused", "pause_requested", "cancel_requested"].includes(current.status)) {
          await refreshTasks();
          toast("任务已在运行，可点击“查看任务与候选”查看进度。");
          return;
        }
        let completed = false; let lastError = null;
        for (const fallbackAction of ["resume_unfinished", "continue_search", "retry_incomplete", "retry"]) {
          try {
            await api(`${base}/control`, { method: "POST", body: { action: fallbackAction } });
            completed = true;
            break;
          } catch (error) {
            lastError = error;
            if (!isUnsupportedResumeActionError(error)) break;
          }
        }
        if (!completed) throw lastError || new Error("当前任务无法继续运行。");
      } else {
        await api(`${base}/control`, { method: "POST", body: { action } });
      }
      if (kind === "generation" && id === state.jobId) {
        if (state.jobPollTimer) window.clearTimeout(state.jobPollTimer);
        pollJob(id);
      }
      if (kind === "export" && id === state.exportId) {
        if (state.exportPollTimer) window.clearTimeout(state.exportPollTimer);
        pollExport(id);
      }
      await refreshTasks();
    } catch (error) { toast(error.message, "error"); }
    finally { state.taskControlsPending.delete(key); syncTaskControlButtons(); }
  }
  async function refreshReports() {
    const root = $("#report-list");
    try {
      const payload = await api("/reports"); const reports = payload.reports || [];
      if (!reports.length) { root.innerHTML = '<div class="empty-state">输出工作区还没有 JSON、Markdown 或 CSV 验证记录。</div>'; return; }
      root.innerHTML = '<div class="data-list-row"><span>文件</span><span>类型</span><span>大小</span><span>更新时间</span></div>';
      reports.forEach((report) => { const row = document.createElement("div"); row.className = "data-list-row"; [report.relative_path, report.kind.toUpperCase(), `${Math.max(1, Math.round(report.size / 1024))} KB`, formatDate(report.updated_at)].forEach((copy) => { const span = document.createElement("span"); span.textContent = copy; row.append(span); }); root.append(row); });
    } catch (error) { root.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
  }

  async function chooseDirectory(purpose) {
    const payload = await api("/dialog/directory", { method: "POST", body: { purpose } });
    return payload.cancelled ? null : payload.path;
  }
  async function saveProfilePart(part, body) {
    const payload = await api(`/profile/${part}`, { method: "POST", body }); state.profile = payload; invalidateConsumptionCalibration(); renderProfile(); return payload;
  }
  async function handleChoosePath(purpose, targetInput) {
    try {
      const path = await chooseDirectory(purpose);
      if (path) {
        $(targetInput).value = path;
      }
    }
    catch (error) { toast(error.message, "error"); }
  }
  function setSourceDirectoryMode(mode) {
    const manual = mode === "manual";
    $$('[name="source-directory-mode"]').forEach((input) => { input.checked = input.value === (manual ? "manual" : "picker"); });
    $("#setting-template-path").readOnly = !manual;
    $("#choose-template-directory").hidden = manual;
  }
  function sourceDirectoryError(message = "") {
    const field = $("#setting-template-path");
    const error = $("#source-setting-error");
    error.textContent = message;
    error.hidden = !message;
    if (message) field.setAttribute("aria-invalid", "true");
    else field.removeAttribute("aria-invalid");
  }
  async function chooseTemplateDirectory() {
    const button = $("#choose-template-directory");
    const save = $("#save-source-setting");
    if (button.disabled || save.disabled) return;
    button.disabled = true; save.disabled = true;
    button.setAttribute("aria-busy", "true");
    $$('[name="source-directory-mode"]').forEach((input) => { input.disabled = true; });
    try {
      const path = await chooseDirectory("template");
      if (path) { $("#setting-template-path").value = path; sourceDirectoryError(); }
    } catch (error) {
      sourceDirectoryError(/timed out|timeout/i.test(error.message)
        ? "目录选择超时，请重试或切换为手动输入路径。" : error.message);
    }
    finally {
      button.disabled = false; save.disabled = false;
      button.removeAttribute("aria-busy");
      $$('[name="source-directory-mode"]').forEach((input) => { input.disabled = false; });
    }
  }
  async function saveSourceSetting() {
    const button = $("#save-source-setting");
    if (button.disabled) return;
    const field = $("#setting-template-path");
    const template = field.value.trim().replace(/[\\/]+$/, "");
    sourceDirectoryError();
    if (!template || !/^(?:\/|~\/|[A-Za-z]:[\\/]|\\\\)/.test(template)) {
      sourceDirectoryError("请选择模板目录，或手动输入完整的绝对路径。");
      setSourceDirectoryMode("manual"); field.focus(); return;
    }
    button.disabled = true; button.setAttribute("aria-busy", "true");
    field.disabled = true;
    $$('[name="source-directory-mode"]').forEach((input) => { input.disabled = true; });
    try {
      const profile = state.profile?.profile || {};
      // Keep the existing trusted repository boundary when resaving a legacy profile.
      const unchanged = template === (profile.template_dir || "").replace(/[\\/]+$/, "");
      const root = unchanged && profile.tile_repo_root ? profile.tile_repo_root : template;
      await saveProfilePart("source", { tile_repo_root: root, template_dir: template });
      toast("模板目录已保存，源模板保持只读。");
    } catch (error) {
      sourceDirectoryError(error.message);
    } finally {
      button.disabled = false; button.removeAttribute("aria-busy"); field.disabled = false;
      $$('[name="source-directory-mode"]').forEach((input) => { input.disabled = false; });
      if (!$("#source-setting-error").hidden) field.focus();
    }
  }
  async function saveOutputSetting() {
    try { await saveProfilePart("output", { output_root: $("#setting-output-path").value.trim() }); toast("输出工作区已保存。"); }
    catch (error) { toast(error.message, "error"); }
  }
  async function saveUlinkSetting() {
    try { await saveProfilePart("ulinkconfig", { ulinkconfig_root: $("#setting-ulink-path").value.trim() }); await loadConfigSets(); toast("ulinkconfig 配置目录已保存。"); }
    catch (error) { toast(error.message, "error"); }
  }

  function switchCalibrationSource(source) {
    state.calibrationSource = source;
    $$('[data-calibration-source]').forEach((button) => {
      const selected = button.dataset.calibrationSource === source;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    $$('[data-calibration-source-panel]').forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.calibrationSourcePanel === source);
    });
    state.calibrationResolveToken += 1;
    state.calibrationSelectionId = null;
    state.calibrationResolvedItems = [];
    state.calibrationNameMatchReady = false;
    state.calibrationNameMatchToken += 1;
    $("#resolved-calibration-templates")?.replaceChildren();
    resetConsumptionCalibration({ clearFile: false });
    updateCalibrationResolveState();
  }
  async function prepareConsumptionView() {
    renderProfile();
    if (state.recentExport) {
      $("#calibration-recent-export-tab").disabled = false;
      const names = state.recentExport.result.outputs?.map((item) => item.output_template_id).join("、") || "刚才导出的模板";
      announce("#calibration-recent-export-copy", `可沿用：${names}`);
    }
    if (!state.recentExport && state.calibrationSource === "recent_export") switchCalibrationSource("output_ids");
    await loadConfigSets();
    updateCalibrationResolveState();
    updateConsumptionCalibrationActionState();
  }
  function revealConsumptionCalibrationStage(stage) {
    const selector = stage === "result" ? "#consumption-calibration-result" : stage === "progress" ? "#consumption-calibration-progress" : "#run-consumption-calibration";
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    window.requestAnimationFrame(() => $(selector)?.scrollIntoView({ behavior, block: stage === "result" ? "start" : "center" }));
  }
  async function resolveCalibrationTemplates() {
    const button = $("#resolve-calibration-templates");
    const source = state.calibrationSource;
    const recentContext = parameterRequestContext(source);
    const operationToken = ++state.calibrationResolveToken;
    try {
      if (!state.profile?.profile?.ulinkconfig_root) throw new Error("请先在设置中选择 ulinkconfig 目录。");
      setBusy(button, true, "正在核验…");
      const body = { source };
      if (source === "output_ids") {
        body.template_ids = uniqueLines($("#calibration-template-ids").value);
        if (!body.template_ids.length) throw new Error("请至少输入一个新模板 ID。");
        if (!body.template_ids.every(isValidTemplateId)) throw new Error("模板 ID 只能包含英文字母、数字、下划线和连字符，且长度不超过 180 个字符。");
      }
      if (source === "files") body.files = await filesToPayload(state.calibrationFiles);
      if (source === "recent_export") Object.assign(body, recentExportResolveFields(recentContext));
      const payload = await api("/parameters/resolve", { method: "POST", body });
      if (operationToken !== state.calibrationResolveToken || source !== state.calibrationSource) return;
      state.calibrationSelectionId = payload.selection_id;
      resetConsumptionCalibration({ clearFile: false });
      state.calibrationResolvedItems = payload.items || [];
      state.calibrationNameMatchReady = false;
      renderCalibrationNameMatches();
      await refreshCalibrationNameMatches();
      if (operationToken !== state.calibrationResolveToken || source !== state.calibrationSource) return;
      updateConsumptionCalibrationActionState();
      toast(`已确认 ${payload.items?.length || 0} 个校准模板。`);
      revealConsumptionCalibrationStage("action");
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); updateCalibrationResolveState(); }
  }

  function renderCalibrationNameMatches(items = []) {
    const matches = new Map(items.map(item => [item.output_template_id, item]));
    const root = $("#resolved-calibration-templates");
    root.replaceChildren();
    state.calibrationResolvedItems.forEach(item => {
      const match = matches.get(item.output_template_id);
      const row = document.createElement("div"); row.className = "resolved-template-row";
      const name = document.createElement("span"); name.textContent = item.output_template_id;
      name.title = `加工源模板：${item.source_template_id}`;
      const status = document.createElement("span");
      status.textContent = match?.matched_template_id ? `CSV：${match.matched_template_id}` : match?.status === "ambiguous" ? `多个匹配：${match.matches.join(" / ")}` : match ? "未匹配" : "上传 CSV 后自动匹配";
      if (match?.message) status.title = match.message;
      row.append(name, status); root.append(row);
    });
  }
  function calibrationNameMatchContext() {
    const file = state.consumptionHistoryFile;
    return JSON.stringify([state.calibrationSelectionId, file?.name, file?.size, file?.lastModified, $("#consumption-template-prefix").value.trim(), $("#consumption-template-suffix").value.trim()]);
  }
  function renderCalibrationMatchStatus(kind, title, message = "", issues = []) {
    const root = $("#calibration-name-match-status");
    root.className = `calibration-match-status is-${kind}`;
    root.replaceChildren();
    const heading = document.createElement("strong"); heading.textContent = title;
    root.append(heading);
    if (message) { const copy = document.createElement("p"); copy.textContent = message; root.append(copy); }
    if (issues.length) {
      const list = document.createElement("ul");
      issues.forEach(item => {
        const row = document.createElement("li");
        const name = document.createElement("strong"); name.textContent = item.output_template_id || item.source_template_id || "未命名模板";
        const reason = document.createElement("span"); reason.textContent = item.message || "CSV 中未找到唯一对应的模板";
        row.append(name, reason); list.append(row);
      });
      const action = document.createElement("button"); action.type = "button"; action.className = "secondary-button";
      action.textContent = "检查模板名前后缀";
      action.addEventListener("click", () => $("#consumption-template-prefix").focus());
      root.append(list, action);
    }
  }
  async function refreshCalibrationNameMatches() {
    const token = ++state.calibrationNameMatchToken;
    const context = calibrationNameMatchContext();
    const current = () => token === state.calibrationNameMatchToken && context === calibrationNameMatchContext();
    state.calibrationNameMatchReady = false;
    updateConsumptionCalibrationActionState();
    if (!state.calibrationSelectionId || !state.consumptionHistoryFile) {
      renderCalibrationMatchStatus("idle", "上传 CSV 并确认模板后自动匹配，无需填写前后缀。");
      renderCalibrationNameMatches();
      return;
    }
    renderCalibrationMatchStatus("loading", "正在匹配 CSV 中的模板…");
    try {
      const body = {
        selection_id: state.calibrationSelectionId,
        history_file: await fileToBase64Payload(state.consumptionHistoryFile, {suffix:".csv", maxBytes:24*1024*1024, label:"历史副本数据"}),
        template_name_prefix: $("#consumption-template-prefix").value.trim(),
        template_name_suffix: $("#consumption-template-suffix").value.trim(),
      };
      if (!current()) return;
      const result = await api("/parameters/match-consumption-templates", {method:"POST", body});
      if (!current()) return;
      state.calibrationNameMatchReady = result.total > 0 && result.matched_count === result.total;
      renderCalibrationNameMatches(result.items || []);
      const issues = (result.items || []).filter(item => item.status !== "matched");
      if (state.calibrationNameMatchReady) {
        renderCalibrationMatchStatus("success", `已匹配 ${result.matched_count} / ${result.total} 个模板，可直接生成草案。`);
      } else {
        renderCalibrationMatchStatus("warning", `${issues.length} 个模板尚未匹配到唯一历史数据`,
          `已匹配 ${result.matched_count} / ${result.total} 个模板，暂不能生成草案。请检查上传的 CSV 是否包含以下模板；名称不一致时可调整前后缀。`, issues);
      }
    } catch (error) {
      if (!current()) return;
      renderCalibrationMatchStatus("error", "历史数据匹配未完成", error.status === 404 ? "模板匹配服务尚未更新，请先导出未保存的候选，再重启工具。" : error.message);
    } finally { if (current()) updateConsumptionCalibrationActionState(); }
  }
  function scheduleCalibrationNameMatches() {
    state.calibrationNameMatchToken += 1;
    state.calibrationNameMatchReady = false;
    if (state.calibrationNameMatchTimer) window.clearTimeout(state.calibrationNameMatchTimer);
    updateConsumptionCalibrationActionState();
    renderCalibrationMatchStatus("loading", "正在匹配 CSV 中的模板…");
    state.calibrationNameMatchTimer = window.setTimeout(refreshCalibrationNameMatches, 300);
  }

  function resetConsumptionCalibration({ clearFile = true } = {}) {
    state.consumptionCalibrationToken += 1;
    state.consumptionCalibrationResult = null;
    state.consumptionCalibrationFingerprint = null;
    if (clearFile) {
      state.consumptionHistoryFile = null;
      state.consumptionConfigLevelFile = null;
      state.consumptionFirstDupOverrides = {};
      const input = $("#consumption-history-file");
      if (input) input.value = "";
      const configInput = $("#consumption-config-level-file");
      if (configInput) configInput.value = "";
      announce("#consumption-history-file-copy", "包含模板、副本参数、关卡号、进入人数与人均消耗；策略组 ID 非必需");
      announce("#consumption-config-level-file-copy", "默认读取当前 ulinkconfig；仅需覆盖本次计算时上传");
    }
    $("#consumption-calibration-result")?.classList.add("is-hidden");
    $("#consumption-calibration-templates")?.replaceChildren();
    $("#consumption-calibration-warnings")?.replaceChildren();
    $("#consumption-calibration-progress")?.classList.add("is-hidden");
    if ($("#write-calibration-config")) $("#write-calibration-config").disabled = true;
    updateConsumptionCalibrationActionState();
  }
  function updateConsumptionCalibrationActionState() {
    const enabled = Boolean(
      state.calibrationSelectionId
      && state.consumptionHistoryFile
      && state.profile?.profile?.ulinkconfig_root
      && state.calibrationNameMatchReady !== false
    );
    const button = $("#run-consumption-calibration");
    setPrimaryActionAvailability(button, enabled && button?.dataset.isBusy !== "true");
  }
  async function fileToBase64Payload(file, { suffix, maxBytes, label }) {
    if (!file || !String(file.name || "").toLowerCase().endsWith(suffix)) throw new Error(`${label}必须是 ${suffix.toUpperCase()} 文件。`);
    if (!(Number(file.size) > 0)) throw new Error(`${label}不能为空。`);
    if (Number(file.size) > maxBytes) throw new Error(`${label}超过 ${Math.round(maxBytes / 1024 / 1024)} MB 安全上限。`);
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    return { name: file.name, data_base64: btoa(binary) };
  }
  function syncConsumptionTargetMode() {
    const manual = $("#consumption-target-mode")?.value === "manual";
    $("#consumption-manual-targets")?.classList.toggle("is-hidden", !manual);
  }
  function selectedConsumptionStrategy() {
    const value = $("#consumption-strategy-source")?.value || "source";
    if (value === "1003" || value === "1004") return { mode: "custom", id: value, label: `指定策略 ${value}` };
    return { mode: "source", id: "", label: "结构策略跟随原关卡" };
  }
  function syncConsumptionSeedPolicy() {
    const seedPolicy = $("#consumption-seed-policy")?.value || "joint";
    const considersStrategy = $("#consumption-strategy-policy")?.value !== "ignore";
    const strategy = selectedConsumptionStrategy();
    const strategyCopy = considersStrategy ? strategy.label : "忽略结构降难策略";
    announce(
      "#consumption-seed-note",
      seedPolicy === "joint"
        ? `${strategyCopy}；联合搜索每档 A/B 与 Seed 0–31，Lucky 保留。`
        : seedPolicy === "observed"
          ? `${strategyCopy}；在历史已观测 Seed 与原 Seed 中搜索主推荐，Lucky 保留。`
          : `${strategyCopy}；保留原 Seed 与 Lucky，作为快速基线。`,
    );
  }
  function consumptionCalibrationContext() {
    const targetOverrides = {};
    if ($("#consumption-target-mode")?.value === "manual") {
      $$('[data-consumption-target]').forEach((input) => {
        const raw = input.value.trim();
        if (raw) targetOverrides[input.dataset.consumptionTarget] = raw;
      });
    }
    const file = state.consumptionHistoryFile;
    const configFile = state.consumptionConfigLevelFile;
    return {
      selectionId: state.calibrationSelectionId || "",
      configSetId: state.defaultConfigSetId || "",
      profile: {
        id: state.profile?.profile?.profile_id || "",
        source: state.profile?.profile?.template_dir || "",
        tileRoot: state.profile?.profile?.tile_repo_root || "",
        outputRoot: state.profile?.profile?.output_root || "",
        ulinkconfigRoot: state.profile?.profile?.ulinkconfig_root || "",
      },
      historyFile: file ? [String(file.name || ""), Number(file.size || 0), Number(file.lastModified || 0)] : null,
      configLevelFile: configFile ? [String(configFile.name || ""), Number(configFile.size || 0), Number(configFile.lastModified || 0)] : null,
      targetMode: $("#consumption-target-mode")?.value || "source",
      targetOverrides,
      seedPolicy: $("#consumption-seed-policy")?.value || "preserve",
      autoFirstDuplicateMax: Boolean($("#consumption-auto-first-max")?.checked),
      strategyGroupId: selectedConsumptionStrategy().id,
      strategyGroupMode: selectedConsumptionStrategy().mode,
      strategyPolicy: $("#consumption-strategy-policy")?.value || "consider",
      templatePrefix: $("#consumption-template-prefix")?.value.trim() || "",
      templateSuffix: $("#consumption-template-suffix")?.value.trim() || "",
      firstDuplicateOverrides: state.consumptionFirstDupOverrides,
    };
  }
  function consumptionCalibrationFingerprint() {
    return JSON.stringify(consumptionCalibrationContext());
  }
  function invalidateConsumptionCalibration() {
    state.consumptionFirstDupOverrides = {};
    resetConsumptionCalibration({ clearFile: false, hidePanel: false });
  }
  function formatCalibrationNumber(value, digits = 2) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "—";
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(numeric);
  }
  function calibrationSeedSimulationCopy(seedSimulation) {
    const mode = String(seedSimulation?.mode || "");
    if (mode === "current_local_config_simulation") return "当前本地配置仿真";
    if (mode === "synthetic") return "使用简化模拟";
    return "仿真状态未返回";
  }
  function calibrationEvidenceCopy(value, seedSimulation) {
    if (seedSimulation?.mode === "synthetic") return "仅简化模拟";
    return ({ association_moderate: "历史样本较充足", association_weak: "历史样本较少", synthetic_only: "仅模拟结果" })[value] || "待确认";
  }
  function calibrationAssociationCopy(association) {
    if (!association || association.status !== "association_only") return "暂无相似样本";
    const range = Array.isArray(association.range) ? association.range : [];
    return `${formatCalibrationNumber(association.point)}·区间 ${formatCalibrationNumber(range[0])}–${formatCalibrationNumber(range[1])}`;
  }
  function calibrationExpectedSourceCopy(expected) {
    const source = String(expected?.source || "");
    const correction = expected?.structure_correction || {};
    if (source === "target_unavailable") return "缺少历史目标";
    if (calibrationIsTransferEstimate(expected) && correction.applied === true) return "跨关段预估";
    if (source === "source_target_empirical_structure_correction" || correction.applied === true) return "按已有测试数据估算";
    if (source === "source_target_conservative_baseline" || correction.in_scope === false) return "仅保留原目标作参考";
    return "估算结果，仅供参考";
  }
  function calibrationHistoryContextCopy(association) {
    const count = Number(association?.neighbour_count || 0);
    if (!association || association.status !== "association_only") return "相似历史样本：暂无";
    return `相似历史样本：${calibrationAssociationCopy(association)} · ${formatCalibrationNumber(count, 0)} 个样本`;
  }
  function calibrationHasPrediction(expected) {
    const correction = expected?.structure_correction || {};
    return typeof expected?.value === "number" && Number.isFinite(expected.value) && expected.value >= 0 && expected.source !== "target_unavailable" && expected.source !== "source_target_conservative_baseline" && expected.estimate_mode !== "unavailable" && correction.applied !== false && (correction.in_scope !== false || correction.applied === true);
  }
  function calibrationIsTransferEstimate(expected) {
    return (expected?.estimate_mode || expected?.structure_correction?.estimate_mode) === "transfer_estimate";
  }
  function calibrationExpectedConsumptionCopy(expected) {
    if (!calibrationHasPrediction(expected)) return "暂无法预估";
    if (expected?.value === null || expected?.value === undefined || expected?.value === "") return "无可用拟合";
    const value = Number(expected?.value);
    if (!Number.isFinite(value)) return "无可用拟合";
    const difference = expected?.difference == null ? NaN : Number(expected.difference);
    const differenceCopy = Number.isFinite(difference)
      ? `${difference >= 0 ? "+" : ""}${formatCalibrationNumber(difference)}`
      : "目标差异不可用";
    return `${formatCalibrationNumber(value)}·与目标 ${differenceCopy}`;
  }
  function dupParameterMax(value) {
    const parts = String(value || "").split("|");
    const a = Number(parts[0]); const b = Number(parts[3]);
    return Number.isFinite(a) && Number.isFinite(b) ? Math.max(a, b) : null;
  }
  function calibrationSyntheticCopy(synthetic) {
    if (typeof synthetic?.distance !== "number" || !Number.isFinite(synthetic.distance)) return "未评估";
    return formatCalibrationNumber(synthetic.distance, 3);
  }
  function calibrationReviewNotice(result) {
    const duplicates = (result.rows || []).flatMap(item => item.duplicates || []);
    const unestimated = duplicates.filter(item => !calibrationHasPrediction(item.expected_consumption));
    const simplified = duplicates.some(item => item.seed_simulation?.mode === "synthetic");
    const transferred = duplicates.some(item => calibrationHasPrediction(item.expected_consumption) && calibrationIsTransferEstimate(item.expected_consumption));
    const assumedAnchor = (result.rows || []).some(item => item.consumption_anchor?.kind === "processed_source_assumed_equal_online") || duplicates.some(item => item.consumption_anchor?.kind === "processed_source_assumed_equal_online");
    const manualTarget = duplicates.some(item => item.target_consumption?.source === "user_override" || item.consumption_anchor?.target_source === "user_override");
    const scope = result.consumption_prediction_model?.scope || result.consumption_model?.scope || {};
    const historyRange = result.coverage?.history_level_range;
    const historyCopy = Number.isInteger(historyRange?.start) && Number.isInteger(historyRange?.end)
      ? `已读取 CSV 第 ${historyRange.start}–${historyRange.end} 关；`
      : "";
    const assumptionCopy = assumedAnchor ? `加工源原参数消耗暂按${manualTarget ? "本次目标" : "原线上目标"}计算。` : "";
    if (transferred) return `${historyCopy}使用第 ${scope.level_index_min ?? 200}–${scope.level_index_max ?? 300} 关训练模型进行跨关段预估。${assumptionCopy}结果待后续实测校验。${unestimated.length ? "部分结果缺少必要数据，暂未预估。" : ""}`;
    if (!unestimated.length) return `${assumptionCopy}${simplified ? "部分结果使用简化模拟，请核对本地配置后重算，或人工确认后再采用。" : ""}`;
    const reasons = new Set(unestimated.flatMap(item => item.expected_consumption?.structure_correction?.reasons || []));
    const cause = reasons.has("outside_level_range") ? `内置模型训练来自第 ${scope.level_index_min ?? 200}–${scope.level_index_max ?? 300} 关，旧草案未提供跨关段预估，请重新生成` : reasons.has("outside_structure_count") || reasons.has("unsupported_signature") ? "部分模板的结构缺少可用的消耗估算依据" : reasons.has("outside_strategy_group") ? "所选降难策略缺少对应测试数据" : "部分模板缺少必要的消耗估算数据";
    return `${historyCopy}${assumptionCopy}${cause}。无法预估消耗的结果仅按模拟排序，不代表消耗已匹配，请人工复核后采用。`;
  }
  function calibrationRecommendationCopy(duplicate) {
    if (!calibrationHasPrediction(duplicate.expected_consumption)) {
      const distance = duplicate.synthetic_difficulty?.distance;
      return typeof distance === "number" && Number.isFinite(distance) ? "仅模拟排序" : "待人工确认";
    }
    if (calibrationIsTransferEstimate(duplicate.expected_consumption)) {
      if (duplicate.recommendation?.status === "transfer_estimate_close") return "预估接近";
      if (duplicate.recommendation?.status === "transfer_estimate_far") return "偏差较大";
      return "需确认";
    }
    if (duplicate.recommendation?.is_recommended) return "推荐";
    if (duplicate.recommendation?.status === "best_fit_outside_tolerance") return "偏差较大";
    return "需确认";
  }
  function calibrationSearchBoundsCopy(comparison, hasManualTarget = false) {
    if (comparison?.status !== "different") return "";
    const first = String(comparison.processed_dup?.[0] || "").split("|");
    const hasFirst = first.length === 4 && /^\d+$/.test(first[0]) && /^\d+$/.test(first[3]);
    const firstCopy = hasFirst ? `（首副本 A≤${Number(first[0])}、B≤${Number(first[3])}）` : "";
    const targetCopy = hasManualTarget ? `目标消耗优先取手动值，其余取 ${comparison.online_template_id}` : `目标消耗仍取 ${comparison.online_template_id}`;
    return `${comparison.processed_template_id} 与 ${comparison.online_template_id} 的副本参数不同；按加工源各档 A/B 上限搜索${firstCopy}，${targetCopy}。`;
  }
  function calibrationParameterReference(duplicate) {
    const source = String(duplicate.ranking_reference_dup || duplicate.original_dup || "—");
    const online = duplicate.original_dup && duplicate.original_dup !== source ? String(duplicate.original_dup) : "";
    return {source, online};
  }
  function renderConsumptionCalibrationResult() {
    const result = state.consumptionCalibrationResult;
    if (!result) return;
    const coverage = result.coverage || {};
    const followsSourceStrategy = result.strategy_group_mode === "source";
    const resolvedStrategyIds = result.strategy_group_ids || [];
    const hasResolvedStrategy = followsSourceStrategy
      ? resolvedStrategyIds.length > 0
      : Boolean(String(result.strategy_group_id || "").trim());
    const summary = $("#consumption-calibration-summary");
    summary.replaceChildren();
    [
      [(result.rows || []).some(item => (item.duplicates || []).some(duplicate => calibrationHasPrediction(duplicate.expected_consumption))) ? "消耗预估，仅供参考" : "模拟结果，仅供参考", true],
      [
        followsSourceStrategy
          ? `策略跟随原关卡：${resolvedStrategyIds.join(" / ") || "未读取"}`
          : `指定策略：${result.strategy_group_id || "—"}`,
        !hasResolvedStrategy,
      ],
      [`覆盖 ${coverage.selected_group_covered_duplicate_count || 0}/10 档`, false],
      [`样本 ${formatCalibrationNumber(coverage.selected_group_sample_entrants || 0, 0)} 人次`, false],
      [
        result.seed_search?.mode === "joint_ab_seed_0_31"
          ? "A/B + Seed 0–31 联合搜索"
          : result.seed_search?.mode === "observed_seeds_plus_original" || result.seed_search?.mode === "observed_seed_alternatives"
            ? "已观测 Seed + 原 Seed 搜索"
            : "保留原 Seed",
        false,
      ],
      [result.strategy_group_config?.status === "available" ? "降难规则已读取" : "降难规则需核对", result.strategy_group_config?.status !== "available"],
      [
        result.structure_strategy_replay?.status === "local_sampled_replay"
          ? "已考虑结构降难"
          : result.structure_strategy_replay?.status === "disabled"
            ? "未考虑结构降难"
            : "结构降难回放不可用",
        result.structure_strategy_replay?.status === "unavailable",
      ],
    ].forEach(([copy, warning]) => {
      const chip = document.createElement("span");
      chip.textContent = copy;
      chip.classList.toggle("is-warning", warning);
      summary.append(chip);
    });
    const reviewNotice = $("#calibration-review-notice");
    reviewNotice.textContent = calibrationReviewNotice(result);
    reviewNotice.classList.toggle("is-hidden", !reviewNotice.textContent);
    const templates = $("#consumption-calibration-templates");
    templates.replaceChildren();
    (result.rows || []).forEach((item) => {
      const section = document.createElement("section");
      section.className = "calibration-template-result";
      section.classList.toggle("has-review-items", !item.recommendation_summary?.all_recommended);
      const heading = document.createElement("div");
      heading.className = "calibration-template-heading";
      const titleBox = document.createElement("div");
      titleBox.className = "calibration-template-title";
      const title = document.createElement("strong");
      title.textContent = item.output_template_id;
      title.title = `加工源模板：${item.source_template_id}`;
      const meta = document.createElement("span");
      meta.textContent = `10 档草案${item.canonical_template_id ? ` · 对应 CSV：${item.canonical_template_id}` : ""} · 策略 ${item.strategy_group_id}`;
      titleBox.append(title, meta);
      const first = item.duplicates?.[0] || {};
      const adjustment = item.first_duplicate_adjustment || {};
      const lower = Number(adjustment.lower_bound ?? dupParameterMax(first.fidelity_baseline_dup));
      const upper = Number(adjustment.upper_bound ?? dupParameterMax(first.ranking_reference_dup || first.original_dup));
      const selectedMax = Number(adjustment.requested_max ?? dupParameterMax(first.recommended_dup));
      const control = document.createElement("div");
      control.className = "calibration-recalculate";
      const controlLabel = document.createElement("label");
      controlLabel.textContent = "首副本最高参数";
      const input = document.createElement("input");
      input.type = "number";
      input.step = "1";
      input.min = String(Number.isFinite(lower) ? lower : 0);
      input.max = String(Number.isFinite(upper) ? upper : 99);
      input.value = String(Number.isFinite(selectedMax) ? selectedMax : "");
      input.dataset.calibrationFirstMax = item.output_template_id;
      controlLabel.append(input);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary-button";
      button.dataset.recalculateCalibration = item.output_template_id;
      button.textContent = "重算本关";
      const note = document.createElement("p");
      note.className = "calibration-recalculate-note";
      const automatic = adjustment.automatic_bounds;
      note.textContent = automatic
        ? `按结构块种类自动扣减：A ${automatic.source_a}−${automatic.replaced_a_type_count}=${automatic.a_max}，B ${automatic.source_b}−${automatic.replaced_b_type_count}=${automatic.b_max}；候选可取更低值。`
        : `可选 ${Number.isFinite(lower) ? lower : "—"}–${Number.isFinite(upper) ? upper : "—"}；Seed 0–31 仿真重算`;
      control.append(controlLabel, button, note);
      heading.append(titleBox, control);
      const fallbackReasons = Array.isArray(item.seed_simulation_fallback_reasons)
        ? item.seed_simulation_fallback_reasons.filter(Boolean)
        : [];
      const fallbackSummary = document.createElement("p");
      fallbackSummary.className = `calibration-fallback-summary${fallbackReasons.length ? " is-warning" : ""}`;
      const comparison = item.source_parameter_comparison;
      fallbackSummary.textContent = comparison?.status === "different"
        ? calibrationSearchBoundsCopy(comparison, (item.duplicates || []).some(duplicate => duplicate.target_consumption?.source === "user_override"))
        : fallbackReasons.length ? "无法按完整配置模拟，已改用简化模拟；请核对配置后重算。" : "";
      fallbackSummary.classList.toggle("is-hidden", !fallbackSummary.textContent);
      const wrap = document.createElement("div");
      wrap.className = "calibration-table-wrap";
      const table = document.createElement("table");
      table.className = "calibration-table";
      table.innerHTML = `<thead><tr><th>副本</th><th>源参数</th><th>参考参数</th><th>候选参数</th><th>推荐结论</th><th>本次目标</th><th>预期人均消耗</th><th>仿真难度差</th><th>Seed / Lucky</th><th>Seed 仿真</th><th>降难策略</th><th>证据</th><th>2 个备选</th></tr></thead>`;
      const body = document.createElement("tbody");
      (item.duplicates || []).forEach((duplicate) => {
        const target = duplicate.target_consumption || {};
        const association = duplicate.historical_association || {};
        const seedSimulation = duplicate.seed_simulation || {};
        const recommendation = duplicate.recommendation || {};
        const row = document.createElement("tr");
        row.classList.toggle("is-not-recommended", recommendation.is_recommended === false);
        const hasTarget = target.value !== null && target.value !== undefined && target.value !== "" && Number.isFinite(Number(target.value));
        const targetSource = target.source === "user_override" ? "手动" : target.adjustment ? `沿用副本 ${target.adjustment.previous_duplicate_index} 的目标` : "原模板";
        const targetCopy = hasTarget ? formatCalibrationNumber(target.value) : "无目标数据";
        const targetMeta = hasTarget ? target.adjustment
          ? `原线上 ${formatCalibrationNumber(target.source_observed_value)}；${targetSource}`
          : `${targetSource} · ${target.sample_entrants || 0} 人次` : "历史数据未覆盖该档";
        const seedCopy = duplicate.seed_changed ? "Seed 已变更" : "Seed 保留";
        const luckyCopy = duplicate.luck_preserved ? "Lucky 保留" : "Lucky 需核对";
        const parameterReference = calibrationParameterReference(duplicate);
        const sourceParameterHtml = `<code>${escapeHtml(parameterReference.source)}</code>${parameterReference.online ? `<small>线上 ${escapeHtml(parameterReference.online)}</small>` : ""}`;
        const seedSimulationCopy = calibrationSeedSimulationCopy(seedSimulation);
        const seedSimulationReason = seedSimulation.fallback_reason ? "请核对配置后重算" : "";
        const observed = (duplicate.observed_effective_strategies || []).join(" / ") || "未记录";
        const rawRule = duplicate.configured_strategy_rule?.raw || "未读取";
        const strategyReplay = seedSimulation.structure_strategy || {};
        const replayed = (strategyReplay.effective_strategies || []).join(" / ");
        const replayCopy = strategyReplay.status === "replayed"
          ? `模拟生效：${replayed || "无策略生效"}`
          : strategyReplay.status === "not_applicable"
            ? "模拟生效：本档不应用"
            : strategyReplay.status === "disabled"
              ? "模拟生效：已关闭"
              : "模拟生效：不可用";
        const alternatives = (duplicate.alternatives || []).map((candidate, index) => {
          const kind = candidate.seed_experimental ? "Seed 备选" : "参数备选";
          return `${index + 1}. ${escapeHtml(candidate.dup_para)}<small>${escapeHtml(kind)}</small>`;
        }).join("<br>") || "—";
        const relativeGap = calibrationHasPrediction(duplicate.expected_consumption) && recommendation.relative_gap != null ? Number(recommendation.relative_gap) : NaN;
        const recommendationMeta = Number.isFinite(relativeGap)
          ? `偏差 ${formatCalibrationNumber(relativeGap * 100, 1)}%`
          : "";
        const expectedSource = calibrationHasPrediction(duplicate.expected_consumption) ? calibrationExpectedSourceCopy(duplicate.expected_consumption) : "";
        const historyContext = calibrationHistoryContextCopy(association);
        row.innerHTML = `<td>${escapeHtml(duplicate.duplicate_index)}</td><td>${sourceParameterHtml}</td><td><code>${escapeHtml(duplicate.fidelity_baseline_dup)}</code></td><td><code>${escapeHtml(duplicate.recommended_dup)}</code></td><td><span class="calibration-recommendation ${recommendation.is_recommended ? "is-recommended" : "is-review"}">${escapeHtml(calibrationRecommendationCopy(duplicate))}</span><small>${escapeHtml(recommendationMeta)}</small></td><td>${escapeHtml(targetCopy)}<small>${escapeHtml(targetMeta)}</small></td><td>${escapeHtml(calibrationExpectedConsumptionCopy(duplicate.expected_consumption))}<small>${escapeHtml(expectedSource)}</small><small>${escapeHtml(historyContext)}</small></td><td>${escapeHtml(calibrationSyntheticCopy(duplicate.synthetic_difficulty))}</td><td>${escapeHtml(seedCopy)}<small>${escapeHtml(luckyCopy)}</small></td><td>${escapeHtml(seedSimulationCopy)}<small>${escapeHtml(seedSimulationReason)}</small></td><td><code>${escapeHtml(rawRule)}</code><small>${escapeHtml(replayCopy)}</small><small>线上记录：${escapeHtml(observed)}</small></td><td>${escapeHtml(calibrationEvidenceCopy(duplicate.evidence_level, seedSimulation))}</td><td class="calibration-alternatives">${alternatives}</td>`;
        body.append(row);
      });
      table.append(body); wrap.append(table); section.append(heading, fallbackSummary, wrap); templates.append(section);
    });
    const warnings = $("#consumption-calibration-warnings");
    warnings.replaceChildren();
    const allWarnings = [
      ...(result.warnings || []),
      ...(result.rows || []).flatMap((item) => item.gradient?.warnings || []),
      ...(result.rows || []).flatMap((item) => (item.seed_simulation_fallback_reasons || []).map((reason) => `${item.output_template_id} 的 Seed 牌面已使用简化模拟：${reason}`)),
    ];
    const uniqueWarnings = [...new Set(allWarnings)];
    if (uniqueWarnings.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = `查看运行记录（${uniqueWarnings.length} 条）`;
      details.append(summary);
      uniqueWarnings.forEach(warning => { const p = document.createElement("p"); p.textContent = warning; details.append(p); });
      warnings.append(details);
    }
    $("#consumption-calibration-result").classList.remove("is-hidden");
    $("#write-calibration-config").disabled = !(result.rows || []).length;
  }
  async function consumptionCalibrationRequestBody(extra = {}) {
    const targetOverrides = {};
    if ($("#consumption-target-mode").value === "manual") {
      $$('[data-consumption-target]').forEach((input) => {
        if (!input.value.trim()) return;
        const value = Number(input.value);
        if (!Number.isFinite(value) || value < 0) throw new Error(`Dup ${input.dataset.consumptionTarget} 的目标消耗必须是非负数。`);
        targetOverrides[input.dataset.consumptionTarget] = value;
      });
    }
    const configLevelPayload = state.consumptionConfigLevelFile
      ? await fileToBase64Payload(state.consumptionConfigLevelFile, { suffix: ".csv", maxBytes: 24 * 1024 * 1024, label: "config_level" })
      : null;
    const strategy = selectedConsumptionStrategy();
    return {
      selection_id: state.calibrationSelectionId,
      parameter_strategy: "fidelity",
      ...(state.defaultConfigSetId ? { config_set_id: state.defaultConfigSetId } : {}),
      history_file: await fileToBase64Payload(state.consumptionHistoryFile, { suffix: ".csv", maxBytes: 24 * 1024 * 1024, label: "历史副本数据" }),
      ...(configLevelPayload ? { config_level_file: configLevelPayload } : {}),
      ...(Object.keys(targetOverrides).length ? { target_overrides: targetOverrides } : {}),
      search_observed_seeds: $("#consumption-seed-policy").value === "observed",
      optimize_all_duplicate_seeds: $("#consumption-seed-policy").value === "joint",
      auto_first_duplicate_max: Boolean($("#consumption-auto-first-max")?.checked),
      strategy_group_id: strategy.id,
      strategy_group_mode: strategy.mode,
      consider_structure_strategy: $("#consumption-strategy-policy").value !== "ignore",
      template_name_prefix: $("#consumption-template-prefix").value.trim(),
      template_name_suffix: $("#consumption-template-suffix").value.trim(),
      ...extra,
    };
  }
  function calibrationProgressModel(record) {
    const progress = record.progress || {};
    const total = Math.max(0, Number(progress.duplicate_total_all) || 0);
    const completed = clamp(Number(progress.duplicate_completed) || 0, 0, total);
    return {
      completed, total, percent: total ? completed / total * 100 : 0,
      elapsed: Math.max(0, Number(record.elapsed_s) || 0),
      message: record.status === "failed" ? record.error?.message || "校准未完成，请重试。" : progress.message || "正在准备校准资料…",
      detail: [progress.current_template, progress.duplicate_index ? `副本 ${progress.duplicate_index} / ${progress.duplicate_total || 10}` : ""].filter(Boolean).join(" · "),
      templateCopy: progress.template_total ? `${progress.template_completed || 0} / ${progress.template_total} 个模板` : "准备中",
    };
  }
  function renderConsumptionCalibrationProgress(record) {
    const root = $("#consumption-calibration-progress");
    if (!root) return;
    const model = calibrationProgressModel(record);
    root.classList.remove("is-hidden");
    root.classList.toggle("is-error", record.status === "failed");
    root.setAttribute("role", record.status === "failed" ? "alert" : "status");
    $("#calibration-progress-message").textContent = model.message;
    $("#calibration-progress-count").textContent = `${model.templateCopy} · 已用时 ${formatDuration(model.elapsed * 1000)}`;
    $("#calibration-progress-detail").textContent = [model.detail, model.total ? `${model.completed} / ${model.total} 个副本已处理` : ""].filter(Boolean).join(" · ");
    const bar = $("#calibration-progress-bar");
    if (model.total) bar.value = model.percent;
    else bar.removeAttribute("value");
  }
  function calibrationOperationIsCurrent(operation) {
    return operation.token === state.consumptionCalibrationToken
      && operation.selectionId === state.calibrationSelectionId
      && operation.fingerprint === consumptionCalibrationFingerprint();
  }
  async function requestConsumptionCalibration(body, operation) {
    if (!calibrationOperationIsCurrent(operation)) return null;
    let record;
    try {
      record = await api("/parameters/consumption-calibration-jobs", { method: "POST", body });
    } catch (error) {
      if (error.status === 404) throw new Error("校准服务尚未更新，请关闭启动窗口并重新启动工具，再生成草案。");
      throw error;
    }
    while (calibrationOperationIsCurrent(operation)) {
      renderConsumptionCalibrationProgress(record);
      if (record.status === "completed") {
        if (!record.result) throw new Error("校准任务未返回审阅结果，请重试。");
        return record.result;
      }
      if (record.status === "failed") throw new Error(record.error?.message || "校准任务未完成，请重试。");
      if (!["queued", "running"].includes(record.status) || !record.job_id) throw new Error("校准任务状态无效，请重试。");
      await new Promise(resolve => window.setTimeout(resolve, 700));
      if (!calibrationOperationIsCurrent(operation)) return null;
      record = await api(`/parameters/consumption-calibration-jobs/${encodeURIComponent(record.job_id)}`);
    }
    return null;
  }
  async function runConsumptionCalibration() {
    const button = $("#run-consumption-calibration");
    if (button.dataset.isBusy === "true") return;
    const selectionId = state.calibrationSelectionId;
    const operationToken = ++state.consumptionCalibrationToken;
    const operation = { token: operationToken, selectionId, fingerprint: consumptionCalibrationFingerprint() };
    try {
      if (!selectionId) throw new Error("请先确认要校准的新模板。");
      if (!state.consumptionHistoryFile) throw new Error("请选择历史副本数据 CSV。");
      const requestFingerprint = consumptionCalibrationFingerprint();
      setBusy(button, true, "正在生成审阅草案…");
      state.consumptionCalibrationFingerprint = null;
      $("#consumption-calibration-result").classList.add("is-hidden");
      renderConsumptionCalibrationProgress({ status: "queued", progress: { message: "正在提交校准任务…" } });
      revealConsumptionCalibrationStage("progress");
      const result = await requestConsumptionCalibration(await consumptionCalibrationRequestBody(), operation);
      if (!result) return;
      if (operationToken !== state.consumptionCalibrationToken
        || state.calibrationSelectionId !== selectionId
        || consumptionCalibrationFingerprint() !== requestFingerprint) return;
      state.consumptionCalibrationResult = result;
      state.consumptionCalibrationFingerprint = requestFingerprint;
      renderConsumptionCalibrationResult();
      revealConsumptionCalibrationStage("result");
      toast(`已生成 ${result.rows?.length || 0} 个模板的消耗校准审阅草案。`);
    } catch (error) {
      if (calibrationOperationIsCurrent(operation)) {
        renderConsumptionCalibrationProgress({ status: "failed", error: { message: error.message } });
        toast(error.message, "error");
      }
    }
    finally { setBusy(button, false); updateConsumptionCalibrationActionState(); }
  }
  async function recalculateConsumptionTemplate(outputTemplateId, trigger) {
    const runButton = $("#run-consumption-calibration");
    if (runButton.dataset.isBusy === "true") return;
    const input = $(`[data-calibration-first-max="${CSS.escape(outputTemplateId)}"]`);
    const value = Number(input?.value);
    const minimum = Number(input?.min); const maximum = Number(input?.max);
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      toast(`首副本最高参数必须是 ${minimum}–${maximum} 之间的整数。`, "error");
      input?.focus();
      return;
    }
    const previous = state.consumptionCalibrationResult;
    if (!previous) return;
    const previousFingerprint = state.consumptionCalibrationFingerprint;
    if (previousFingerprint !== consumptionCalibrationFingerprint()) {
      invalidateConsumptionCalibration();
      toast("配置或输入已变化，请先重新生成整批审阅草案。", "error");
      return;
    }
    const operation = { token: ++state.consumptionCalibrationToken, selectionId: state.calibrationSelectionId, fingerprint: consumptionCalibrationFingerprint() };
    try {
      setBusy(trigger, true, "正在重算…");
      setBusy(runButton, true, "正在重算…");
      input.disabled = true;
      state.consumptionCalibrationFingerprint = null;
      renderConsumptionCalibrationProgress({ status: "queued", progress: { message: "正在提交重算任务…", current_template: outputTemplateId } });
      revealConsumptionCalibrationStage("progress");
      const result = await requestConsumptionCalibration(await consumptionCalibrationRequestBody({
          focus_output_template_id: outputTemplateId,
          first_duplicate_max: value,
          simulate_first_duplicate_seeds: true,
          search_observed_seeds: false,
        }), operation);
      if (!result || !calibrationOperationIsCurrent(operation)) return;
      const replacement = result.rows?.[0];
      if (!replacement) throw new Error("本关没有生成可用的重算结果。");
      state.consumptionFirstDupOverrides = {
        ...state.consumptionFirstDupOverrides,
        [outputTemplateId]: value,
      };
      state.consumptionCalibrationResult = {
        ...previous,
        warnings: [...new Set([...(previous.warnings || []), ...(result.warnings || [])])],
        rows: (previous.rows || []).map((item) => (
          item.output_template_id === outputTemplateId ? replacement : item
        )),
      };
      state.consumptionCalibrationFingerprint = consumptionCalibrationFingerprint();
      renderConsumptionCalibrationResult();
      toast(`${outputTemplateId} 已按首副本最高参数 ${value} 重算，已比较 Seed 0–31。`);
    } catch (error) {
      if (calibrationOperationIsCurrent(operation)) {
        renderConsumptionCalibrationProgress({ status: "failed", error: { message: error.message } });
        toast(error.message, "error");
      }
    }
    finally {
      if (state.consumptionCalibrationResult === previous && calibrationOperationIsCurrent(operation)) state.consumptionCalibrationFingerprint = previousFingerprint;
      input.disabled = false;
      setBusy(trigger, false); setBusy(runButton, false); updateConsumptionCalibrationActionState();
    }
  }
  async function writeConsumptionCalibrationConfig() {
    const result = state.consumptionCalibrationResult;
    if (!result?.rows?.length) return;
    if (state.consumptionCalibrationFingerprint !== consumptionCalibrationFingerprint()) {
      invalidateConsumptionCalibration(); toast("校准输入已变化，请重新生成审阅表。", "error"); return;
    }
    const button = $("#write-calibration-config");
    try {
      setBusy(button, true, "正在安全写入…");
      const payload = await api("/parameters/write-calibration-config", {
        method: "POST",
        body: { config_set_id: result.config_set_id || state.defaultConfigSetId, rows: result.rows, ...(result.calibration_context ? { calibration_context: result.calibration_context } : {}) },
      });
      const reviewCount = (result.rows || []).reduce((sum, item) => (
        sum + (item.duplicates || []).filter((duplicate) => duplicate.recommendation?.is_recommended === false).length
      ), 0);
      toast(`${payload.updated_count || 0} 条配置已写入并备份${reviewCount ? `；${reviewCount} 档仍需人工确认` : ""}。`);
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); }
  }
  function downloadCsvFile(headers, rows, filename) {
    const quote = (value) => {
      let text = String(value ?? "");
      if (/^[=+\-@]/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const csv = `\ufeff${[headers, ...rows].map((row) => row.map(quote).join(",")).join("\r\n")}`;
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  }
  function downloadCalibrationReviewCsv() {
    const result = state.consumptionCalibrationResult; if (!result) return;
    if (state.consumptionCalibrationFingerprint !== consumptionCalibrationFingerprint()) {
      invalidateConsumptionCalibration(); toast("校准输入已变化，请重新生成审阅草案。", "error"); return;
    }
    const headers = ["修改前模板ID", "修改后模板ID", "副本", "原DupPara", "参考参数", "候选参数", "推荐结论", "目标偏差比例", "线上目标消耗", "目标来源", "预期人均消耗_方向性", "预期来源", "结构校正是否应用", "与目标差异", "预期区间下界", "预期区间上界", "相似历史样本估计_上下文", "相似历史样本区间下界_上下文", "相似历史样本区间上界_上下文", "相似历史样本样本数_上下文", "相似历史样本样本人次_上下文", "仿真难度距离_诊断", "Seed是否变更", "Lucky是否保留", "Seed仿真状态", "Seed仿真改用其他计算方式的原因", "StrategyGroupId", "策略原始规则", "是否考虑结构策略", "本地回放生效策略", "本地回放失败策略", "已观测生效策略", "证据等级", "备选1", "备选1类型", "备选2", "备选2类型", "人工审阅状态", "结论边界"];
    headers[headers.indexOf("预期人均消耗_方向性")] = "预期人均消耗_估算";
    headers[headers.indexOf("预期区间下界")] = "历史误差参考下界";
    headers[headers.indexOf("预期区间上界")] = "历史误差参考上界";
    headers.push("对应CSV模板ID", "加工源与线上源配置比较", "加工源首副本上限", "搜索源参数", "模拟次级差异_诊断");
    headers.push("预估方式", "消耗模型版本", "模型训练关段", "加工源消耗按本次目标_假设", "预估参照参数", "区间说明");
    headers.push("本次目标消耗", "目标调整原因", "沿用目标副本", "首副本自动上限A", "首副本自动上限B");
    const rows = (result.rows || []).flatMap((item) => (item.duplicates || []).map((duplicate) => {
      const target = duplicate.target_consumption || {}; const association = duplicate.historical_association || {}; const range = association.range || [];
      const expected = duplicate.expected_consumption || {}; const expectedRange = expected.range || []; const correction = expected.structure_correction || {};
      const seedSimulation = duplicate.seed_simulation || {};
      const alternativeType = (candidate) => candidate?.seed_experimental ? "Seed 备选" : candidate?.kind || "参数备选";
      const strategyReplay = seedSimulation.structure_strategy || {};
      const hasPrediction = calibrationHasPrediction(expected);
      const comparison = item.source_parameter_comparison || {};
      const anchor = duplicate.consumption_anchor || item.consumption_anchor || {};
      const trainingScope = expected.training_scope || correction.training_scope || result.consumption_prediction_model?.scope || {};
      const trainingRange = Number.isInteger(trainingScope.level_index_min) && Number.isInteger(trainingScope.level_index_max) ? `${trainingScope.level_index_min}–${trainingScope.level_index_max}` : "";
      const intervalCopy = hasPrediction && expected.interval_kind === "training_residual_reference" ? "历史误差参考，非本关段实测保证" : "";
      const automaticBounds = item.first_duplicate_adjustment?.automatic_bounds || {};
      return [
        item.source_template_id, item.output_template_id, duplicate.duplicate_index, duplicate.original_dup,
        duplicate.fidelity_baseline_dup, duplicate.recommended_dup, calibrationRecommendationCopy(duplicate),
        hasPrediction ? duplicate.recommendation?.relative_gap : "", target.source_observed_value ?? target.value, target.source,
        hasPrediction ? expected.value : "", calibrationExpectedSourceCopy(expected), correction.applied ? "是" : "否",
        hasPrediction ? expected.difference : "", hasPrediction ? expectedRange[0] : "", hasPrediction ? expectedRange[1] : "",
        association.point, range[0], range[1], association.neighbour_count, association.entrant_count,
        duplicate.synthetic_difficulty?.distance, duplicate.seed_changed ? "是" : "否", duplicate.luck_preserved ? "是" : "否",
        calibrationSeedSimulationCopy(seedSimulation), seedSimulation.fallback_reason || "", duplicate.strategy_group_id,
        duplicate.configured_strategy_rule?.raw, result.consider_structure_strategy ? "是" : "否",
        (strategyReplay.effective_strategies || []).join("|"), (strategyReplay.failed_strategies || []).join("|"),
        (duplicate.observed_effective_strategies || []).join("|"), calibrationEvidenceCopy(duplicate.evidence_level, seedSimulation),
        duplicate.alternatives?.[0]?.dup_para || "", alternativeType(duplicate.alternatives?.[0]),
        duplicate.alternatives?.[1]?.dup_para || "", alternativeType(duplicate.alternatives?.[1]),
        duplicate.recommendation?.is_recommended ? "推荐" : "人工确认",
        `${hasPrediction ? "消耗估算" : "仅模拟排序"}；${calibrationExpectedSourceCopy(expected)}；相似历史样本仅供对照；不保证线上精确达成`,
        item.canonical_template_id || "", comparison.status === "different" ? "不同" : comparison.status === "same" ? "一致" : "仅一份配置",
        comparison.first_duplicate_max ?? "", calibrationParameterReference(duplicate).source, duplicate.synthetic_difficulty?.stable_tiebreak ?? "",
        hasPrediction ? calibrationExpectedSourceCopy(expected) : "未预估",
        expected.model_id || correction.model_id || result.consumption_prediction_model?.model_id || "", trainingRange,
        anchor.kind === "processed_source_assumed_equal_online" ? "是" : anchor.kind ? "否" : "",
        anchor.source_dup || calibrationParameterReference(duplicate).source, intervalCopy,
        target.value, target.adjustment ? "线上消耗回升，沿用前一副本目标" : "",
        target.adjustment?.previous_duplicate_index ?? "", automaticBounds.a_max ?? "", automaticBounds.b_max ?? "",
      ];
    }));
    downloadCsvFile(headers, rows, `目标消耗校准审阅_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.csv`);
  }
  function downloadCalibrationDraftCsv() {
    const result = state.consumptionCalibrationResult; if (!result) return;
    if (state.consumptionCalibrationFingerprint !== consumptionCalibrationFingerprint()) {
      invalidateConsumptionCalibration(); toast("校准输入已变化，请重新生成审阅草案。", "error"); return;
    }
    const headers = ["修改后模板ID", "修改前模板ID", ...Array.from({ length: 10 }, (_, index) => `DupPara${index + 1}`), "StrategyGroupId", "结构策略仿真", "Seed方案", "Lucky", "审阅状态", "结论边界"];
    headers.push("对应CSV模板ID", "加工源首副本上限");
    headers.push("预估方式", "消耗模型版本", "模型训练关段", "加工源消耗按本次目标_假设");
    const rows = (result.rows || []).map((item) => {
      const duplicates = item.duplicates || [];
      const predictions = duplicates.map(duplicate => duplicate.expected_consumption).filter(Boolean);
      const expected = predictions.filter(calibrationHasPrediction);
      const modelIds = [...new Set([...predictions.map(value => value.model_id || value.structure_correction?.model_id), result.consumption_prediction_model?.model_id].filter(Boolean))];
      const scope = predictions[0]?.training_scope || predictions[0]?.structure_correction?.training_scope || result.consumption_prediction_model?.scope || {};
      const trainingRange = Number.isInteger(scope.level_index_min) && Number.isInteger(scope.level_index_max) ? `${scope.level_index_min}–${scope.level_index_max}` : "";
      const assumedAnchor = item.consumption_anchor?.kind === "processed_source_assumed_equal_online" || duplicates.some(duplicate => duplicate.consumption_anchor?.kind === "processed_source_assumed_equal_online");
      const estimateMode = expected.some(calibrationIsTransferEstimate) ? "跨关段预估" : expected.length ? "按已有测试数据估算" : "未预估";
      return [
        item.output_template_id, item.source_template_id,
        ...Array.from({ length: 10 }, (_, index) => duplicates[index]?.recommended_dup || ""),
        item.strategy_group_id, result.consider_structure_strategy ? "考虑所选策略" : "忽略结构降难",
        result.seed_search?.mode === "joint_ab_seed_0_31" ? "A/B + Seed 0–31 联合搜索" : item.search_observed_seeds ? "在已观测 Seed 中搜索（含原 Seed）" : "保留原 Seed",
        "保留原值", item.recommendation_summary?.all_recommended ? "全部推荐" : "含人工确认候选",
        "模拟估算；写入前请审阅；不保证线上精确达成", item.canonical_template_id || "",
        item.source_parameter_comparison?.first_duplicate_max ?? "", estimateMode, modelIds.join(" / "), trainingRange,
        assumedAnchor ? "是" : "否",
      ];
    });
    downloadCsvFile(headers, rows, `DupPara校准草案_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.csv`);
  }

  function switchParameterSource(source) {
    state.parameterSource = source;
    $$('[data-param-source]').forEach((button) => button.classList.toggle("is-selected", button.dataset.paramSource === source));
    $$('[data-param-source-panel]').forEach((panel) => panel.classList.toggle("is-active", panel.dataset.paramSourcePanel === source));
    state.parameterResolveToken += 1;
    state.parameterCalculationToken += 1;
    state.unlaunchedLookupToken += 1;
    state.parameterSelectionId = null;
    state.parameterSelectionContext = null;
    $("#calculate-parameters").disabled = true;
    updateParameterResolveState();
  }
  function switchParameterMode(mode) {
    const nextMode = mode === "unlaunched" ? "unlaunched" : "online";
    state.parameterMode = nextMode;
    state.parameterResolveToken += 1;
    state.parameterCalculationToken += 1;
    state.unlaunchedLookupToken += 1;
    $$('[data-parameter-mode]').forEach((button) => {
      const selected = button.dataset.parameterMode === state.parameterMode;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    $$('[data-parameter-mode-panel]').forEach((panel) => {
      const selected = panel.dataset.parameterModePanel === state.parameterMode;
      panel.classList.toggle("is-active", selected);
      panel.hidden = !selected;
    });
    $$('[data-parameter-mode-output]').forEach((output) => {
      output.hidden = output.dataset.parameterModeOutput !== state.parameterMode;
    });
    const isUnlaunched = state.parameterMode === "unlaunched";
    $("#parameter-source-title").textContent = isUnlaunched ? "选择待查询模板" : "选择新模板";
    $("#parameter-source-copy").textContent = isUnlaunched
      ? "先解析导出记录中的原模板 ID，再查询现有参数"
      : "模板 ID 从当前输出目录查找";
    $("#resolve-parameter-templates").textContent = isUnlaunched ? "查询原模板参数" : "确认新模板";
    updateParameterResolveState();
  }
  async function prepareParameterView() {
    renderProfile();
    if (state.recentExport) {
      $("#recent-export-tab").disabled = false;
      const names = state.recentExport.result.outputs?.map((item) => item.output_template_id).join("、") || "刚才导出的模板";
      $("#recent-export-copy").textContent = `可沿用：${names}`;
    }
    if (state.parameterEntryPreferRecent && state.recentExport) switchParameterSource("recent_export");
    else if (!state.recentExport && state.parameterSource === "recent_export") switchParameterSource("output_ids");
    state.parameterEntryPreferRecent = false;
    updateParameterResolveState();
    switchParameterMode(state.parameterMode);
    renderStrategies(); await loadConfigSets();
  }
  function renderStrategies() {
    const root = $("#strategy-options");
    const currentSelection = $('input[name="parameter-strategy"]:checked')?.value || "fidelity";
    root.replaceChildren();
    const availableStrategies = { ...(state.strategies || {}) };
    if (!availableStrategies.fidelity) {
      availableStrategies.fidelity = {
        label: "保真副本参数",
        description: "逐副本保留 Seed / Luck，按实际替换种类扣减 A / B；按原模板实际牌量保留主侧 4、次侧 3 的经验底线，必要时少扣。不是消耗复刻。",
      };
    }
    availableStrategies.fidelity = {
      ...availableStrategies.fidelity,
      description: "逐副本保留 Seed / Luck，按实际替换种类扣减 A / B；按原模板实际牌量保留主侧 4、次侧 3 的经验底线，必要时少扣。不是消耗复刻。",
    };
    const preferredOrder = ["fidelity", "balanced", "flat", "aggressive", "steady", "gentle"];
    const entries = [
      ...preferredOrder.filter((id) => availableStrategies[id]).map((id) => [id, availableStrategies[id]]),
      ...Object.entries(availableStrategies).filter(([id]) => !preferredOrder.includes(id)),
    ];
    const compactStrategyCopy = {
      fidelity: "保留各副本 Seed / Luck 与原梯度",
      balanced: "A 从副本 2、B 从副本 3 开始递减",
      flat: "副本 2–10 沿用副本 1",
      aggressive: "副本 3 / 4 起，每档减 1",
      steady: "副本 3 / 4 起，每 2 档减 1",
      gentle: "副本 3 / 4 起，每 3 档减 1",
    };
    const preserveGroup = document.createElement("section"); preserveGroup.className = "strategy-group preserve-strategy-group";
    const preserveHeading = document.createElement("div"); preserveHeading.className = "strategy-group-heading";
    preserveHeading.innerHTML = "<strong>保真原副本参数</strong><span>默认·不重做副本梯度</span>";
    const preserveGrid = document.createElement("div"); preserveGrid.className = "strategy-options";
    preserveGroup.append(preserveHeading, preserveGrid);
    const rebuildGroup = document.createElement("section"); rebuildGroup.className = "strategy-group rebuild-strategy-group";
    const rebuildHeading = document.createElement("div"); rebuildHeading.className = "strategy-group-heading";
    rebuildHeading.innerHTML = "<strong>重做副本梯度</strong><span>从 DupPara1 重建后续副本</span>";
    const rebuildGrid = document.createElement("div"); rebuildGrid.className = "strategy-options";
    rebuildGroup.append(rebuildHeading, rebuildGrid);
    root.append(preserveGroup, rebuildGroup);
    entries.forEach(([id, detail], index) => {
      const label = document.createElement("label"); label.className = "strategy-option";
      const input = document.createElement("input"); input.type = "radio"; input.name = "parameter-strategy"; input.value = id;
      input.checked = id === currentSelection || (!availableStrategies[currentSelection] && ((availableStrategies.fidelity && id === "fidelity") || index === 0));
      const span = document.createElement("span"); const strong = document.createElement("b"); strong.textContent = detail.label;
      if (id === "fidelity") { const badge = document.createElement("em"); badge.textContent = "默认"; strong.append(badge); }
      const small = document.createElement("small"); small.textContent = compactStrategyCopy[id] || detail.description;
      span.title = detail.description || small.textContent;
      span.append(strong, small); label.append(input, span);
      (id === "fidelity" ? preserveGrid : rebuildGrid).append(label);
    });
  }
  function populateConfigSetSelect(select, values, emptyCopy) {
    select.replaceChildren();
    if (!values.length) {
      const option = document.createElement("option"); option.value = ""; option.textContent = emptyCopy; select.append(option); return;
    }
    values.forEach((item) => { const option = document.createElement("option"); option.value = item.id; option.textContent = `${item.label} · ${item.relative_path}`; select.append(option); });
    const preferred = values.find((item) => /(^|\/)tile01_ios_config$/.test(item.id)) || values[0];
    if (preferred) select.value = preferred.id;
  }
  async function loadConfigSets() {
    const onlineSelect = $("#config-set-select");
    if (!state.profile?.profile?.ulinkconfig_root) {
      populateConfigSetSelect(onlineSelect, [], "请先配置目录");
      $("#calculate-parameters").disabled = true;
      updateParameterResolveState();
      updateConsumptionCalibrationActionState();
      return;
    }
    try {
      const payload = await api("/parameters/config-sets");
      populateConfigSetSelect(onlineSelect, payload.config_sets || [], "未找到成对配置表");
      state.defaultConfigSetId = onlineSelect.value || "";
      $("#calculate-parameters").disabled = !state.parameterSelectionId || !onlineSelect.value;
      updateParameterResolveState();
      updateConsumptionCalibrationActionState();
    } catch (error) {
      state.defaultConfigSetId = "";
      populateConfigSetSelect(onlineSelect, [], "配置表读取失败");
      updateParameterResolveState(); updateConsumptionCalibrationActionState(); toast(error.message, "error");
    }
  }
  async function chooseUlinkFromParameter() {
    try {
      const path = await chooseDirectory("ulinkconfig"); if (!path) return;
      $("#setting-ulink-path").value = path; await saveProfilePart("ulinkconfig", { ulinkconfig_root: path }); await loadConfigSets(); toast("ulinkconfig 路径已保存，之后会自动沿用。");
    } catch (error) { toast(error.message, "error"); }
  }
  async function lookupUnlaunchedParameters(selectionId, requestContext = null) {
    const token = ++state.unlaunchedLookupToken;
    const result = await api("/parameters/unlaunched-index", { method: "POST", body: {
      selection_id: selectionId,
    } });
    if (token !== state.unlaunchedLookupToken
      || state.parameterSelectionId !== selectionId
      || (requestContext && !parameterRequestContextIsCurrent(requestContext))) return null;
    state.unlaunchedParameterResult = result;
    renderUnlaunchedParameterResult();
    return result;
  }
  function renderUnlaunchedParameterResult() {
    const result = state.unlaunchedParameterResult; if (!result) return;
    const root = $("#unlaunched-parameter-results"); root.classList.remove("is-hidden");
    const duplicateCount = result.duplicate_template_ids?.length || 0;
    $("#unlaunched-result-summary").textContent = `${result.rows?.length || 0} 条配置 · ${duplicateCount ? `${duplicateCount} 个重复模板 ID` : "无重复模板 ID"}`;
    const head = $("#unlaunched-result-head"); head.replaceChildren();
    const headRow = document.createElement("tr");
    ["模板 ID", "LevelId", "配置来源", ...Array.from({ length: 10 }, (_, index) => `DupPara${index + 1}`), "匹配状态"].forEach((copy, index) => {
      const th = document.createElement("th"); th.textContent = copy; if (index === 0) th.className = "sticky"; headRow.append(th);
    });
    head.append(headRow);
    const body = $("#unlaunched-result-body"); body.replaceChildren();
    (result.rows || []).forEach((item) => {
      const row = document.createElement("tr"); if (item.is_duplicate) row.classList.add("has-duplicate");
      const values = [item.template_id, item.level_id, item.config_level_path, ...(item.dup_params || Array(10).fill("—"))];
      values.forEach((value, index) => { const cell = document.createElement("td"); cell.textContent = String(value || "—"); if (index === 0) cell.classList.add("sticky"); row.append(cell); });
      const status = document.createElement("td"); const badge = document.createElement("span"); badge.className = item.is_duplicate ? "duplicate-id-chip" : "status-chip success"; badge.textContent = item.is_duplicate ? `重复 ID · 共 ${item.match_count} 条` : "唯一"; status.append(badge); row.append(status); body.append(row);
    });
    const warnings = $("#unlaunched-parameter-warnings"); warnings.replaceChildren();
    (result.warnings || []).forEach((warning) => { const p = document.createElement("p"); p.textContent = warning; warnings.append(p); });
    root.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function resolveParameterTemplates() {
    const button = $("#resolve-parameter-templates");
    const context = parameterRequestContext();
    const mode = state.parameterMode;
    const operationToken = ++state.parameterResolveToken;
    state.parameterCalculationToken += 1;
    try {
      if (mode === "unlaunched" && !state.profile?.profile?.ulinkconfig_root) throw new Error("请先选择 ulinkconfig 目录。");
      setBusy(button, true, mode === "unlaunched" ? "正在查询…" : "正在核验…"); let body = { source: context.source };
      if (context.source === "output_ids") {
        body.template_ids = uniqueLines($("#parameter-template-ids").value);
        if (!body.template_ids.length) throw new Error("请至少输入一个新模板 ID。");
        if (!body.template_ids.every(isValidTemplateId)) throw new Error("模板 ID 只能包含英文字母、数字、下划线和连字符，且长度不超过 180 个字符。");
      }
      if (context.source === "files") body.files = await filesToPayload(state.parameterFiles);
      if (context.source === "recent_export") Object.assign(body, recentExportResolveFields(context));
      if (operationToken !== state.parameterResolveToken || !parameterRequestContextIsCurrent(context)) return;
      const payload = await api("/parameters/resolve", { method: "POST", body });
      if (operationToken !== state.parameterResolveToken || !parameterRequestContextIsCurrent(context)) return;
      state.parameterResult = null;
      $("#parameter-results")?.classList.add("is-hidden");
      state.parameterSelectionId = payload.selection_id;
      state.parameterSelectionContext = {
        ...context,
        exportId: payload.source_export_id || context.exportId,
        jobId: payload.source_job_id || context.jobId,
      };
      if (payload.source_export_id && state.recentExport?.exportId === payload.source_export_id) {
        state.recentExport.parameterConfigurationCompleted = Boolean(payload.parameter_configured || payload.parameter_configured_at);
        state.recentExport.parameterConfiguredAt = payload.parameter_configured_at || null;
        updateExportSuccessActionPriority();
      }
      const root = $("#resolved-parameter-templates"); root.replaceChildren();
      (payload.items || []).forEach((item) => { const row = document.createElement("div"); row.className = "resolved-template-row"; const left = document.createElement("span"); left.textContent = `${item.output_template_id} ← ${item.source_template_id}`; const right = document.createElement("span"); right.textContent = `${item.replacement_count} 格替换`; row.append(left, right); root.append(row); });
      if (mode === "unlaunched") {
        const result = await lookupUnlaunchedParameters(payload.selection_id, context);
        if (!result || operationToken !== state.parameterResolveToken || state.parameterMode !== mode || !parameterRequestContextIsCurrent(context)) return;
        if (result.source_export_id && state.recentExport?.exportId === result.source_export_id) {
          state.recentExport.parameterConfigurationCompleted = Boolean(result.parameter_configured || result.parameter_configured_at);
          state.recentExport.parameterConfiguredAt = result.parameter_configured_at || null;
          updateExportSuccessActionPriority();
        }
        toast(`已按 ${result.requested_template_ids?.length || 0} 个原模板 ID 查询现有参数。`);
      } else {
        $("#calculate-parameters").disabled = !$("#config-set-select").value;
        toast(`已确认 ${payload.items?.length || 0} 个新模板。`);
      }
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); }
  }
  async function calculateParameters() {
    const button = $("#calculate-parameters");
    const selectionId = state.parameterSelectionId;
    const selectionContext = state.parameterSelectionContext ? { ...state.parameterSelectionContext } : null;
    const operationToken = ++state.parameterCalculationToken;
    try {
      const strategy = $('input[name="parameter-strategy"]:checked')?.value;
      if (!selectionId || !selectionContext) throw new Error("请先确认新模板。");
      state.parameterResult = null;
      $("#parameter-results")?.classList.add("is-hidden");
      setBusy(button, true, "正在只读计算…");
      const result = await api("/parameters/calculate", { method: "POST", body: {
        selection_id: selectionId,
        config_set_id: $("#config-set-select").value,
        level_start: Math.round(numberValue("#level-range-start", 200)),
        level_end: Math.round(numberValue("#level-range-end", 300)),
        strategy,
      } });
      if (operationToken !== state.parameterCalculationToken
        || state.parameterSelectionId !== selectionId
        || !parameterRequestContextIsCurrent(selectionContext)) return;
      state.parameterResult = result;
      if (result.source_export_id
        && result.source_export_id === selectionContext.exportId
        && state.recentExport?.exportId === result.source_export_id) {
        state.recentExport.parameterConfigurationCompleted = Boolean(result.parameter_configured || result.parameter_configured_at);
        state.recentExport.parameterConfiguredAt = result.parameter_configured_at || null;
        if (state.exportSnapshot?.export_id === result.source_export_id) {
          state.exportSnapshot.parameter_configured = state.recentExport.parameterConfigurationCompleted;
          state.exportSnapshot.parameter_configured_at = state.recentExport.parameterConfiguredAt;
        }
        updateExportSuccessActionPriority();
      }
      renderParameterResult();
      updateConsumptionCalibrationActionState();
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(button, false); }
  }
  function renderParameterResult() {
    const result = state.parameterResult; if (!result) return;
    $("#parameter-results").classList.remove("is-hidden");
    $("#parameter-result-summary").textContent = `${result.rows?.length || 0} 组结果 · ${result.level_range?.start}–${result.level_range?.end} 关 · ${result.strategy?.label || ""} · 参数仅供参考，不能保证消耗相同`;
    const head = $("#parameter-result-head"); head.replaceChildren();
    const groupRow = document.createElement("tr");
    [["修改前模板 ID", 1, 2], ["修改后模板 ID", 1, 2], ["修改前用于关卡位置", 1, 2], ["修改前 DupPara", 10, 1], ["新推荐 DupPara", 10, 1], ["替换 A/B 结构种类", 1, 2], ["状态", 1, 2]].forEach(([copy, span, rowSpan], index) => {
      const th = document.createElement("th"); th.textContent = copy; th.colSpan = span; th.rowSpan = rowSpan; if (index === 0) th.className = "sticky"; groupRow.append(th);
    });
    const detailRow = document.createElement("tr");
    [...Array.from({ length: 10 }, (_, index) => `原 ${index + 1}`), ...Array.from({ length: 10 }, (_, index) => `新 ${index + 1}`)].forEach((copy) => { const th = document.createElement("th"); th.textContent = copy; detailRow.append(th); });
    head.append(groupRow, detailRow);
    const body = $("#parameter-result-body"); body.replaceChildren();
    (result.rows || []).forEach((item) => {
      const row = document.createElement("tr");
      const counts = item.source_a_tile_count == null || item.source_b_tile_count == null
        ? "原牌量未知"
        : `原牌 A/B ${item.source_a_tile_count}/${item.source_b_tile_count}`;
      const capacity = item.remaining_a_group_capacity == null || item.remaining_b_group_capacity == null
        ? "剩余三张组容量未知"
        : `剩余三张组 A/B ${item.remaining_a_group_capacity}/${item.remaining_b_group_capacity}`;
      const floors = item.minimum_a_types == null || item.minimum_b_types == null
        ? "经验底线未应用"
        : `保留底线 A/B ${item.minimum_a_types}/${item.minimum_b_types}`;
      const partiallyDeducted = (item.partial_deductions || []).filter((part) => part.applied_a < part.planned_a || part.applied_b < part.planned_b).length;
      const readyStatus = `可用${partiallyDeducted ? ` · ${partiallyDeducted} 个副本少扣` : ""}`;
      const values = [item.source_template_id, item.output_template_id, (item.online_positions || []).join(", ") || "—", ...(item.before_dup || Array(10).fill("—")), ...(item.recommended_dup || Array(10).fill("—")), `${item.replaced_a_type_count} / ${item.replaced_b_type_count}`, item.status === "ready" ? readyStatus : item.message || "异常"];
      values.forEach((value, index) => { const cell = document.createElement("td"); cell.textContent = String(value); if (index === 0) cell.classList.add("sticky"); if (item.status !== "ready") cell.classList.add("error-cell"); if (index === values.length - 1 && item.status === "ready") { cell.classList.add("parameter-status-cell"); const detail = document.createElement("small"); detail.textContent = `${counts} · ${capacity} · ${floors}`; cell.append(detail); } row.append(cell); }); body.append(row);
    });
    const warnings = $("#parameter-warnings"); warnings.replaceChildren();
    (result.warnings || []).forEach((warning) => { const p = document.createElement("p"); p.textContent = warning; warnings.append(p); });
    $("#parameter-results").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function downloadParameterCsv() {
    const result = state.parameterResult; if (!result) return;
    const headers = ["修改前模板ID", "修改后模板ID", "修改前用于关卡位置", ...Array.from({ length: 10 }, (_, index) => `修改前DupPara${index + 1}`), ...Array.from({ length: 10 }, (_, index) => `新推荐DupPara${index + 1}`), "替换A结构种类数", "替换B结构种类数", "替换A牌格数", "替换B牌格数", "原模板A牌数量", "原模板B牌数量", "剩余A三张组容量", "剩余B三张组容量", "主牌侧", "保留A种类底线", "保留B种类底线", "少扣副本数", "状态", "说明（参数参考，不能保证消耗相同）"];
    const rows = (result.rows || []).map((item) => [item.source_template_id, item.output_template_id, (item.online_positions || []).join("|"), ...(item.before_dup || Array(10).fill("")), ...(item.recommended_dup || Array(10).fill("")), item.replaced_a_type_count, item.replaced_b_type_count, item.replaced_a_tile_count, item.replaced_b_tile_count, item.source_a_tile_count, item.source_b_tile_count, item.remaining_a_group_capacity, item.remaining_b_group_capacity, item.source_majority_side, item.minimum_a_types, item.minimum_b_types, (item.partial_deductions || []).filter((part) => part.applied_a < part.planned_a || part.applied_b < part.planned_b).length, item.status, item.message || ""]);
    const warningRows = (result.warnings || []).map((warning) => {
      const row = Array(headers.length).fill("");
      row[headers.indexOf("状态")] = "warning";
      row[headers.indexOf("说明（参数参考，不能保证消耗相同）")] = warning;
      return row;
    });
    downloadCsvFile(headers, [...rows, ...warningRows], `新模板参数推荐_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.csv`);
  }

  function bindEvents() {
    $("#automatic-group-max-difference").addEventListener("input", capturePlanForm);
    $("#task-source-preview").addEventListener("close", () => { state.sourcePreviewToken++; });
    $("#collapse-candidates").addEventListener("click", () => {
      $("#candidate-workspace").classList.add("is-hidden");
      $("#generation-state").classList.remove("is-hidden");
      $("#generation-activity-log").scrollIntoView({block:"center"});
    });
    document.addEventListener("change", event => {
      if (event.target.matches("[data-source-layer-mode], [data-source-layer-range]")) changeSourceLayer(event.target);
    });
    document.addEventListener("click", event => {
      const step = event.target.closest("[data-source-layer-step]");
      if (step) changeSourceLayer(step, Number(step.dataset.sourceLayerStep));
      const source = event.target.closest("[data-view-source]");
      if (source) showTaskSource(source.dataset.viewSource);
      if (event.target.closest("[data-source-preview-close]")) { state.sourcePreviewToken++; $("#task-source-preview").close(); }
    });
    document.addEventListener("click", (event) => {
      const nav = event.target.closest("[data-nav]"); if (nav) {
        event.preventDefault();
        if (nav.dataset.nav === "parameters") {
          state.parameterEntryPreferRecent = nav.id === "configure-exported-templates" && Boolean(state.recentExport);
          if (state.parameterEntryPreferRecent) state.parameterMode = "online";
        }
        navigate(nav.dataset.nav); return;
      }
      const tab = event.target.closest("[data-import-tab]"); if (tab) { switchImportTab(tab.dataset.importTab); return; }
      const step = event.target.closest("#stepper li button");
      if (step) {
        const stepNumber = Number(step.closest("li").dataset.step);
        if (stepNumber === 4) openExportStep(); else setFlow(stepNumber);
        return;
      }
      const matrixRow = event.target.closest("#template-matrix-body tr"); if (matrixRow) { editTemplatePlan(matrixRow.dataset.templateKey); return; }
      const workTab = event.target.closest("[data-work-index]"); if (workTab) { setActiveWork(Number(workTab.dataset.workIndex)); return; }
      const compare = event.target.closest("[data-compare-candidate]"); if (compare) { toggleCompare(compare.dataset.compareCandidate); return; }
      const focus = event.target.closest("[data-focus-candidate]"); if (focus) { openFocus(focus.dataset.focusCandidate); return; }
      const choose = event.target.closest("[data-select-candidate]"); if (choose) { chooseCandidate(choose.dataset.selectCandidate); return; }
      const adjust = event.target.closest("[data-adjust-failed-template]");
      if (adjust) { adjustFailedTemplate(adjust.dataset.adjustFailedTemplate, adjust.dataset.failedPlanId); return; }
      const runStaged = event.target.closest("[data-run-staged-template]");
      if (runStaged) { runSingleStagedTemplate(runStaged.dataset.runStagedTemplate, runStaged); return; }
      const taskAction = event.target.closest("[data-task-action]"); if (taskAction) {
        const run = () => controlTask(taskAction.dataset.taskKind, taskAction.dataset.taskId, taskAction.dataset.taskAction);
        if (taskAction.dataset.taskAction === "cancel") requestConfirmation("终止这项任务？", "已完成结果会保留；当前计算会在下一个安全检查点停止。", run);
        else run();
        return;
      }
      const load = event.target.closest("[data-load-job]"); if (load) { loadJob(load.dataset.loadJob); return; }
      const loadExport = event.target.closest("[data-load-export]"); if (loadExport) { loadExportRecord(loadExport.dataset.loadExport, loadExport.dataset.jobId); return; }
      const parameterTab = event.target.closest("[data-param-source]"); if (parameterTab && !parameterTab.disabled) { switchParameterSource(parameterTab.dataset.paramSource); return; }
      const calibrationTab = event.target.closest("[data-calibration-source]"); if (calibrationTab && !calibrationTab.disabled) { switchCalibrationSource(calibrationTab.dataset.calibrationSource); return; }
      const parameterMode = event.target.closest("[data-parameter-mode]"); if (parameterMode) { switchParameterMode(parameterMode.dataset.parameterMode); return; }
      const choosePath = event.target.closest("[data-choose-path]"); if (choosePath) {
        const purpose = choosePath.dataset.choosePath; const target = purpose === "output" ? "#setting-output-path" : "#setting-ulink-path"; handleChoosePath(purpose, target); return;
      }
    });
    document.addEventListener("keydown", (event) => {
      const modeTab = event.target.closest?.("[data-parameter-mode]");
      if (modeTab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const tabs = $$('[data-parameter-mode]');
        const current = tabs.indexOf(modeTab);
        const next = event.key === "Home" ? 0
          : event.key === "End" ? tabs.length - 1
            : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        switchParameterMode(tabs[next].dataset.parameterMode);
        tabs[next].focus();
        return;
      }
      if (!['Enter', ' '].includes(event.key)) return;
      const row = event.target.closest?.("[data-load-job]");
      if (row) { event.preventDefault(); loadJob(row.dataset.loadJob); }
    });
    $("#template-ids").addEventListener("input", () => { $("#template-id-count").textContent = `已输入 ${uniqueLines($("#template-ids").value).length} 项`; updateImportActionState(); });
    $("#template-files").addEventListener("change", (event) => { state.importFiles = [...event.target.files]; renderFileList("#template-file-list", state.importFiles); updateImportActionState(); });
    $("#template-drop-zone").addEventListener("dragover", (event) => event.preventDefault());
    $("#template-drop-zone").addEventListener("drop", (event) => { event.preventDefault(); state.importFiles = [...event.dataTransfer.files]; renderFileList("#template-file-list", state.importFiles); updateImportActionState(); });
    $("#inspect-templates").addEventListener("click", inspectTemplates);
    $("#abandon-task").addEventListener("click", () => {
      const committing = state.exportSnapshot?.status === "committing";
      requestConfirmation(
        "放弃当前任务并重新创建？",
        committing
          ? "当前导出已进入安全写入，无法中断，但会继续完成并保留在任务记录中。页面将清空当前工作区，历史任务、已完成结果和输出文件都不会删除。"
          : "运行中的生成或导出会请求在下一个安全检查点终止。页面将清空当前工作区，但历史任务、已完成结果、输出文件和路径设置都会保留。",
        abandonCurrentTask,
        { safeLabel: "保留当前任务", confirmLabel: "放弃并重新创建" },
      );
    });
    $("#export-new-task").addEventListener("click", () => {
      resetCurrentTaskWorkspace();
      toast("已返回导入模板，可开始创建新任务。");
    });
    $("#open-export-directory").addEventListener("click", openCompletedExportDirectory);
    $("#back-to-import").addEventListener("click", () => setFlow(1));
    $("#precision-mode-toggle").addEventListener("click", () => setPrecisionMode(!state.batchPlan.precisionMode));
    $("#edit-batch-plan").addEventListener("click", editBatchPlan); $("#close-template-editor").addEventListener("click", editBatchPlan);
    $("#template-plan-mode").addEventListener("change", () => {
      invalidateGenerationForPlanChange();
      const rule = state.templateRules.get(state.activeTemplate); if (!rule) return;
      const nextMode = $("#template-plan-mode").value;
      if (rule.mode === "use_batch" && ["replace", "append"].includes(nextMode)) rule.plan = clone(state.batchPlan);
      rule.mode = nextMode;
      $("#template-mode-help").textContent = PLAN_MODE_COPY[rule.mode]; $("#plan-fields").classList.toggle("is-hidden", ["use_batch", "skip"].includes(rule.mode)); renderMatrix(); renderConfigurationSummary();
      if (!["use_batch", "skip"].includes(rule.mode)) syncPlanForm(rule.plan);
    });
    const planIds = ["unit-count", "candidate-count", "search-attempt-budget", "unit-1-step", "unit-2-step", "required-pre-gate-recycles", "first-recycle-lead-tiles", "layout", "tower-mode", "replacement-sources", "type-count-kind", "type-count-min", "type-count-max", "uniform-groups-per-type", "custom-groups-per-type", "max-layer-kind", "max-layer-value", "total-limit-kind", "total-limit-value", "max-structure-layer-span-kind", "max-structure-layer-span", "quality-position", "quality-symmetry", "quality-density", "experience-archetype", "special-preset", "long-range-wave-count", "online-style-reference", "serial-difficulty"];
    planIds.forEach((id) => $(`#${id}`).addEventListener("change", capturePlanForm));
    ["uniform-groups-per-type", "custom-groups-per-type"].forEach((id) => $(`#${id}`).addEventListener("input", capturePlanForm));
    $("#total-count-minimum").addEventListener("input", capturePlanForm);
    $("#total-minimum-kind").addEventListener("change", capturePlanForm);
    $$('input[name="structure-group-mode"]').forEach((input) => input.addEventListener("change", capturePlanForm));
    $$('input[name="difficulty-target"]').forEach((input) => input.addEventListener("change", capturePlanForm));
    $("#total-limit-value").addEventListener("input", () => {
      refreshConditionalPlanFields();
    });
    $("#max-layer-value").addEventListener("input", refreshConditionalPlanFields);
    $("#required-pre-gate-recycles").addEventListener("input", refreshConditionalPlanFields);
    $("#first-recycle-lead-tiles").addEventListener("input", refreshConditionalPlanFields);
    $("#max-structure-layer-span").addEventListener("input", refreshConditionalPlanFields);
    $("#min-same-type-layer-span").addEventListener("input", capturePlanForm);
    $$("#allowed-types input").forEach((input) => input.addEventListener("change", capturePlanForm));
    $("#batch-seed").addEventListener("input", () => { invalidateGenerationForPlanChange(); state.seed = $("#batch-seed").value.trim(); state.seedWasAutoResolved = false; });
    $("#lock-seed").addEventListener("change", () => { if ($("#lock-seed").checked && !state.seed) { state.seed = String(crypto.getRandomValues(new Uint32Array(1))[0]); state.seedWasAutoResolved = false; $("#batch-seed").value = state.seed; } });
    $("#copy-seed").addEventListener("click", async () => { if (!state.seed) return toast("当前还没有谜题生成种子。", "error"); try { await navigator.clipboard.writeText(state.seed); toast("谜题生成种子已复制。"); } catch { toast(`种子：${state.seed}`); } });
    $("#generate-candidates").addEventListener("click", startGeneration);
    $("#generation-pause").addEventListener("click", () => controlCurrentJob($("#generation-pause").dataset.action || "pause"));
    $("#generation-cancel").addEventListener("click", () => requestConfirmation("终止候选生成？", "已完成的成品方案会保留；当前方案会在下一个安全检查点停止，之后可继续搜索未完成项。", () => controlCurrentJob("cancel")));
    $("#generation-use-partial").addEventListener("click", useAvailableCandidates);
    $("#generation-adjust-and-continue").addEventListener("click", () => openContinuationParameterEditor($("#generation-adjust-and-continue")));
    $("#generation-retry").addEventListener("click", () => {
      if ($("#generation-retry").dataset.action === "adjust") {
        const first = (state.jobResult?.items || []).find(isDeterministicFailure); if (first) adjustFailedTemplate(first.template_key, first.plan_id);
      } else continueCurrentJobSearch();
    });
    $("#undo-parameter-suggestion").addEventListener("click", () => undoParameterSuggestion($("#undo-parameter-suggestion")));
    $("#regenerate").addEventListener("click", () => { if (!$("#lock-seed").checked && state.seedWasAutoResolved) { state.seed = ""; state.seedWasAutoResolved = false; $("#batch-seed").value = ""; } startGeneration(); });
    $("#work-prev").addEventListener("click", () => setActiveWork(state.activeWorkIndex - 1));
    $("#work-next").addEventListener("click", () => setActiveWork(state.activeWorkIndex + 1));
    $("#clear-work-selection").addEventListener("click", clearActiveWorkSelection);
    $("#complete-work-selection").addEventListener("click", completeActiveWorkSelection);
    $("#candidate-prev").addEventListener("click", () => { state.candidatePage -= 1; renderCandidateCards(); }); $("#candidate-next").addEventListener("click", () => { state.candidatePage += 1; renderCandidateCards(); });
    $("#candidate-grid").addEventListener("click", (event) => {
      const info = event.target.closest("[data-candidate-parameter-info]");
      if (info) { openCandidateParameterDialog(info.dataset.candidateParameterInfo); return; }
    });
    $("#open-compare").addEventListener("click", openComparison); $("#close-compare").addEventListener("click", () => { $("#compare-workspace").classList.add("is-hidden"); $("#candidate-workspace").classList.remove("is-hidden"); window.setTimeout(() => $("#open-compare")?.focus(), 0); });
    $("#compare-mode").addEventListener("change", renderComparison); $("#compare-layer").addEventListener("input", () => { $("#compare-layer-value").textContent = $("#compare-layer").value; renderComparison(); });
    $("#compare-zoom").addEventListener("input", () => { $("#compare-zoom-value").textContent = `${$("#compare-zoom").value}%`; renderComparison(); });
    $("#focus-candidate-prev").addEventListener("click", () => moveFocusedCandidate(-1));
    $("#focus-candidate-next").addEventListener("click", () => moveFocusedCandidate(1));
    $("#focus-work-prev").addEventListener("click", () => moveFocusedWorkItem(-1));
    $("#focus-work-next").addEventListener("click", () => moveFocusedWorkItem(1));
    $("#close-focus").addEventListener("click", () => closeFocus());
    $("#focus-layer").addEventListener("input", () => setFocusLayer($("#focus-layer").value));
    $("#focus-layer-down").addEventListener("click", () => setFocusLayer(Number($("#focus-layer").value) - 1));
    $("#focus-layer-up").addEventListener("click", () => setFocusLayer(Number($("#focus-layer").value) + 1));
    $("#focus-layer-all").addEventListener("click", () => setFocusLayer(Number($("#focus-layer").max)));
    $("#trace-unit").addEventListener("change", () => { stopPlayer(); state.player.unitIndex = Number($("#trace-unit").value); state.player.routeKey = "threshold"; state.player.step = 0; renderTraceRoutes(); renderPlayer(); });
    $("#trace-route").addEventListener("change", () => { stopPlayer(); state.player.routeKey = $("#trace-route").value; state.player.step = 0; renderPlayer(); });
    $("#trace-prev").addEventListener("click", () => stepPlayer(-1)); $("#trace-next").addEventListener("click", () => stepPlayer(1)); $("#trace-play").addEventListener("click", togglePlayer);
    $("#trace-timeline").addEventListener("input", () => { stopPlayer(); state.player.step = Number($("#trace-timeline").value); renderPlayer(); });
    $("#trace-speed").addEventListener("change", () => { if (state.player.timer) { stopPlayer(); togglePlayer(); } });
    $("#back-to-candidates").addEventListener("click", () => {
      const terminal = ["completed", "failed", "cancelled"].includes(state.exportSnapshot?.status);
      if (terminal) resetExportViewContext();
      setFlow(3); window.setTimeout(() => $("#review-selections")?.focus(), 0);
    }); $("#export-selected").addEventListener("click", exportSelected);
    $("#export-pause").addEventListener("click", () => controlCurrentExport($("#export-pause").dataset.action || "pause"));
    $("#export-cancel").addEventListener("click", () => requestConfirmation("终止验证导出？", "若尚未进入安全写入，任务会在检查点停止且不会留下半份正式文件；已进入事务提交后将继续完成。", () => controlCurrentExport("cancel")));
    $("#export-retry").addEventListener("click", () => controlCurrentExport("retry"));
    document.addEventListener("click", (event) => { if (event.target.closest("#review-selections")) openExportStep(); });
    $("#work-item-tabs").addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      if (!event.target.closest?.('[role="tab"]')) return;
      event.preventDefault();
      const length = successfulWorkItems().length;
      if (!length) return;
      const last = length - 1;
      const next = event.key === "Home" ? 0 : event.key === "End" ? last : (state.activeWorkIndex + (event.key === "ArrowRight" ? 1 : -1) + length) % length;
      setActiveWork(next); window.setTimeout(() => $("#work-item-tabs [aria-selected=\"true\"]")?.focus(), 0);
    });
    $("#refresh-tasks").addEventListener("click", refreshTasks); $("#refresh-reports").addEventListener("click", refreshReports);
    $$('[name="source-directory-mode"]').forEach((input) => input.addEventListener("change", () => setSourceDirectoryMode(input.value)));
    $("#choose-template-directory").addEventListener("click", chooseTemplateDirectory);
    $("#setting-template-path").addEventListener("input", () => sourceDirectoryError());
    $("#save-source-setting").addEventListener("click", saveSourceSetting); $("#save-output-setting").addEventListener("click", saveOutputSetting); $("#save-ulink-setting").addEventListener("click", saveUlinkSetting);
    $("#open-help").addEventListener("click", openHelpDialog);
    $("#close-help").addEventListener("click", closeHelpDialog);
    $("#help-start-link").addEventListener("click", () => openHelpTarget("help-start"));
    $("#help-gradient-example-mode").addEventListener("change", renderGradientExample);
    $("#help-dialog").addEventListener("click", (event) => {
      if (event.target === $("#help-dialog")) closeHelpDialog();
      const result = event.target.closest?.("[data-help-result-target]");
      if (result) {
        openHelpTarget(result.dataset.helpResultTarget);
        return;
      }
      const inlineHelp = event.target.closest?.("[data-help-link]");
      if (inlineHelp) {
        event.preventDefault();
        openHelpTarget(inlineHelp.dataset.helpLink);
        return;
      }
      const link = event.target.closest?.(".help-nav a");
      if (link) {
        event.preventDefault();
        const search = $("#help-search-input");
        if (search) search.value = "";
        filterHelpTopics("");
        scrollToHelpTopic(link.getAttribute("href").slice(1), "smooth");
      }
    });
    $("#help-dialog").addEventListener("close", () => {
      const returnTarget = helpReturnFocus;
      if (returnTarget?.isConnected) window.setTimeout(() => returnTarget.focus(), 0);
      helpReturnFocus = null;
    });
    $("#help-search-input").addEventListener("input", (event) => {
      filterHelpTopics(event.currentTarget.value);
      $("#help-content").scrollTop = 0;
    });
    $("#clear-help-search").addEventListener("click", () => {
      const search = $("#help-search-input");
      search.value = "";
      filterHelpTopics("");
      search.focus();
    });
    $("#help-content").addEventListener("scroll", () => {
      if (helpScrollFrame) return;
      helpScrollFrame = window.requestAnimationFrame(syncHelpTopicFromScroll);
    }, { passive: true });
    window.openHelpTarget = openHelpTarget;
    $("#close-template-adjustment").addEventListener("click", () => $("#template-adjustment-dialog").close("cancel"));
    $("#cancel-template-adjustment").addEventListener("click", () => $("#template-adjustment-dialog").close("cancel"));
    $("#save-template-adjustment").addEventListener("click", saveTemplateAdjustment);
    $$("input[name=\"continuation-adjustment-mode\"]").forEach((input) => input.addEventListener("change", () => {
      const context = state.adjustmentDialog;
      if (context?.kind !== "continuation") return;
      if (context.draft) capturePlanForm();
      if (input.value === "uniform") {
        context.uniformDraft = clone(context.draft);
        context.draft = context.uniformDraft;
      }
      refreshContinuationTargetOptions();
    }));
    $("#continuation-template-list").addEventListener("change", refreshContinuationTargetOptions);
    $("#continuation-adjustment-target").addEventListener("change", (event) => switchContinuationAdjustmentTarget(event.currentTarget.value));
    $("#template-adjustment-dialog").addEventListener("close", restoreAdjustmentForm);
    $("#template-adjustment-dialog").addEventListener("click", (event) => {
      if (event.target === $("#template-adjustment-dialog")) $("#template-adjustment-dialog").close("cancel");
    });
    $("#close-candidate-parameter").addEventListener("click", () => $("#candidate-parameter-dialog").close());
    $("#candidate-parameter-dialog").addEventListener("click", (event) => {
      if (event.target === $("#candidate-parameter-dialog")) $("#candidate-parameter-dialog").close();
    });
    $("#candidate-task-overview").addEventListener("toggle", (event) => {
      state.candidateOverviewOpened = event.currentTarget.open;
      $("#candidate-task-overview > summary")?.setAttribute("aria-expanded", String(event.currentTarget.open));
    });
    $("#confirm-dialog").addEventListener("close", () => {
      const action = state.confirmAction; const trigger = state.confirmTrigger; const confirmed = $("#confirm-dialog").returnValue === "confirm";
      state.confirmAction = null; state.confirmTrigger = null;
      if (confirmed && typeof action === "function") action();
      else window.setTimeout(() => trigger?.isConnected && trigger.focus(), 0);
    });
    $("#parameter-template-ids").addEventListener("input", updateParameterResolveState);
    $("#parameter-files").addEventListener("change", (event) => { state.parameterFiles = [...event.target.files]; renderFileList("#parameter-file-list", state.parameterFiles); updateParameterResolveState(); });
    const parameterDrop = $("label[for=\"parameter-files\"]");
    parameterDrop.addEventListener("dragover", (event) => event.preventDefault());
    parameterDrop.addEventListener("drop", (event) => { event.preventDefault(); state.parameterFiles = [...event.dataTransfer.files]; renderFileList("#parameter-file-list", state.parameterFiles); updateParameterResolveState(); });
    $("#resolve-parameter-templates").addEventListener("click", resolveParameterTemplates); $("#choose-ulink-path").addEventListener("click", chooseUlinkFromParameter);
    $("#choose-unlaunched-ulink-path").addEventListener("click", chooseUlinkFromParameter);
    $("#calculate-parameters").addEventListener("click", calculateParameters); $("#download-parameter-csv").addEventListener("click", downloadParameterCsv);
    $("#config-set-select").addEventListener("change", () => { state.defaultConfigSetId = $("#config-set-select").value; invalidateConsumptionCalibration(); });
    $("#calibration-template-ids").addEventListener("input", updateCalibrationResolveState);
    $("#calibration-template-files").addEventListener("change", (event) => { state.calibrationFiles = [...event.target.files]; renderFileList("#calibration-template-file-list", state.calibrationFiles); updateCalibrationResolveState(); });
    const calibrationDrop = $('label[for="calibration-template-files"]');
    calibrationDrop.addEventListener("dragover", (event) => event.preventDefault());
    calibrationDrop.addEventListener("drop", (event) => { event.preventDefault(); state.calibrationFiles = [...event.dataTransfer.files]; renderFileList("#calibration-template-file-list", state.calibrationFiles); updateCalibrationResolveState(); });
    $("#resolve-calibration-templates").addEventListener("click", resolveCalibrationTemplates);
    $("#consumption-history-file").addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      state.consumptionHistoryFile = file || null;
      invalidateConsumptionCalibration();
      announce(
        "#consumption-history-file-copy",
        file ? `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB` : "包含模板、副本参数、进入人数与人均消耗；策略组 ID 非必需",
      );
      updateConsumptionCalibrationActionState();
      refreshCalibrationNameMatches();
    });
    $("#consumption-config-level-file").addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      state.consumptionConfigLevelFile = file || null;
      invalidateConsumptionCalibration();
      announce(
        "#consumption-config-level-file-copy",
        file ? `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB` : "默认读取当前 ulinkconfig；仅需覆盖本次计算时上传",
      );
    });
    $("#consumption-target-mode").addEventListener("change", () => { invalidateConsumptionCalibration(); syncConsumptionTargetMode(); });
    $("#consumption-seed-policy").addEventListener("change", () => { invalidateConsumptionCalibration(); syncConsumptionSeedPolicy(); });
    $("#consumption-auto-first-max").addEventListener("change", invalidateConsumptionCalibration);
    $("#consumption-strategy-source").addEventListener("change", () => {
      invalidateConsumptionCalibration(); syncConsumptionSeedPolicy();
    });
    $("#consumption-strategy-policy").addEventListener("change", () => { invalidateConsumptionCalibration(); syncConsumptionSeedPolicy(); });
    ["#consumption-template-prefix", "#consumption-template-suffix"].forEach(selector => $(selector).addEventListener("input", () => { invalidateConsumptionCalibration(); scheduleCalibrationNameMatches(); }));
    $$("[data-consumption-target]").forEach((input) => input.addEventListener("input", invalidateConsumptionCalibration));
    $("#run-consumption-calibration").addEventListener("click", runConsumptionCalibration);
    $("#consumption-calibration-templates").addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-recalculate-calibration]");
      if (trigger) recalculateConsumptionTemplate(trigger.dataset.recalculateCalibration, trigger);
    });
    $("#download-calibration-review").addEventListener("click", downloadCalibrationReviewCsv);
    $("#download-calibration-draft").addEventListener("click", downloadCalibrationDraftCsv);
    $("#write-calibration-config").addEventListener("click", writeConsumptionCalibrationConfig);
  }

  async function bootstrap() {
    initStaticControls(); bindEvents(); syncPlanForm(state.batchPlan); setFlow(1); updateImportActionState(); updateParameterResolveState(); updateCalibrationResolveState();
    try {
      const payload = await api("/bootstrap"); state.csrf = payload.csrf_token; state.profile = payload.profile_status; state.strategies = payload.strategies || {}; renderProfile(); renderStrategies();
      const recoverableExport = (payload.exports || []).find((record) => (
        record?.status === "completed"
        && record?.export_id
        && Array.isArray(record?.result?.outputs)
        && record.result.outputs.length > 0
      ));
      if (recoverableExport) {
        rememberCompletedExport(recoverableExport);
        $("#recent-export-tab").disabled = false;
        $("#calibration-recent-export-tab").disabled = false;
        updateExportSuccessActionPriority();
        updateParameterResolveState();
        updateCalibrationResolveState();
      }
      if ((payload.jobs || []).length || (payload.exports || []).length) refreshTasks();
    } catch (error) { toast(error.message, "error"); $("#environment-text").textContent = "本地服务未连接"; }
  }

  bootstrap();
})();
