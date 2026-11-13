"use strict";

const TILE_PATH_SUFFIX = "tile/Tile/Assets/Resources/TileMatchLevelData";
const TEMPLATE_PATH_SUFFIX = "tileleveltemplate/Template";
const TERMINAL_JOB_STATES = new Set(["succeeded", "failed", "partial_failed", "cancelled", "interrupted"]);
const TERMINAL_TEMPLATE_STATES = new Set(["verified", "succeeded", "failed", "no_op", "no_tower", "exported", "interrupted", "cancelled"]);
const PHASE_ORDER = ["decode", "inspect", "distribution", "placement", "register_output"];
const ROUTE_TITLES = {workspace: "模板优化", ordinary_add: "增加Btile工具", parameters: "一键配置新参数", history: "运行记录", settings: "设置"};
const VM = window.TowerReducerViewModel;
const INCREASE = window.TowerIncrease;
const ORDINARY = window.TowerOrdinaryAdd;

const safeStorage = {
  failed: false,
  get(key) {
    try { return window.localStorage.getItem(key) || ""; }
    catch (_error) { this.failed = true; return ""; }
  },
  set(key, value) {
    try { window.localStorage.setItem(key, value); }
    catch (_error) { this.failed = true; }
  },
  remove(key) {
    try { window.localStorage.removeItem(key); }
    catch (_error) { this.failed = true; }
  },
};

const state = {
  csrfToken: "",
  bootstrap: {},
  settings: {},
  syncDefaultsInitialized: false,
  selectedFiles: [],
  importMode: "ids",
  importBusy: false,
  importSession: null,
  importController: null,
  importEpoch: 0,
  activeOverride: "",
  helpTopic: "start",
  importedFiles: [],
  importId: "",
  overrides: {},
  mode: "reduce",
  increaseDefaults: INCREASE.defaults(),
  increaseOverrides: {},
  ordinaryDefaults: ORDINARY.defaults(),
  ordinaryOverrides: {},
  workbenchGroup: "tower",
  workbenchContexts: {},
  increaseDraftTimer: null,
  currentJob: null,
  pendingJobId: "",
  jobCreationPending: false,
  currentJobId: safeStorage.get("tower-reducer-current-job"),
  exportPlan: null,
  syncPlan: null,
  exportPlanContext: "",
  syncPlanContext: "",
  namingRegenerationActive: false,
  zipBusy: false,
  lastFocused: null,
  pollTimer: null,
  pollController: null,
  batchController: null,
  pollFailures: 0,
  requestGeneration: 0,
  rerunKey: "",
  rerunKeys: [],
  rerunBatch: false,
  resultSelection: [],
  resultSelectionJob: "",
  rerunIncrease: null,
  rerunOrdinary: null,
  rerunAdviceContext: "",
  rerunSession: 0,
  rerunPreviewActive: false,
  templatePreview: null,
  previewSourceOnly: false,
  previewRequest: 0,
  previewController: null,
  previewJobId: "",
  previewTemplateKey: "",
  translationBusy: null,
  batchCenter: null,
  translationFeedback: "",
  translationError: false,
  manualDraft: null,
  manualMessage: "",
  manualError: false,
  previewReturnPosition: null,
  resultPreviewCache: new Map(),
  resultPreviewObserver: null,
  resultPreviewWork: {active:0,queue:[],inflight:new Set()},
  exportTask: null,
  exportPreviewTask: null,
  exportPreviewRun: null,
  toastTimer: null,
  deliveryCompletion: 0,
  deliveryDirectories: [],
};

const byId = (id) => document.getElementById(id);
const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const asObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const asArray = (value) => Array.isArray(value) ? value : Object.values(asObject(value));
const asText = (value, fallback = "") => typeof value === "string" && value.trim() ? value : fallback;
const asNumber = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
const formatBytes = (value) => {
  const bytes = asNumber(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};
const escapeHTML = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function icon(name) {
  return `<svg aria-hidden="true"><use href="/assets/icons.svg#${escapeHTML(name)}"></use></svg>`;
}

function setStatusPill(element, label, tone = "neutral") {
  if (!element) return;
  element.className = `status-pill ${tone}`;
  element.innerHTML = `${icon(tone === "danger" ? "warning" : "status")}<span>${escapeHTML(label)}</span>`;
}

function apiError(payload, status) {
  const root = asObject(payload);
  const wrapped = asObject(root.error);
  const message = asText(wrapped.message, asText(root.message, `本地服务返回错误（${status}）`));
  const error = new Error(message);
  error.code = asText(wrapped.code, asText(root.code, "request_failed"));
  error.details = wrapped.details ?? root.details ?? null;
  error.status = status;
  return error;
}

async function apiRequest(path, options = {}) {
  const init = {method: options.method || "GET", headers: {"Accept": "application/json"}};
  if (init.method !== "GET" && init.method !== "HEAD") {
    init.headers["X-CSRF-Token"] = state.csrfToken;
  }
  if (options.formData) {
    init.body = options.formData;
  } else if (Object.prototype.hasOwnProperty.call(options, "json")) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.json);
  }
  if (options.signal) init.signal = options.signal;
  const response = await fetch(path, init);
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json().catch(() => ({}));
  }
  if (!response.ok) throw apiError(payload, response.status);
  if (options.binary) return response.blob();
  return payload ?? {};
}

function setServiceStatus(kind, message) {
  const element = byId("service-status");
  element.classList.remove("is-connecting", "is-online", "is-offline");
  element.classList.add(kind);
  byId("service-status-text").textContent = message;
}

function showToast(message, type = "success", options = {}) {
  const region = byId("toast-region");
  window.clearTimeout(state.toastTimer);
  region.classList.toggle("is-centered", options.placement === "center");
  const toast = document.createElement("div");
  toast.className = `toast is-${type}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const title = document.createElement("strong");
  title.className = "toast-title";
  title.textContent = message;
  toast.append(title);
  if (options.detail) {
    const detail = document.createElement("p");
    detail.className = "toast-detail";
    detail.textContent = options.detail;
    toast.append(detail);
  }
  region.replaceChildren(toast);
  state.toastTimer = window.setTimeout(() => {
    toast.remove();
    state.toastTimer = null;
  }, options.duration ?? 5200);
}

function showFeedback(id, message, type = "neutral") {
  const box = byId(id);
  if (!box) return;
  box.hidden = false;
  box.classList.toggle("is-error", type === "error");
  box.classList.toggle("is-warning", type === "warning");
  const text = box.querySelector("span");
  if (text) text.textContent = message;
}

function showDeliveryCompletion(title, message, directories, returnFocusId = "") {
  state.deliveryCompletion += 1;
  state.deliveryDirectories = asArray(directories).filter(item => item && typeof item.path === "string");
  byId("completion-title").textContent = title;
  byId("completion-description").textContent = message;
  byId("completion-feedback").hidden = true;
  byId("completion-directories").innerHTML = state.deliveryDirectories.map(item => {
    const label = asText(item.label, "目录");
    const buttonLabel = label === "输出目录" ? "打开输出目录" : label === "Tile 仓" ? "打开 Tile 仓目录" : `打开${label}目录`;
    return `<div class="completion-directory"><div><strong>${escapeHTML(label)}</strong><p>${escapeHTML(item.path)}</p></div>${item.key ? `<button class="secondary-button" type="button" data-directory-key="${escapeHTML(item.key)}">${escapeHTML(buttonLabel)}</button>` : ""}</div>`;
  }).join("");
  if (!state.deliveryDirectories.some(item => item.key)) {
    showFeedback("completion-feedback", "当前服务不支持一键打开目录，请重新启动工具，或根据路径手动打开。", "warning");
  }
  openDialog(byId("delivery-completion-dialog"));
  if (returnFocusId) state.lastFocused = byId(returnFocusId);
  byId("completion-acknowledge").focus();
}

async function openCompletedDirectory(button) {
  if (button.disabled || !state.deliveryDirectories.some(item => item.key === button.dataset.directoryKey)) return;
  const completion = state.deliveryCompletion;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const result = await apiRequest("/api/open-directory", {method: "POST", json: {key: button.dataset.directoryKey}});
    if (state.deliveryCompletion === completion && byId("delivery-completion-dialog").open) {
      showFeedback("completion-feedback", result.opened ? "已请求打开目录。" : "未能打开目录，请重试。", result.opened ? "neutral" : "error");
    }
  } catch (error) {
    if (state.deliveryCompletion === completion && byId("delivery-completion-dialog").open) {
      showFeedback("completion-feedback", `打开目录失败：${error.message}`, "error");
    }
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function clearFeedback(id) {
  const box = byId(id);
  if (box) box.hidden = true;
}

function openDialog(dialog) {
  if (!dialog || dialog.open) return;
  state.lastFocused = document.activeElement;
  byId("app-shell").inert = true;
  dialog.showModal();
  const firstControl = dialog.querySelector("button:not([disabled]), input:not([disabled]), select:not([disabled])");
  if (firstControl) firstControl.focus();
}

function closeDialog(dialog, force = false) {
  if (!dialog || !dialog.open) return;
  if (dialog.dataset.pending === "true" && !force) return;
  if(dialog.id==="template-preview-dialog" && translationPending() && !force)return;
  if (dialog.id === "template-preview-dialog" && (state.manualDraft?.history.length || state.manualDraft?.recovery) && !force) {
    const confirmation=byId("manual-close-dialog");
    byId("manual-close-feedback").textContent="微调尚未保存。点击保存后统一验收；关闭不保存将丢弃本次草稿，已验收产物不变。";
    byId("manual-close-save").disabled=translationPending();
    state.previewCloseFocus=state.lastFocused;
    openDialog(confirmation);
    return;
  }
  dialog.close();
  byId("app-shell").inert = dialog.id==="manual-close-dialog" && byId("template-preview-dialog").open;
  if(dialog.id==="template-preview-dialog") {
    const position=state.previewReturnPosition;
    const origin=all("[data-preview-template]",byId("result-table-body")).find(button=>button.dataset.previewTemplate===position?.key && Boolean(button.dataset.resultThumbnail)===Boolean(position?.thumbnail));
    (origin || state.lastFocused)?.focus({preventScroll:true});
    if(position && position.jobId===state.currentJobId){window.scrollTo(position.x,position.y);const region=byId("result-table-body").closest(".table-scroll");if(region)region.scrollLeft=position.tableX;}
  } else if (state.lastFocused && document.contains(state.lastFocused)) state.lastFocused.focus({preventScroll:true});
  state.lastFocused = dialog.id==="manual-close-dialog" ? state.previewCloseFocus : null;
}

function setDialogPending(dialog, pending) {
  if (!dialog) return;
  dialog.dataset.pending = pending ? "true" : "false";
  all("button, input, select", dialog).forEach((control) => {
    if (control.dataset.keepEnabled === "true") return;
    if (pending) {
      control.dataset.wasDisabled = control.disabled ? "true" : "false";
      control.disabled = true;
    } else {
      control.disabled = control.dataset.wasDisabled === "true";
      delete control.dataset.wasDisabled;
    }
  });
}

function abortableDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("操作已取消", "AbortError"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("操作已取消", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", onAbort, {once: true});
  });
}

function focusTransitionHeading(heading, moveFocus = true) {
  if (moveFocus && heading) {
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    heading.focus({preventScroll: true});
  }
  const reducedMotion = Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  window.scrollTo({top: 0, behavior: VM.navigationScrollBehavior(reducedMotion)});
}

function routeTo(route, moveFocus = true) {
  if(state.batchCenter)state.batchCenter.cancelled=true;
  if (["workspace", "ordinary_add"].includes(route)) {
    if (!switchWorkbench(route === "ordinary_add" ? "ordinary" : "tower")) return;
  }
  const viewRoute = route === "ordinary_add" ? "workspace" : route;
  all("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== viewRoute;
  });
  all("[data-route]").forEach((control) => {
    const active = control.dataset.route === route;
    control.classList.toggle("is-active", active);
    if (active) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  });
  if (route === "history") renderHistory();
  if (route === "settings") loadSettings(false);
  if (route === "parameters") window.towerParameterUI?.enter();
  document.title = `${ROUTE_TITLES[route] || "塔占比修改"} · 塔占比修改及增加Btile工具`;
  const view = byId(`${viewRoute}-view`);
  const heading = viewRoute === "workspace"
    ? view?.querySelector("[data-flow]:not([hidden]) h1")
    : view?.querySelector("h1");
  focusTransitionHeading(heading, moveFocus);
}

function switchWorkbench(group) {
  if (state.workbenchGroup === group) return true;
  if (state.importBusy || state.namingRegenerationActive || state.jobCreationPending) {
    showToast("请等待当前读取或重新生成完成，再切换工具。", "warning");return false;
  }
  const keys = ["importId", "importedFiles", "selectedFiles", "importMode", "currentJob", "currentJobId", "mode"];
  state.workbenchContexts[state.workbenchGroup] = {
    ...Object.fromEntries(keys.map(key => [key, state[key]])),
    ids: byId("template-ids").value, source: byId("source-repository").value,
    step: document.querySelector("[data-flow]:not([hidden])")?.dataset.flow || "1",
    naming: byId("export-naming-rule").value, namingEdited: byId("export-naming-rule").dataset.edited,
    activeOverride:state.activeOverride, retainedCounts:collectRetainedCounts(),
  };
  cancelBatchRegeneration();cancelPolling();cancelTemplatePreview();
  state.pendingJobId = "";
  const saved = state.workbenchContexts[group] || {importId:"", importedFiles:[], selectedFiles:[], importMode:"ids",
    currentJob:null,currentJobId:"",mode:group === "ordinary" ? "ordinary_add" : "reduce",ids:"",source:"tile",step:1};
  for (const key of keys) state[key] = saved[key];
  state.workbenchGroup = group;
  state.importSession = null;
  byId("template-ids").value = saved.ids;
  byId("source-repository").value = saved.source;
  byId("template-files").value = "";
  byId("export-naming-rule").value = saved.naming || currentNamingRule();
  if (saved.namingEdited) byId("export-naming-rule").dataset.edited = saved.namingEdited;
  else delete byId("export-naming-rule").dataset.edited;
  resetImportSession();setImportMode(saved.importMode);
  updateSelectedFiles(saved.selectedFiles, false);
  state.importId = saved.importId;
  setWorkspaceMode(saved.mode);
  renderConfigurationList(saved.importedFiles.filter(row => ["ready", "no_tower"].includes(row.status) && !row.duplicateOf && !row.hasBlockingIssues));
  for (const input of all("[data-tower-file]")) {
    const value = saved.retainedCounts?.[input.dataset.towerFile]?.[Number(input.dataset.towerIndex)];
    if (Number.isInteger(value)) input.value = String(value);
  }
  if (saved.activeOverride && state.importedFiles.some(file => file.fileId === saved.activeOverride))
    setOverrideMode(saved.activeOverride, "custom");
  byId("import-issues-summary").hidden = true;
  if (state.currentJob) renderJob(state.currentJob);
  showStep(saved.step, false);
  if (state.currentJobId) safeStorage.set("tower-reducer-current-job", state.currentJobId);
  else safeStorage.remove("tower-reducer-current-job");
  if (state.currentJob && !TERMINAL_JOB_STATES.has(state.currentJob.status)) schedulePoll(100);
  return true;
}

function showStep(step, moveFocus = true) {
  const transition = VM.resolveFlowStep(step, state.importId);
  step = transition.step;
  if (transition.blocked) showToast(transition.message, "error");
  let activeSection = null;
  all("[data-flow]").forEach((section) => {
    const active = section.dataset.flow === String(step);
    section.hidden = !active;
    section.classList.toggle("is-active", active);
    if (active) activeSection = section;
  });
  all("[data-step-indicator]").forEach((item) => {
    const value = Number(item.dataset.stepIndicator);
    item.classList.toggle("is-current", value === Number(step));
    item.classList.toggle("is-complete", value < Number(step));
    if (value === Number(step)) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  focusTransitionHeading(activeSection?.querySelector("h1"), moveFocus);
}

function setInvalid(control, message) {
  if (!control) return false;
  control.setAttribute("aria-invalid", "true");
  const field = control.closest(".field") || control.parentElement;
  if (field) {
    const errorId = `${control.id || "field"}-error`;
    let error = byId(errorId);
    if (!error) {
      error = document.createElement("p");
      error.id = errorId;
      error.className = "field-error";
      error.setAttribute("role", "alert");
      field.append(error);
    }
    error.textContent = message || "请检查此字段。";
    const described = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    described.add(errorId);
    control.setAttribute("aria-describedby", [...described].join(" "));
  }
  return false;
}

function clearInvalid(form) {
  all("[aria-invalid='true']", form).forEach((control) => {
    control.removeAttribute("aria-invalid");
    const described = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => !id.endsWith("-error"));
    if (described.length) control.setAttribute("aria-describedby", described.join(" "));
    else control.removeAttribute("aria-describedby");
  });
  all(".field-error", form).forEach((error) => error.remove());
}

function focusFirstInvalid(form) {
  const firstInvalid = form.querySelector("[aria-invalid='true']");
  if (firstInvalid) firstInvalid.focus();
  return Boolean(firstInvalid);
}

function normalizeSettings(payload) {
  const root = asObject(payload);
  const settings = asObject(root.settings);
  const candidates = asObject(root.candidates);
  return {
    tileRoot: asText(settings.tile_root, asText(root.tile_root)),
    templateRoot: asText(settings.template_root, asText(root.template_root)),
    configRoot: asText(settings.config_root, asText(root.config_root)),
    exportRoot: asText(settings.export_root, asText(root.export_root)),
    namingRule: asText(settings.naming_rule, asText(root.naming_rule, "lessT{原模板ID}")),
    increaseNamingRule: asText(settings.increase_naming_rule, asText(root.increase_naming_rule, "moreT{原模板ID}")),
    tileConfirmed: Boolean(settings.tile_confirmed ?? root.tile_confirmed),
    templateConfirmed: Boolean(settings.template_confirmed ?? root.template_confirmed),
    syncReady: Boolean(settings.sync_ready ?? root.sync_ready),
    defaultSyncTargets: asArray(settings.default_sync_targets ?? root.default_sync_targets).filter(t => t === "tile" || t === "template"),
    candidates: {
      tile: asArray(candidates.tile).map(String),
      template: asArray(candidates.template).map(String),
    },
    discoveryTimedOut: Boolean(root.timed_out ?? asObject(root.discovery).timed_out),
    discoveryLimitReached: Boolean(root.limit_reached ?? asObject(root.discovery).limit_reached),
    loadError: root.load_error || settings.load_error || null,
  };
}

function normalizeImported(payload) {
  return VM.normalizeImported(payload);
}

function normalizeTemplate(row, key, index) {
  return VM.normalizeTemplate(row, key, index);
}

function normalizeJob(payload) {
  return VM.normalizeJob(payload, state.currentJobId);
}

function statusLabel(status) {
  const labels = {
    ready: "可处理",
    queued: "排队中",
    running: "处理中",
    succeeded: "已通过",
    verified: "已通过",
    partial_failed: "部分成功",
    failed: "失败",
    no_op: "无需修改，已跳过",
    no_tower: "无需优化",
    generated: "已生成",
    generating: "生成中",
    exported: "已导出",
    interrupted: "已中断",
    cancelled: "已取消",
  };
  return labels[status] || "状态未知";
}

function statusTone(status) {
  if (["succeeded", "verified", "ready", "exported"].includes(status)) return "success";
  if (["failed", "interrupted", "cancelled"].includes(status)) return "danger";
  if (["partial_failed", "no_op", "no_tower"].includes(status)) return "warning";
  return "neutral";
}

function applySyncDefaults(settings, force = false) {
  const targets = new Set(settings.defaultSyncTargets || []);
  all("input[name='default_sync_targets']").forEach(input => { input.checked = targets.has(input.value); });
  if (!force && state.syncDefaultsInitialized) return;
  all("input[name='sync_targets']").forEach(input => {
    const confirmed = input.value === "tile" ? settings.tileConfirmed : settings.templateConfirmed;
    input.checked = targets.has(input.value) && confirmed;
  });
  state.syncDefaultsInitialized = true;
}

function populatePaths(settings, forceSyncDefaults = false) {
  const tile = settings.tileRoot || "尚未绑定";
  const template = settings.templateRoot || "尚未绑定";
  byId("sync-tile-path").textContent = tile;
  byId("sync-template-path").textContent = template;
  byId("binding-tile-path").textContent = tile;
  byId("binding-template-path").textContent = template;
  byId("settings-tile-path").value = settings.tileRoot;
  byId("settings-template-path").value = settings.templateRoot;
  if (byId("settings-config-root")) byId("settings-config-root").value = settings.configRoot || "";
  byId("settings-naming-rule").value = settings.namingRule;
  byId("settings-increase-naming-rule").value = settings.increaseNamingRule;
  byId("settings-export-directory").value = settings.exportRoot;
  if (!byId("export-naming-rule").dataset.edited) byId("export-naming-rule").value = currentNamingRule();
  byId("export-directory").value = settings.exportRoot;
  const confirmed = settings.tileConfirmed && settings.templateConfirmed;
  setStatusPill(byId("settings-binding-status"), confirmed ? "已确认" : "等待确认", confirmed ? "success" : "warning");
  byId("sync-tile").disabled = !settings.tileConfirmed;
  byId("sync-template").disabled = !settings.templateConfirmed;
  applySyncDefaults(settings, forceSyncDefaults);
  updateImportControls();
}

function candidateChoice(kind) {
  return document.querySelector(`input[name="${kind}_candidate"]:checked`)?.value || "";
}

function renderPathDiscovery(settings) {
  const notice = byId("path-discovery-notice");
  const partial = settings.discoveryTimedOut || settings.discoveryLimitReached;
  notice.hidden = !partial;
  if (partial) {
    notice.textContent = settings.discoveryTimedOut
      ? "路径搜索已超时，当前候选可能不完整，请人工核对。"
      : "路径搜索已达数量上限，请人工核对或在设置中填写。";
  }
  const definitions = [
    ["template", "模板仓", settings.candidates.template, settings.templateRoot],
    ["tile", "Tile 仓", settings.candidates.tile, settings.tileRoot],
  ];
  byId("path-candidate-list").innerHTML = definitions.map(([kind, label, candidates, current]) => {
    if (!candidates.length) return `<fieldset><legend>${label}</legend><p class="empty-copy">未自动发现，请在设置中选择目录。</p></fieldset>`;
    return `<fieldset><legend>${label}${candidates.length > 1 ? ` · ${candidates.length} 个候选` : ""}</legend>${candidates.map((path, index) => {
      const checked = path === current || (!current && candidates.length === 1 && index === 0);
      return `<label><input type="radio" name="${kind}_candidate" value="${escapeHTML(path)}" ${checked ? "checked" : ""}><span>${escapeHTML(path)}</span></label>`;
    }).join("")}</fieldset>`;
  }).join("");
}

async function loadSettings(showBinding = true) {
  try {
    const payload = await apiRequest("/api/settings");
    state.settings = normalizeSettings(payload);
    state.settings.tileRoot = VM.resolveCandidate(state.settings.candidates.tile, state.settings.tileRoot);
    state.settings.templateRoot = VM.resolveCandidate(state.settings.candidates.template, state.settings.templateRoot);
    populatePaths(state.settings);
    renderPathDiscovery(state.settings);
    if (state.settings.loadError) {
      showToast("设置文件读取失败，需要在保存时选择恢复。", "error");
    }
    if (showBinding && !sessionStorage.getItem("tower.pathsDeferred") && !(state.settings.tileConfirmed || state.settings.templateConfirmed)) {
      openDialog(byId("binding-dialog"));
    }
  } catch (error) {
    showToast(`读取设置失败：${error.message}`, "error");
  }
}

async function initialize() {
  try {
    const bootstrap = asObject(await apiRequest("/api/bootstrap"));
    state.bootstrap = bootstrap;
    state.csrfToken = asText(bootstrap.csrf_token);
    setServiceStatus("is-online", "本地服务已连接");
    if (safeStorage.failed) showToast("浏览器本地存储不可用，本次仍可正常操作，刷新后不会自动恢复当前任务。", "error");
    renderHistory();
    await loadSettings(true);
    if (state.currentJobId) {
      await loadJob(state.currentJobId, false);
      if (state.currentJob && !TERMINAL_JOB_STATES.has(state.currentJob.status)) schedulePoll();
    }
  } catch (error) {
    setServiceStatus("is-offline", "本地服务不可用");
    showToast(`无法连接本地服务：${error.message}`, "error");
  }
}

function updateSelectedFiles(files, append = true) {
  if (state.importBusy) return;
  state.selectedFiles = append ? [...state.selectedFiles, ...files] : [...files];
  state.importId = "";
  resetImportSession();
  const list = byId("file-list");
  if (!state.selectedFiles.length) {
    list.innerHTML = '<li class="empty-row">尚未选择模板。</li>';
    updateImportControls();
    return;
  }
  list.innerHTML = state.selectedFiles.map((file, index) => `
    <li>
      <span class="file-name">${escapeHTML(file.name)} <span class="file-meta">· ${formatBytes(file.size)}</span></span>
      <button class="remove-file" type="button" data-remove-file="${index}" aria-label="移除 ${escapeHTML(file.name)}">移除</button>
    </li>`).join("");
  updateImportControls();
}

function templateIds() {
  return VM.parseTemplateIds(byId("template-ids").value);
}

function resetImportSession() {
  state.importSession = null;
  byId("import-progress").hidden = true;
  byId("import-results").hidden = true;
}

function setImportBusy(busy) {
  state.importBusy = busy;
  all("[data-import-mode], #template-ids, #source-repository, #template-files, .remove-file", byId("import-form"))
    .forEach((input) => { input.disabled = busy; });
  byId("drop-zone").toggleAttribute("aria-disabled", busy);
  byId("import-form").setAttribute("aria-busy", String(busy));
  updateImportControls();
}

function renderImportProgress(progress) {
  byId("import-progress").hidden = false;
  byId("import-progress-bar").max = progress.total;
  byId("import-progress-bar").value = progress.completed;
  byId("import-progress-label").textContent = `已读取 ${progress.completed} / ${progress.total} 个模板`;
  byId("import-progress-detail").textContent = progress.phase === "merging"
    ? "正在合并清单与检查跨批重复"
    : progress.phase === "complete" ? "已合并为一个批次" : `内部第 ${progress.batch} / ${progress.totalBatches} 批`;
}

function updateImportControls() {
  const source = byId("source-repository").value;
  const path = source === "tile" ? state.settings.tileRoot : state.settings.templateRoot;
  const confirmed = source === "tile" ? state.settings.tileConfirmed : source === "template" && state.settings.templateConfirmed;
  const count = templateIds().length;
  byId("template-id-count").textContent = `已输入 ${count} 项`;
  all("[data-import-workspace-mode]").forEach(button => {
    const mode = button.dataset.importWorkspaceMode;
    button.hidden = state.mode === "ordinary_add" ? mode !== "ordinary_add" : mode === "ordinary_add";
    button.disabled = state.importBusy || (state.importMode === "ids" ? !count || !confirmed : !state.selectedFiles.length)
      || Boolean(state.importSession?.workspaceMode && state.importSession.workspaceMode !== mode);
    button.innerHTML = `${state.importBusy && state.mode === mode ? "正在解析…" : mode === "ordinary_add" ? "解析模板" : `去${mode === "increase" ? "增加" : "减少"}塔`}${icon("arrow")}`;
  });
  byId("import-entry-actions").classList.toggle("is-single", state.mode === "ordinary_add");
  byId("source-status-label").textContent = state.importMode === "files" ? "上传文件只读" : confirmed ? "源模板只读" : "请先确认模板库路径";
  byId("source-path-label").textContent = state.importMode === "files" ? "不需要仓库路径" : path || "在设置中绑定读取来源";
  byId("source-path-label").title = path || "";
}

function setImportMode(mode) {
  if (state.importBusy) return;
  if (state.importMode !== mode) { state.importId = ""; resetImportSession(); }
  state.importMode = mode;
  all("[data-import-mode]").forEach((button) => {
    const selected = button.dataset.importMode === mode;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  byId("import-ids-panel").hidden = mode !== "ids";
  byId("import-files-panel").hidden = mode !== "files";
  byId("import-results").hidden = true;
  clearFeedback("import-feedback");
  updateImportControls();
}

async function importTemplates(event) {
  event.preventDefault();
  if (state.importBusy) return;
  byId("import-results").hidden = true;
  clearFeedback("import-feedback");
  const form = event.currentTarget;
  clearInvalid(form);
  if (state.importMode === "ids" && !templateIds().length) {
    setInvalid(byId("template-ids"), "请先输入至少一个模板 ID。");
    focusFirstInvalid(form);
    return;
  }
  if (state.importMode === "files" && !state.selectedFiles.length) {
    setInvalid(byId("template-files"), "请先选择至少一个模板文件。");
    focusFirstInvalid(form);
    return;
  }
  const source = byId("source-repository").value;
  const sourceConfirmed = source === "tile" ? state.settings.tileConfirmed : source === "template" && state.settings.templateConfirmed;
  if (state.importMode === "ids" && !sourceConfirmed) {
    setInvalid(byId("source-repository"), "请在设置中确认有效的读取来源路径。");
    focusFirstInvalid(form);
    return;
  }
  const mode = state.importSession?.workspaceMode || event.submitter?.dataset.importWorkspaceMode || state.mode;
  const epoch = ++state.importEpoch;
  const controller = new AbortController();
  state.importController = controller;
  try {
    setWorkspaceMode(mode);
    setImportBusy(true);
    const sourcePath = source === "tile" ? state.settings.tileRoot : state.settings.templateRoot;
    if (state.importSession && state.importMode === "ids" && state.importSession.sourcePath !== sourcePath) resetImportSession();
    if (!state.importSession) {
      state.importSession = window.TowerReducerBatchImport.createSession({
        items: state.importMode === "ids" ? templateIds() : state.selectedFiles,
        mode: state.importMode, source, limits: state.bootstrap.limits});
      state.importSession.sourcePath = sourcePath;
      state.importSession.workspaceMode = mode;
    }
    const result = await window.TowerReducerBatchImport.run(state.importSession, {
      request: apiRequest, signal: controller.signal,
      onProgress: (progress) => { if (epoch === state.importEpoch) renderImportProgress(progress); },
    });
    if (epoch !== state.importEpoch) return;
    if (state.importMode === "ids" && sourcePath !== (source === "tile" ? state.settings.tileRoot : state.settings.templateRoot)) {
      resetImportSession();
      throw new Error("模板库路径已修改，请使用新路径重新解析模板");
    }
    const imported = normalizeImported(result);
    state.importSession = null;
    state.importId = imported.importId;
    state.importedFiles = imported.files;
    if (mode === "ordinary_add") state.ordinaryOverrides = {};
    else {state.overrides = {};state.increaseOverrides = {};}
    const ready = imported.files.filter((row) => ["ready", "no_tower"].includes(row.status) && !row.duplicateOf && !row.hasBlockingIssues);
    const failed = imported.files.filter((row) => row.status === "failed" || row.hasBlockingIssues);
    if (!ready.length) {
      showFeedback("import-feedback", "没有可用于生成的模板，请根据逐文件错误修正后重新导入。", "error");
      renderImportResponse(imported.files);
      return;
    }
    renderImportResponse(imported.files);
    renderConfigurationList(ready);
    const issues = imported.files.filter((row) => row.status === "failed" || row.hasBlockingIssues || row.duplicateOf);
    byId("import-issues-summary").hidden = !issues.length;
    byId("import-issues-title").textContent = `${failed.length} 项未导入${issues.length > failed.length ? ` · ${issues.length - failed.length} 项重复已合并` : ""}`;
    byId("import-issues-list").innerHTML = issues.map((row) => `<li>${escapeHTML(row.name)}：${escapeHTML(row.duplicateOf ? "内容重复，已合并" : asText(row.error.message, "结构检查未通过"))}</li>`).join("");
    showStep(2);
  } catch (error) {
    if (epoch !== state.importEpoch || error.name === "AbortError") return;
    const completed = state.importSession?.completed || 0;
    const total = state.importSession?.total || 0;
    const retry = total ? `已读取 ${completed} / ${total} 个，已完成部分仍保留。点击“去${mode === "increase" ? "增加" : "减少"}塔”继续解析未完成部分。` : "";
    showFeedback("import-feedback", `解析未完成：${error.message}${retry ? ` ${retry}` : ""}`, "error");
  } finally {
    if (epoch === state.importEpoch) {
      state.importController = null;
      setImportBusy(false);
    }
  }
}

function renderImportResponse(files) {
  byId("import-results").hidden = false;
  byId("import-results").innerHTML = `<ul class="file-list">${files.map((file) => {
    const usable = ["ready", "no_tower"].includes(file.status) && !file.hasBlockingIssues;
    const warning = usable && file.hasWarnings;
    const tone = file.duplicateOf ? "neutral" : usable ? (file.status === "no_tower" || warning ? "warning" : "success") : "danger";
    const detail = file.duplicateOf
      ? "内容重复，已由服务端按 SHA 去重"
      : usable
        ? `内部 TemplateId：${file.templateId} · ${file.tileCount} 张 Tile`
        : asText(file.error.message, file.hasBlockingIssues ? "模板结构异常，已阻止生成" : "无法解析");
    const label = file.duplicateOf
      ? "已去重"
      : file.hasBlockingIssues
        ? "结构异常"
        : file.status === "no_tower"
          ? state.mode === "increase" ? "无塔·可新增塔" : "无塔·生成改 ID 副本"
          : warning
            ? "可继续·有提醒"
            : usable
              ? "检查通过"
              : "检查失败";
    const inspection = VM.describeImportInspection(file);
    const facts = inspection.facts.map((fact) => `<span>${escapeHTML(fact)}</span>`).join("");
    const issues = inspection.issues.length
      ? `<ul class="inspection-issues">${inspection.issues.map((issue) => `<li class="${issue.severity === "error" ? "is-error" : "is-warning"}">${escapeHTML(issue.message)}</li>`).join("")}</ul>`
      : '<p class="file-meta">未发现结构异常或提醒。</p>';
    const inspectionDetails = file.hasInspection
      ? `<details class="import-inspection"${inspection.issues.length ? " open" : ""}><summary>查看模板检查依据</summary><div class="inspection-facts">${facts}</div>${issues}</details>`
      : "";
    return `<li><div class="file-result-main"><strong>${escapeHTML(file.name)}</strong><p class="file-meta">${escapeHTML(detail)}</p>${inspectionDetails}</div><span class="status-pill ${tone}">${icon(usable ? "status" : "warning")}<span>${escapeHTML(label)}</span></span></li>`;
  }).join("")}</ul>`;
}

function renderConfigurationList(files) {
  byId("template-count-pill").textContent = `${files.length} 个模板`;
  byId("template-config-list").innerHTML = files.length ? files.map((file) => {
    const inspection = VM.describeImportInspection(file);
    const warningCounts = new Map();
    inspection.issues.forEach((issue) => warningCounts.set(issue.message, (warningCounts.get(issue.message) || 0) + 1));
    const warnings = [...warningCounts].map(([message, count]) => `${message}${count > 1 ? `（${count}处）` : ""}`).join("；");
    return `<tr data-template-config="${escapeHTML(file.fileId)}"><td><strong>${escapeHTML(file.templateId)}</strong><span class="file-meta source-facts">${file.tileCount} 张 · A ${file.aCount} / B ${file.bCount} · ${file.towerCount} 堆塔</span><details class="import-inspection"><summary>结构明细${inspection.issues.length ? ` · ${inspection.issues.length} 条提醒` : ""}</summary><div class="inspection-facts">${inspection.facts.map((fact) => `<span>${escapeHTML(fact)}</span>`).join("")}</div>${warnings ? `<p class="inspection-warning">${escapeHTML(warnings)}</p>` : ""}</details></td><td>${file.ratio === null ? "—" : `${(file.ratio * 100).toFixed(1)}%`}</td><td><span class="plan-mode-label">使用批次默认</span><span class="plan-summary file-meta"></span></td><td><div class="row-actions"><button class="row-action" type="button" data-preview-import="${escapeHTML(file.fileId)}">查看原模板</button><button class="row-action" type="button" data-override-mode="custom" data-file-id="${escapeHTML(file.fileId)}">单独设置</button></div></td></tr>`;
  }).join("") : '<tr><td colspan="4" class="table-empty">解析模板后显示清单。</td></tr>';
  renderTowerFields(files);
  showBatchEditor();
  updateParameterSummaries();
}

function currentNamingRule(mode = state.mode) {
  if (mode === "ordinary_add") return "moreB{原模板ID}";
  return mode === "increase" ? asText(state.settings.increaseNamingRule, "moreT{原模板ID}") : asText(state.settings.namingRule, "lessT{原模板ID}");
}

function setWorkspaceMode(mode) {
  if (!["reduce", "increase", "ordinary_add"].includes(mode)) return;
  window.towerIncreasePreview?.cancel();
  clearTimeout(state.increaseDraftTimer);
  window.towerIncreaseDraft?.cancel();
  state.mode = mode;
  byId("flow-one-title").textContent = mode === "ordinary_add" ? "增加Btile工具" : "导入关卡模板";
  byId("flow-two-title").textContent = mode === "ordinary_add" ? "配置非塔区域新增 Tile" : mode === "increase" ? "配置新增塔与摆放" : "配置塔保留与补牌规则";
  if (!byId("export-naming-rule").dataset.edited) byId("export-naming-rule").value = currentNamingRule();
  invalidatePlans();
  showBatchEditor();
  updateImportControls();
}

function renderOrdinaryEditor() {
  const fileId = state.activeOverride;
  const file = state.importedFiles.find(row => row.fileId === fileId);
  byId("batch-parameter-fields").hidden = true;
  byId("parameter-override-editor").hidden = true;
  byId("advanced-tower-card").hidden = true;
  byId("increase-parameter-fields").hidden = true;
  let fields = byId("ordinary-parameter-fields");
  if (!fields) {
    fields = document.createElement("div");fields.id = "ordinary-parameter-fields";
    byId("parameter-editor-title").closest(".card").append(fields);
  }
  fields.hidden = false;
  byId("parameter-editor-title").textContent = file ? file.templateId : "批次默认参数";
  byId("restore-batch-parameters").hidden = !file;
  ORDINARY.mountEditor(fields, {prefix:"ordinary-config", options:file ? state.ordinaryOverrides[fileId] : state.ordinaryDefaults,
    onChange(next) {
      if (file) state.ordinaryOverrides[fileId] = next;else state.ordinaryDefaults = next;
      invalidatePlans();updateParameterSummaries();
    }, onPreview:file ? next => openTemplatePreview(fileId, "ordinary", next) : null,
  });
  updateParameterSummaries();
}

function renderIncreaseEditor() {
  clearTimeout(state.increaseDraftTimer);
  window.towerIncreaseDraft?.cancel();
  const fileId = state.activeOverride;
  const file = state.importedFiles.find(row => row.fileId === fileId);
  byId("batch-parameter-fields").hidden = true;
  byId("parameter-override-editor").hidden = true;
  byId("advanced-tower-card").hidden = true;
  byId("increase-parameter-fields").hidden = false;
  byId("parameter-editor-title").textContent = file ? file.templateId : "批次默认参数";
  byId("restore-batch-parameters").hidden = !file;
  const options = file ? state.increaseOverrides[fileId] : state.increaseDefaults;
  INCREASE.mountEditor(byId("increase-parameter-fields"), {prefix: "increase-config", options,
    onChange(next) {
      if (file) state.increaseOverrides[fileId] = next; else state.increaseDefaults = next;
      invalidatePlans();updateParameterSummaries();
      if (file && !byId("increase-preview-dialog").open) {
        clearTimeout(state.increaseDraftTimer);
        window.towerIncreaseDraft.configure({import_id: state.importId, file_id: fileId, increase: next});
        state.increaseDraftTimer = setTimeout(() => window.towerIncreaseDraft.preview(), 350);
      }
    },
    onPreview: file ? next => openIncreasePreview(fileId, next) : null,
  });
  if (file) {
    const status = document.createElement("p");status.id = "increase-draft-status";status.className = "field-help";status.setAttribute("role", "status");status.setAttribute("aria-live", "polite");
    status.textContent = "修改参数后自动试算；打开预览查看两侧阵型。";
    byId("increase-parameter-fields").append(status);
  }
  updateParameterSummaries();
}

function openIncreasePreview(fileId, options = null) {
  const file = state.importedFiles.find(row => row.fileId === fileId);
  if (!file || !state.importId) return showToast("请先重新导入模板。", "error");
  clearTimeout(state.increaseDraftTimer);
  window.towerIncreaseDraft?.cancel();
  window.towerIncreasePreview.open({label: file.templateId, import_id: state.importId, file_id: fileId,
    options: options || state.increaseOverrides[fileId] || state.increaseDefaults,
    onChange(next) {state.increaseOverrides[fileId] = next;state.activeOverride = fileId;renderIncreaseEditor();},
  });
}

function parameterSummary(options) {
  if (options.mode === "ordinary_add") return ORDINARY.summary(options.ordinary_add);
  if (options.mode === "increase") return INCREASE.summary(options.increase);
  const strategy = {top_first: "顶层优先", bottom_first: "底层优先", balanced: "五五开", seeded_random: "随机"}[options.strategy] || "顶层优先";
  const ratio = options.ratio === 0 ? "无塔" : `${Number(((options.min_ratio ?? 0) * 100).toFixed(3))}% ≤ 塔占比 <${Number((options.ratio * 100).toFixed(3))}%`;
  const replenish = options.replenish_type && options.replenish_type !== "auto" ? ` · ${{A:"全部补 A",B:"全部补 B",majority_A:"以 A 为主",majority_B:"以 B 为主",preserve:"原牌类型一致"}[options.replenish_type] || ""}` : "";
  return `${ratio}${replenish} · ${strategy} · 单层${options.max_tiles_per_layer}张 · 最多新增${options.max_added_layers}层`;
}

function renderReplenishGuidance(row, type, context) {
  const recommendation = VM.replenishRecommendation(row);
  const warning = VM.replenishTypeWarning(row, type);
  return `<p class="field-help">${escapeHTML(recommendation.message)}</p>${recommendation.type ? `<button class="text-button" type="button" data-apply-replenish="${escapeHTML(context)}">采用推荐</button>` : ""}${warning ? `<p class="field-help inspection-warning" role="status">${escapeHTML(warning)}</p>` : ""}`;
}

function updateReplenishGuidance(context) {
  if (context === "override") {
    const fileId = state.activeOverride;
    const row = state.importedFiles.find((file) => file.fileId === fileId);
    const target = byId(`override-replenish-guidance-${fileId}`);
    if (row && target) target.innerHTML = renderReplenishGuidance(row, state.overrides[fileId]?.replenish_type, context);
  } else {
    const row = state.currentJob?.templates.find((item) => item.key === state.rerunKey);
    if (row) byId("rerun-replenish-guidance").innerHTML = renderReplenishGuidance(row, byId("rerun-replenish-type").value, "rerun");
  }
}

function applyReplenishRecommendation(context) {
  const row = context === "override" ? state.importedFiles.find((file) => file.fileId === state.activeOverride)
    : state.currentJob?.templates.find((item) => item.key === state.rerunKey);
  const recommendation = VM.replenishRecommendation(row);
  if (!recommendation.type) return;
  if (context === "override") {
    const fileId = state.activeOverride;
    if (!state.overrides[fileId]) return;
    state.overrides[fileId].replenish_type = recommendation.type;
    byId(`override-replenish-${fileId}`).value = recommendation.type;
    updateParameterSummaries();
    invalidatePlans();
  } else {
    byId("rerun-replenish-type").value = recommendation.type;
  }
  updateReplenishGuidance(context);
}

function updateParameterSummaries() {
  const defaults = collectDefaults();
  byId("batch-summary-text").textContent = parameterSummary(defaults);
  all("[data-template-config]").forEach((item) => {
    const fileId = item.dataset.templateConfig;
    const override = state.mode === "ordinary_add" ? state.ordinaryOverrides[fileId] : state.mode === "increase" ? state.increaseOverrides[fileId] : state.overrides[fileId];
    const custom = Boolean(override);
    item.querySelector(".plan-mode-label").textContent = custom ? "单独设置" : "使用批次默认";
    item.querySelector(".plan-summary").textContent = custom ? parameterSummary(state.mode === "ordinary_add" ? {...defaults, ordinary_add:override} : state.mode === "increase" ? {...defaults, increase:override} : {...defaults, ...override}) : "";
    item.querySelector(".plan-summary").hidden = !custom;
    item.classList.toggle("is-editing", fileId === state.activeOverride);
  });
}

function showBatchEditor() {
  state.activeOverride = "";
  if (byId("ordinary-parameter-fields")) byId("ordinary-parameter-fields").hidden = state.mode !== "ordinary_add";
  if (state.mode === "ordinary_add") return renderOrdinaryEditor();
  if (state.mode === "increase") return renderIncreaseEditor();
  if (byId("increase-parameter-fields")) byId("increase-parameter-fields").hidden = true;
  byId("batch-parameter-fields").hidden = false;
  byId("parameter-override-editor").hidden = true;
  byId("parameter-override-editor").innerHTML = "";
  byId("parameter-editor-title").textContent = "批次默认参数";
  byId("restore-batch-parameters").hidden = true;
  byId("advanced-tower-card").hidden = byId("tower-mode").value !== "advanced";
  updateParameterSummaries();
}

function renderTowerFields(files) {
  const groups = files.filter((file) => file.towerStacks.length);
  byId("tower-fields").innerHTML = groups.length ? groups.map((file) => `
    <div class="tower-field-group">
      <strong>${escapeHTML(file.templateId)}</strong>
      <p class="field-help">原塔堆：${escapeHTML(file.towerStacks.join(" / "))}</p>
      <div class="override-fields">${file.towerStacks.map((count, index) => `
        <div class="field">
          <label for="tower-${escapeHTML(file.fileId)}-${index}">第 ${index + 1} 堆</label>
          <input id="tower-${escapeHTML(file.fileId)}-${index}" type="number" min="0" max="${count}" value="${count}" data-tower-file="${escapeHTML(file.fileId)}" data-tower-index="${index}" aria-describedby="tower-help-${escapeHTML(file.fileId)}-${index}">
          <p id="tower-help-${escapeHTML(file.fileId)}-${index}">原数量 ${count}</p>
        </div>`).join("")}</div>
    </div>`).join("") : '<p class="empty-copy">本批次没有可配置的塔堆。</p>';
}

function setOverrideMode(fileId, mode) {
  if (state.mode === "ordinary_add") {
    if (mode === "custom") {
      state.activeOverride = fileId;
      if (!state.ordinaryOverrides[fileId]) state.ordinaryOverrides[fileId] = structuredClone(state.ordinaryDefaults);
    } else {delete state.ordinaryOverrides[fileId];state.activeOverride = "";}
    renderOrdinaryEditor();invalidatePlans();return;
  }
  if (state.mode === "increase") {
    if (mode === "custom") {
      state.activeOverride = fileId;
      if (!state.increaseOverrides[fileId]) state.increaseOverrides[fileId] = structuredClone(state.increaseDefaults);
    } else {
      delete state.increaseOverrides[fileId];
      state.activeOverride = "";
    }
    renderIncreaseEditor();
    updateParameterSummaries();
    return;
  }
  const item = document.querySelector(`[data-template-config="${CSS.escape(fileId)}"]`);
  if (!item) return;
  const fields = byId("parameter-override-editor");
  if (mode === "custom") {
    state.activeOverride = fileId;
    state.overrides[fileId] = state.overrides[fileId] || {
      min_ratio: Number(byId("min-target-ratio").value) / 100,
      ratio: Number(byId("target-ratio").value) / 100,
      replenish_type: byId("batch-replenish-type")?.value || "auto",
      strategy: byId("placement-strategy").value,
      max_tiles_per_layer: Number(byId("layer-limit").value),
      max_added_layers: Number(byId("max-new-layers").value),
      seed: Number(byId("random-seed").value),
    };
    fields.hidden = false;
    byId("batch-parameter-fields").hidden = true;
    byId("advanced-tower-card").hidden = true;
    byId("parameter-editor-title").textContent = state.importedFiles.find((file) => file.fileId === fileId)?.templateId || "单独设置";
    byId("restore-batch-parameters").hidden = false;
    fields.innerHTML = `
      <div class="field"><label for="override-min-ratio-${escapeHTML(fileId)}">塔占比下限</label><div class="input-suffix"><input id="override-min-ratio-${escapeHTML(fileId)}" type="number" min="0" max="100" step="0.1" value="${(state.overrides[fileId].min_ratio ?? 0) * 100}" data-override-field="min_ratio" data-value-kind="ratio" data-file-id="${escapeHTML(fileId)}" aria-describedby="override-min-ratio-help-${escapeHTML(fileId)}"><span>%</span></div><p id="override-min-ratio-help-${escapeHTML(fileId)}">结果可等于下限；清空塔时上下限都填 0。</p></div>
      <div class="field"><label for="override-ratio-${escapeHTML(fileId)}">塔占比上限</label><div class="input-suffix"><input id="override-ratio-${escapeHTML(fileId)}" type="number" min="0" max="100" step="0.1" value="${state.overrides[fileId].ratio * 100}" data-override-field="ratio" data-value-kind="ratio" data-file-id="${escapeHTML(fileId)}" aria-describedby="override-ratio-help-${escapeHTML(fileId)}"><span>%</span></div><p id="override-ratio-help-${escapeHTML(fileId)}">可填 0–100%，且不能高于该原模板塔占比；0 为去塔，其余结果严格小于上限。</p></div>
      <div class="field span-two"><label for="override-replenish-${escapeHTML(fileId)}">非塔补牌类型</label><select id="override-replenish-${escapeHTML(fileId)}" data-override-field="replenish_type" data-file-id="${escapeHTML(fileId)}" aria-describedby="override-replenish-help-${escapeHTML(fileId)}"><option value="auto">自动</option><option value="majority_A">以 A Tile 为主</option><option value="majority_B">以 B Tile 为主</option><option value="preserve">与原搬出牌类型一致</option><option value="A">全部补 A</option><option value="B">全部补 B</option></select><p id="override-replenish-help-${escapeHTML(fileId)}">“为主”会用少量另一类补齐三张成组，并邻近原非塔同类。</p><div id="override-replenish-guidance-${escapeHTML(fileId)}" class="replenish-guidance"></div></div>
      <div class="field"><label for="override-strategy-${escapeHTML(fileId)}">单独补牌策略</label><select id="override-strategy-${escapeHTML(fileId)}" data-override-field="strategy" data-file-id="${escapeHTML(fileId)}"><option value="top_first">顶层优先</option><option value="bottom_first">底层优先</option><option value="balanced">五五开</option><option value="seeded_random">随机</option></select></div>
      <div class="field"><label for="override-layer-${escapeHTML(fileId)}">单层非塔 Tile 上限</label><input id="override-layer-${escapeHTML(fileId)}" type="number" min="1" step="1" value="${state.overrides[fileId].max_tiles_per_layer}" data-override-field="max_tiles_per_layer" data-value-kind="number" data-file-id="${escapeHTML(fileId)}"></div>
      <div class="field"><label for="override-levels-${escapeHTML(fileId)}">最多新增层数</label><input id="override-levels-${escapeHTML(fileId)}" type="number" min="0" step="1" value="${state.overrides[fileId].max_added_layers}" data-override-field="max_added_layers" data-value-kind="number" data-file-id="${escapeHTML(fileId)}"></div>
      <div class="field" data-override-seed ${state.overrides[fileId].strategy !== "seeded_random" ? "hidden" : ""}><label for="override-seed-${escapeHTML(fileId)}">随机种子</label><input id="override-seed-${escapeHTML(fileId)}" type="number" step="1" value="${state.overrides[fileId].seed}" data-override-field="seed" data-value-kind="number" data-file-id="${escapeHTML(fileId)}"></div>`;
    const select = fields.querySelector("[data-override-field='strategy']");
    if (select) select.value = state.overrides[fileId].strategy;
    byId(`override-replenish-${fileId}`).value = state.overrides[fileId].replenish_type || "auto";
    updateReplenishGuidance("override");
  } else {
    delete state.overrides[fileId];
    showBatchEditor();
  }
  updateParameterSummaries();
}

function collectRetainedCounts() {
  const retainedCounts = {};
  all("[data-tower-file]").forEach((input) => {
    const fileId = input.dataset.towerFile;
    retainedCounts[fileId] = retainedCounts[fileId] || [];
    retainedCounts[fileId][Number(input.dataset.towerIndex)] = Number(input.value);
  });
  return retainedCounts;
}

function collectDefaults() {
  if (state.mode === "ordinary_add") return {mode:"ordinary_add", ordinary_add:structuredClone(state.ordinaryDefaults), naming_rule:currentNamingRule()};
  if (state.mode === "increase") return {mode: "increase", increase: INCREASE.normalizeOptions(state.increaseDefaults), naming_rule: currentNamingRule()};
  return {
    min_ratio: Number(byId("min-target-ratio").value) / 100,
    ratio: Number(byId("target-ratio").value) / 100,
    replenish_type: byId("batch-replenish-type")?.value || "auto",
    retained_counts: null,
    strategy: byId("placement-strategy").value,
    max_tiles_per_layer: Number(byId("layer-limit").value),
    max_added_layers: Number(byId("max-new-layers").value),
    seed: Number(byId("random-seed").value || 0),
    naming_rule: asText(state.settings.namingRule, "lessT{原模板ID}"),
  };
}

function collectOverrides() {
  if (state.mode === "ordinary_add") return Object.fromEntries(Object.entries(state.ordinaryOverrides).map(([key, ordinary_add]) => [key, {mode:"ordinary_add", ordinary_add:structuredClone(ordinary_add)}]));
  if (state.mode === "increase") return Object.fromEntries(Object.entries(state.increaseOverrides).map(([key, increase]) => [key, {mode: "increase", increase: INCREASE.normalizeOptions(increase)}]));
  const overrides = Object.fromEntries(Object.entries(state.overrides).map(([key, value]) => [key, {...value}]));
  if (byId("tower-mode").value === "advanced") {
    Object.entries(collectRetainedCounts()).forEach(([fileId, counts]) => {
      overrides[fileId] = {...(overrides[fileId] || {}), retained_counts: counts};
    });
  }
  return overrides;
}

function validateConfiguration(form) {
  clearInvalid(form);
  if (state.mode === "ordinary_add") {
    for (const [fileId, options] of [["",state.ordinaryDefaults],...Object.entries(state.ordinaryOverrides)]) {
      const invalid = ORDINARY.validate(options);
      if (!invalid) continue;
      state.activeOverride = fileId;renderOrdinaryEditor();
      const input = byId("ordinary-parameter-fields").querySelector(`[data-ordinary-field="${invalid.field}"]`);
      setInvalid(input, invalid.message);focusFirstInvalid(form);return false;
    }
    return true;
  }
  if (state.mode === "increase") {
    let error = INCREASE.validate(state.increaseDefaults);
    let invalidOptions = state.increaseDefaults;
    if (error) {state.activeOverride = "";renderIncreaseEditor();}
    for (const [fileId, options] of error ? [] : Object.entries(state.increaseOverrides)) {
      const message = INCREASE.validate(options);
      if (message) {state.activeOverride = fileId;renderIncreaseEditor();error = message;invalidOptions = options;break;}
    }
    if (error) {
      const input = byId("increase-parameter-fields").querySelector(`[data-increase-field="${INCREASE.invalidField(invalidOptions)}"]`);
      const disclosure = input.closest("details");if (disclosure) disclosure.open = true;
      setInvalid(input, error);input.focus();showToast(error, "error");return false;
    }
    return true;
  }
  const minimum = Number(byId("min-target-ratio").value);
  const ratio = Number(byId("target-ratio").value);
  const layer = Number(byId("layer-limit").value);
  const levels = Number(byId("max-new-layers").value);
  for (const [fileId, options] of Object.entries(state.overrides)) {
    const range = VM.validateRatioRangePercent((options.min_ratio ?? minimum / 100) * 100, (options.ratio ?? ratio / 100) * 100);
    if (!range.ok) {
      setOverrideMode(fileId, "custom");
      setInvalid(byId(`override-${range.field === "min_ratio" ? "min-ratio" : "ratio"}-${fileId}`), range.message);
      focusFirstInvalid(form);
      return false;
    }
    const checks = [
      ["replenish_type", "replenish", ["auto", "A", "B", "majority_A", "majority_B", "preserve"].includes(options.replenish_type), "请选择补牌类型。"],
      ["max_tiles_per_layer", "layer", Number.isInteger(options.max_tiles_per_layer) && options.max_tiles_per_layer >= 1, "必须是正整数。"],
      ["max_added_layers", "levels", Number.isInteger(options.max_added_layers) && options.max_added_layers >= 0, "必须是非负整数。"],
      ["seed", "seed", Number.isInteger(options.seed), "随机种子必须是整数。"],
    ];
    const invalid = checks.find(([key, _id, valid]) => options[key] !== undefined && !valid);
    if (invalid) {
      setOverrideMode(fileId, "custom");
      setInvalid(byId(`override-${invalid[1]}-${fileId}`), invalid[3]);
      focusFirstInvalid(form);
      return false;
    }
  }
  const range = VM.validateRatioRangePercent(minimum, ratio);
  if (!range.ok) setInvalid(byId(range.field === "min_ratio" ? "min-target-ratio" : "target-ratio"), range.message);
  if (!Number.isInteger(layer) || layer < 1) setInvalid(byId("layer-limit"), "单层建议上限必须是正整数。");
  if (!Number.isInteger(levels) || levels < 0) setInvalid(byId("max-new-layers"), "最多新增层数必须是非负整数。");
  const invalidBatchSeed = byId("placement-strategy").value === "seeded_random" && !Number.isInteger(Number(byId("random-seed").value));
  if (invalidBatchSeed) setInvalid(byId("random-seed"), "随机种子必须是整数。");
  if (!range.ok || !Number.isInteger(layer) || layer < 1 || !Number.isInteger(levels) || levels < 0 || invalidBatchSeed) {
    showBatchEditor();
    focusFirstInvalid(form);
    showToast("请检查标记的参数。", "error");
    return false;
  }
  for (const file of state.importedFiles || []) {
    if (file.status === "failed" || file.hasBlockingIssues || file.duplicateOf) continue;
    const options = state.overrides[file.fileId] || {};
    const sourceRange = VM.validateRatioRangePercent((options.min_ratio ?? minimum / 100) * 100,
      (options.ratio ?? ratio / 100) * 100, Number.isFinite(file.ratio) ? file.ratio * 100 : null, file.templateId);
    if (sourceRange.ok) continue;
    if (options.ratio !== undefined) {
      setOverrideMode(file.fileId, "custom");
      setInvalid(byId(`override-ratio-${file.fileId}`), sourceRange.message);
    } else {
      showBatchEditor();
      setInvalid(byId("target-ratio"), sourceRange.message);
    }
    focusFirstInvalid(form);
    return false;
  }
  all("[data-override-field='ratio']", form).forEach((input) => {
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 0 || value > 100) setInvalid(input, "单模板塔占比只可设为 0–100%。");
  });
  all("[data-override-field='max_tiles_per_layer']", form).forEach((input) => {
    if (!Number.isInteger(Number(input.value)) || Number(input.value) < 1) setInvalid(input, "单层建议上限必须是正整数。");
  });
  all("[data-override-field='max_added_layers']", form).forEach((input) => {
    if (!Number.isInteger(Number(input.value)) || Number(input.value) < 0) setInvalid(input, "新增层数必须是非负整数。");
  });
  all("[data-override-field='seed']", form).forEach((input) => {
    if (!Number.isInteger(Number(input.value))) setInvalid(input, "随机种子必须是整数。");
  });
  let invalidTowerCount = false;
  if (byId("tower-mode").value === "advanced") {
    all("[data-tower-file]", form).forEach((input) => {
      const value = Number(input.value);
      const maximum = Number(input.max);
      if (!Number.isInteger(value) || value < 0 || value > maximum) {
        invalidTowerCount = true;
        setInvalid(input, `保留数必须是 0–${maximum} 的整数。`);
      }
    });
    if (invalidTowerCount) showBatchEditor();
  }
  if (focusFirstInvalid(form)) {
    showToast("请检查标记的参数。", "error");
    return false;
  }
  return true;
}

function planContextFingerprint() {
  const job = state.currentJob;
  const artifacts = job ? job.templates.flatMap((row) => [
    row.outputSha256,
    ...row.artifacts.map((item) => asText(asObject(item).sha256)),
  ].filter(Boolean)).sort() : [];
  return VM.planFingerprint({
    jobId: state.currentJobId,
    revision: job?.revision || 0,
    artifacts,
    parameters: {rows:job?.templates.map((row) => ({key: row.key, attemptId: row.attemptId, parameters: row.parameters})) || [],selection:[...(state.resultSelection||[])].sort()},
    naming: byId("export-naming-rule")?.value.trim() || "",
    exportRoot: byId("export-directory")?.value.trim() || "",
  });
}

function invalidatePlans(reason = "") {
  state.exportPlan = null;
  state.syncPlan = null;
  state.exportPlanContext = "";
  state.syncPlanContext = "";
  if (reason && byId("export-status")) {
    setStatusPill(byId("export-status"), "需重新预览", "warning");
  }
}

async function createJob(event) {
  if(state.batchCenter){event.preventDefault();return showToast("请等待批量居中完成，再生成模板。","warning");}
  event.preventDefault();
  if (state.jobCreationPending) return;
  if (!state.importId) {
    showToast("导入记录已失效，请返回重新检查模板。", "error");
    showStep(1);
    return;
  }
  if (!validateConfiguration(event.currentTarget)) return;
  const button = byId("generate-button");
  button.disabled = true;
  state.jobCreationPending = true;
  const context = {group:state.workbenchGroup,importId:state.importId};
  let generation;
  try {
    cancelBatchRegeneration();
    generation = state.requestGeneration;
    const payload = await apiRequest("/api/jobs", {
      method: "POST",
      json: {import_id: state.importId, defaults: collectDefaults(), overrides: collectOverrides()},
    });
    const jobId = asText(asObject(payload).job_id);
    if (!jobId) throw new Error("服务端没有返回任务编号。");
    if (generation !== state.requestGeneration || context.group !== state.workbenchGroup || context.importId !== state.importId) {
      showToast("任务已创建，可在运行记录中查看。", "warning");return;
    }
    cancelPolling();
    state.currentJobId = jobId;
    safeStorage.set("tower-reducer-current-job", jobId);
    state.currentJob = normalizeJob(payload);
    applySyncDefaults(state.settings, true);
    invalidatePlans();
    showStep(3);
    renderJob(state.currentJob);
    schedulePoll(100);
  } catch (error) {
    showToast(`创建任务失败：${error.message}`, "error");
  } finally {
    state.jobCreationPending = false;
    button.disabled = false;
  }
}

function calculateProgress(job) {
  if (job.progress.percent > 0) return Math.min(100, Math.max(0, job.progress.percent));
  if (job.progress.total > 0) return Math.round(job.progress.completed / job.progress.total * 100);
  return ["succeeded", "partial_failed"].includes(job.status) && job.templates.length ? 100 : 0;
}

function updateResultSelection(job = state.currentJob) {
  if (!job) return [];
  if (state.resultSelectionJob !== job.id) {
    state.resultSelectionJob = job.id;
    state.resultSelection = [];
  }
  const available = job.templates.filter(row => VM.canRerunTemplate(row));
  const selected = job.templates.filter(row => (state.resultSelection || []).includes(row.key));
  const rerunnable = selected.filter(row => VM.canRerunTemplate(row));
  state.resultSelection = selected.map(row => row.key);
  byId("result-selection-count").textContent = `已选 ${selected.length} 个模板`;
  byId("batch-rerun-button").disabled = !rerunnable.length||Boolean(state.batchCenter);
  byId("clear-result-selection").disabled = !selected.length;
  byId("select-failed-templates").disabled = !available.some(row => ["failed", "interrupted"].includes(row.status));
  const selectAll = byId("select-all-result-templates");
  selectAll.checked = Boolean(available.length && rerunnable.length === available.length);
  selectAll.indeterminate = selected.length > 0 && !selectAll.checked;
  selectAll.disabled = !available.length;
  all("[data-select-result-template]").forEach(input => {input.checked = state.resultSelection.includes(input.dataset.selectResultTemplate);});
  if(state.exportPlanContext&&state.exportPlanContext!==planContextFingerprint())invalidatePlans("勾选范围已变化");
  updateNamingRegenerationControl();
  if(typeof updateBatchCenterControls==="function")updateBatchCenterControls(job);
  return rerunnable;
}

function batchCenterTargets(job=state.currentJob) {
  return (job?.templates||[]).filter(row=>["verified","succeeded","exported"].includes(row.status)&&row.artifacts?.[0]?.id).map(row=>({key:row.key,label:row.templateId&&row.templateId!=="—"?row.templateId:row.outputId&&row.outputId!=="—"?row.outputId:row.key,artifactId:row.artifacts[0].id}));
}

function renderBatchCenterProgress(operation,phase="processing",label="") {
  const percent=phase==="complete"?100:Math.min(99,Math.floor(operation.completed/operation.total*100));
  byId("batch-center-progress-panel").hidden=false;
  byId("batch-center-progress").value=percent;
  byId("batch-center-progress-value").textContent=`${percent}%`;
  byId("batch-center-progress-title").textContent=phase==="complete"?(operation.errors.length?"处理完成，部分项目失败":"居中完成"):phase==="refresh"?"已保存，正在刷新结果":phase==="stopped"?"批量居中已停止":"正在居中新模板";
  const status=byId("batch-center-status");
  status.hidden=false;
  status.classList.toggle("inspection-warning",phase==="complete"&&Boolean(operation.errors.length));
  if(phase==="processing")status.textContent=`已处理 ${operation.completed}/${operation.total} · 正在处理 ${label||"新模板"}（平移、验收并保存），请保持页面开启。`;
  if(phase==="refresh")status.textContent=`已处理 ${operation.completed}/${operation.total} · 正在更新预览与导出状态，请稍候。`;
}

function updateBatchCenterControls(job=state.currentJob) {
  const button=byId("batch-center-button");
  if(!button)return;
  if(state.batchCenterStatusJob!==job?.id)byId("batch-center-progress-panel").hidden=true;
  button.disabled=!batchCenterTargets(job).length||Boolean(state.batchCenter)||Boolean(state.namingRegenerationActive)||translationPending()||Boolean(state.manualDraft)||Boolean(state.exportTask)||!TERMINAL_JOB_STATES.has(job?.status);
  button.setAttribute("aria-busy",String(Boolean(state.batchCenter)));
  button.title="居中当前任务全部已验收的新模板，不受勾选范围影响；只平移，不重新生成";
}

async function centerAllTemplates() {
  const job=state.currentJob,targets=batchCenterTargets(job);
  if(!targets.length||state.batchCenter||state.namingRegenerationActive||translationPending()||state.manualDraft||state.exportTask||!TERMINAL_JOB_STATES.has(job?.status))return;
  const operation={jobId:job.id,generation:state.requestGeneration,completed:0,total:targets.length,centered:0,skipped:0,limited:0,errors:[],cancelled:false};
  state.batchCenter=operation;
  state.batchCenterStatusJob=job.id;
  const current=()=>state.batchCenter===operation&&state.currentJobId===operation.jobId&&state.requestGeneration===operation.generation&&!operation.cancelled;
  const progress=(label="")=>{if(current())renderBatchCenterProgress(operation,"processing",label);};
  invalidatePlans("正在批量居中新模板");updateResultSelection();progress();
  try{
    for(const target of targets){
      if(!current())break;
      progress(target.label);
      try{
        const result=await apiRequest(`/api/jobs/${encodeURIComponent(operation.jobId)}/center/${encodeURIComponent(target.key)}`,{method:"POST",json:{artifact_id:target.artifactId}});
        if(!current())break;
        if(result.skipped||result.centered===false)operation.skipped++;else operation.centered++;
        if(result.centering?.clamped)operation.limited++;
        state.resultPreviewCache?.clear();invalidatePlans("阵型已居中，请重新预览导出或同步");
      }catch(error){if(!current())break;operation.errors.push(`${target.label}：${error.message}`);}
      operation.completed++;progress();
    }
    if(!current())return;
    renderBatchCenterProgress(operation,"refresh");
    const position={x:window.scrollX,y:window.scrollY,table:byId("result-table-body").closest?.(".table-scroll"),tableX:byId("result-table-body").closest?.(".table-scroll")?.scrollLeft||0};
    try{await loadJob(operation.jobId,false,true);}catch(error){operation.errors.push(`刷新结果：${error.message}`);}
    if(!current())return;
    window.scrollTo({left:position.x,top:position.y,behavior:"auto"});if(position.table)position.table.scrollLeft=position.tableX;
    const status=byId("batch-center-status");
    renderBatchCenterProgress(operation,"complete");
    status.textContent=`已处理 ${operation.completed}/${operation.total}：已平移居中 ${operation.centered}，未平移 ${operation.skipped}，失败 ${operation.errors.length}${operation.limited?`（${operation.limited} 个受安全边界限制，已尽量居中）`:''}${operation.errors.length?`。${operation.errors.join('；')}`:'。'}`;
    status.classList.toggle("inspection-warning",Boolean(operation.errors.length));
    showToast(operation.errors.length?"批量居中部分失败，请查看逐项原因。":"批量居中完成，阵型已验收保存。",operation.errors.length?"warning":"success");
  }finally{
    if(state.batchCenter===operation){state.batchCenter=null;if(state.currentJob)updateResultSelection();if(state.currentJobId===operation.jobId&&operation.cancelled){renderBatchCenterProgress(operation,"stopped");byId("batch-center-status").textContent=`已停止批量居中，已处理 ${operation.completed}/${operation.total}；已提交的结果保留。`;}}
  }
}

function openSelectedRerunEditor() {
  const selected = updateResultSelection();
  if (!selected.length) return;
  const modes = new Set(selected.map(row => row.operation || row.parameters.mode || "reduce"));
  if (modes.size > 1) return showToast("请选择同一种工具模式的模板，再批量修改参数。", "warning");
  openRerunEditor(selected[0].key, selected.map(row => row.key));
}

function renderJob(job) {
  updateResultSelection(job);
  const progress = calculateProgress(job);
  const terminal = TERMINAL_JOB_STATES.has(job.status);
  const running = ["queued", "running"].includes(job.status);
  byId("task-panel").hidden = !running;
  byId("task-title").textContent = running ? "正在生成并验收模板" : statusLabel(job.status);
  byId("task-description").textContent = running ? `已完成 ${job.progress.completed}/${job.progress.total} 个模板，请保持工具开启。` : "任务已停止计算，可查看逐模板结果与下一步操作。";
  byId("task-progress").value = progress;
  byId("task-progress-value").textContent = `${progress}%`;
  byId("job-id-label").textContent = job.id || "暂无任务编号";
  document.querySelector(".task-spinner").classList.toggle("is-running", running);
  const currentPhase = job.progress.stage || job.templates.find((row) => !TERMINAL_TEMPLATE_STATES.has(row.status))?.stage || "";
  const currentIndex = PHASE_ORDER.indexOf(currentPhase);
  all("[data-phase]").forEach((item, index) => {
    item.classList.toggle("is-active", running && currentIndex >= 0 && index === currentIndex);
    item.classList.toggle("is-complete", (terminal && job.status !== "failed") || (currentIndex >= 0 && index < currentIndex));
  });
  const logs = job.logs.length ? job.logs : ["等待服务端写入任务日志。"];
  byId("log-list").innerHTML = logs.slice(-12).map((entry) => {
    const row = asObject(entry);
    return `<li>${escapeHTML(asText(row.message, typeof entry === "string" ? entry : JSON.stringify(entry)))}</li>`;
  }).join("");
  renderJobResults(job, state.resultSelection);
  updateNamingRegenerationControl();
  renderHistory();
}

function increaseAdviceContext(row) {
  return VM.planFingerprint({job: state.currentJobId, key: row.key, attempt: row.attemptId,
    source: row.sourceSha256, parameters: row.parameters, advice: row.parameterAdvice});
}

function renderParameterAdvice(row, interactive = false) {
  const advice = VM.parameterAdviceView(row);
  if (!advice.schemes.length) return `<p class="advice-message">${escapeHTML(advice.message)}</p>`;
  const increase = ["increase", "ordinary_add"].includes(row.operation) || ["increase", "ordinary_add"].includes(row.parameters.mode);
  return `<div class="parameter-advice">${advice.schemes.map((scheme, index) => `<section class="advice-scheme" aria-label="方案${index + 1}">
    <p class="advice-title">方案${index === 0 ? "一" : "二"} · ${escapeHTML(scheme.title)}</p>
    <ul>${scheme.adjustments.map((line) => `<li>${escapeHTML(line)}</li>`).join("")}</ul>
    <p class="advice-unchanged">保持不变：${escapeHTML(scheme.unchanged || "其他参数")}</p>
    <p class="advice-outcome">${escapeHTML(scheme.outcome)}</p>
    ${increase ? `<button class="secondary-button" type="button" ${interactive ? `data-fill-advice="${index}"` : `data-apply-increase-advice="${index}" data-advice-template="${escapeHTML(row.key)}" data-advice-context="${escapeHTML(increaseAdviceContext(row))}"`}>一键应用推荐参数</button>`
      : interactive ? `<button class="secondary-button" type="button" data-fill-advice="${index}">填入方案${index === 0 ? "一" : "二"}</button>` : ""}
  </section>`).join("")}</div>`;
}

function renderJobResults(job, selectedKeys = []) {
  const rows = job.templates;
  byId("result-quantity-heading").textContent = rows.some(row => ["increase", "ordinary_add"].includes(row.operation)) ? "Tile 数量变化" : "挪动 Tile";
  const outcomes = VM.summarizeTemplateOutcomes(rows);
  const {passed, failed, noOp: noop} = outcomes;
  byId("metric-input").textContent = String(rows.length || job.progress.total);
  byId("metric-pass").textContent = `${passed} 个通过`;
  byId("metric-fail").textContent = `${failed} 个失败`;
  byId("metric-noop").textContent = `${noop} 个无需优化`;
  setStatusPill(byId("batch-status"), statusLabel(job.status), statusTone(job.status));
  byId("result-table-body").innerHTML = rows.length ? rows.map((row) => {
    const oldRatio = row.oldRatio === null ? null : (row.oldRatio <= 1 ? row.oldRatio * 100 : row.oldRatio);
    const newRatio = row.newRatio === null ? null : (row.newRatio <= 1 ? row.newRatio * 100 : row.newRatio);
    const ratioTransition = `${oldRatio === null ? "—" : `${oldRatio.toFixed(1)}%`} → ${newRatio === null ? "—" : `${newRatio.toFixed(1)}%`}`;
    const ratioText = ratioTransition;
    const artifact = asObject(row.artifacts[0]);
    const download = artifact.id
      ? `<a class="row-action" data-download-artifact href="/api/jobs/${encodeURIComponent(job.id)}/download/${encodeURIComponent(artifact.id)}">单文件下载</a>`
      : "";
    const action = row.stage === "import"
      ? '<button class="row-action" type="button" data-reimport-template>重新导入</button>'
      : VM.canRerunTemplate(row)
      ? `<button class="row-action" type="button" data-rerun-template="${escapeHTML(row.key)}">${["failed", "interrupted"].includes(row.status) ? "单独调整并重新运行" : "修改参数并重新生成"}</button>`
      : row.status === "no_op" && !artifact.id
        ? '<span class="file-meta">不生成新文件</span>'
        : "";
    const parameterAdvice = ["failed", "interrupted", "no_op"].includes(row.status) && row.stage !== "import" ? renderParameterAdvice(row) : "";
    const recovery = ["failed", "interrupted"].includes(row.status) && row.stage !== "import" && !artifact.id && !["increase", "ordinary_add"].includes(row.operation)
      ? `<button class="row-action" type="button" data-manual-recovery="${escapeHTML(row.key)}">删除所有塔并手动补牌</button>` : "";
    const issues = row.verification.issues.map((issue) => asText(asObject(issue).message)).filter(Boolean);
    const coordinatePreview = row.destinations.slice(0, 6).map((raw) => {
      const point = asObject(raw);
      return `(${asNumber(point.x)}, ${asNumber(point.y)}, ${asNumber(point.z)})`;
    }).join("、");
    const verificationFacts = Object.keys(row.verification.facts).length ? JSON.stringify(row.verification.facts) : "";
    const attemptTrail = row.attempts.map((raw, index) => {
      const attempt = asObject(raw);
      return `第 ${asNumber(attempt.attempt_id ?? attempt.id, index + 1)} 次 ${statusLabel(asText(attempt.status))}`;
    }).join("、");
    const movement = VM.tileMovementView(row);
    const detailLines = [
      row.outputId !== "—" ? `新 ID：${row.outputId}` : "",
      row.manualEdit && Object.keys(row.manualEdit).length ? `已手动微调：新增 ${row.manualEdit.added} 张 / 删除 ${row.manualEdit.deleted} 张，以当前验收产物为准` : "",
      row.retained.length ? `塔堆保留：${row.retained.join(" / ")}` : "",
      row.isNoTower
        ? "无塔模板：已跳过，不命名、不生成文件，原模板保持不变"
        : row.moveCount ? `${movement.hasCountEvidence ? movement.summary : `搬移：${row.moveType} · ${row.moveCount} 张`} · 新增 ${row.addedLayers} 层 · ${row.strategy || "无需补牌"}${row.strategy === "seeded_random" ? ` / 种子 ${row.seed}` : ""}` : "",
      movement.countSummary,
      row.operation !== "ordinary_add" && row.plan.original_z_offset ? `下方新增 ${row.plan.original_z_offset} 层，原非塔层号统一上移，原塔不动` : "",
      row.operation === "ordinary_add" ? `非塔新增 ${row.addedLayers} 层 · ${row.plan.original_z_offset ? `原非塔层序上移 ${row.plan.original_z_offset} 层，原塔不动` : "原层序不变"}` : row.increaseSummary ? INCREASE.describeSummary(row.increaseSummary) : "",
      row.operation === "ordinary_add" && Number.isInteger(row.plan.top_added_tiles) ? `上方新增 ${row.plan.top_added_tiles} 张 / ${row.plan.top_added_layers} 层；下方新增 ${row.plan.bottom_added_tiles} 张 / ${row.plan.bottom_added_layers} 层` : "",
      coordinatePreview ? `补牌坐标：${coordinatePreview}${row.destinations.length > 6 ? ` 等 ${row.destinations.length} 个` : ""}` : "",
      verificationFacts ? `验收事实：${verificationFacts}` : "",
      issues.length ? `验收：${issues.join("；")}` : "",
      row.sourceSha256 ? `源 SHA：${row.sourceSha256}` : "",
      row.outputSha256 ? `产物 SHA：${row.outputSha256}` : "",
      row.attemptId ? `当前尝试：第 ${row.attemptId} 次` : "",
      attemptTrail ? `尝试记录：${attemptTrail}` : "",
    ].filter(Boolean);
    const sourceId = row.templateId === "—" ? row.name.replace(/_m\.bytes$/, "") : row.templateId;
    const evidence = detailLines.length ? `<details class="result-evidence"><summary aria-label="查看 ${escapeHTML(sourceId)} 的生成与验收明细">明细</summary><p>${detailLines.map(escapeHTML).join("<br>")}</p></details>` : "";
    const moveFacts = [movement.changeSummary, ["increase", "ordinary_add"].includes(row.operation) ? movement.countSummary : row.addedLayers ? `新增 ${row.addedLayers} 层` : ""].filter(Boolean).join(" · ");
    const preview = row.stage !== "import" && TERMINAL_TEMPLATE_STATES.has(row.status)
      ? `<button class="row-action" type="button" data-preview-template="${escapeHTML(row.key)}">${artifact.id ? "阵型对比" : "查看原模板"}</button>` : "";
    const selection = `<input type="checkbox" data-select-result-template="${escapeHTML(row.key)}" aria-label="选择模板 ${escapeHTML(sourceId)}" ${selectedKeys.includes(row.key) ? "checked" : ""} ${VM.canRerunTemplate(row) ? "" : "disabled"}>`;
    const adviceDisclosure = parameterAdvice ? `<details class="result-advice"><summary>查看参数调整建议</summary>${parameterAdvice}</details>` : "";
    const adjusted=row.adjusted&&artifact.id?'<span class="status-pill adjusted">已微调</span>':'';
    const geometryWarnings=artifact.id?row.outputGeometryWarnings||[]:row.sourceGeometryWarnings||[];
    const geometryNotice=geometryWarnings.length?`<p class="inspection-warning">${artifact.id?'原模板一压一已保留':'原模板存在一压一'}：${geometryWarnings.map(w=>`第${w.lower_z+1}、${w.upper_z+1}层(${w.x}, ${w.y})`).map(escapeHTML).join('；')}。${artifact.id?'可在编辑红框处修正，不影响导出。':'生成时可原样保留，编辑红框可定位。'}</p>`:'';
    const thumbnail=TERMINAL_TEMPLATE_STATES.has(row.status)?`<button type="button" class="result-thumbnails" data-result-thumbnail="${escapeHTML(row.key)}" data-preview-template="${escapeHTML(row.key)}" data-artifact="${escapeHTML(artifact.id||'source')}" aria-label="查看 ${escapeHTML(sourceId)} 原模板和新模板预览"><span><span class="thumbnail-caption">原模板</span><span class="thumbnail-slot">待载入</span></span><span><span class="thumbnail-caption">新模板</span><span class="thumbnail-slot">${artifact.id?'待载入':'未生成'}</span></span></button>`:'<span class="file-meta">生成后显示</span>';
    return `<tr><td class="result-select-cell">${selection}</td><td><strong>${escapeHTML(sourceId)}</strong></td><td>${escapeHTML(row.outputId)}</td><td>${escapeHTML(ratioText)}</td><td><span class="result-movement">${escapeHTML(movement.summary)}</span><span class="file-meta source-facts">${escapeHTML(moveFacts)}</span></td><td><div class="result-status-labels"><span class="status-pill ${statusTone(row.status)}">${icon(statusTone(row.status) === "danger" ? "warning" : "status")}${escapeHTML(statusLabel(row.status))}</span>${adjusted}</div>${row.stage==="placement_expanded"?'<p class="file-meta">正在继续搜索其他合法摆法…</p>':""}${row.error.message ? `<p class="inspection-warning">${escapeHTML(row.error.message)}</p>` : ""}${geometryNotice}${adviceDisclosure}${evidence}</td><td>${thumbnail}</td><td><div class="row-actions">${preview}${action}${recovery}${download}</div></td></tr>`;
  }).join("") : '<tr><td colspan="8" class="table-empty">尚无生成结果。</td></tr>';
  if(typeof observeResultThumbnails==="function")observeResultThumbnails(job);
  byId("download-zip-button").disabled = !job.artifacts.length;
}

function observeResultThumbnails(job) {
  if(typeof window==="undefined" || !window.IntersectionObserver || !window.TowerTemplatePreview?.thumbnailMarkup)return;
  state.resultPreviewObserver?.disconnect();
  const work=state.resultPreviewWork;
  for(const waiting of work.queue)work.inflight.delete(waiting.id);
  work.queue=[];
  const cache=state.resultPreviewCache;
  const paint=(button,data)=>{button.innerHTML=[['原模板',data.source],['新模板',data.output]].map(([label,board])=>`<span><span class="thumbnail-caption">${label}</span><span class="thumbnail-slot">${window.TowerTemplatePreview.thumbnailMarkup(board)}</span></span>`).join('');};
  const pump=()=>{
    while(work.active<2 && work.queue.length){const {button,key,id,jobId}=work.queue.shift();work.active++;
      apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/preview/${encodeURIComponent(key)}`).then(data=>{
        if(data.artifact_id!==null && data.artifact_id!==button.dataset.artifact)return;
        cache.set(id,data);while(cache.size>100)cache.delete(cache.keys().next().value);
        if(state.currentJobId===jobId){const current=all('[data-result-thumbnail]',byId('result-table-body')).find(item=>item.dataset.resultThumbnail===key&&item.dataset.artifact===button.dataset.artifact);if(current)paint(current,data);}
      }).catch(()=>{if(button.isConnected)button.querySelectorAll('.thumbnail-slot').forEach(slot=>slot.textContent='点击查看');})
        .finally(()=>{work.active--;work.inflight.delete(id);pump();});
    }
  };
  state.resultPreviewObserver=new window.IntersectionObserver(entries=>{
    for(const {target:button,isIntersecting} of entries){if(!isIntersecting)continue;
      state.resultPreviewObserver.unobserve(button);
      const key=button.dataset.resultThumbnail,id=JSON.stringify([job.id,key,button.dataset.artifact]);
      if(cache.has(id))paint(button,cache.get(id));
      else if(!work.inflight.has(id)){work.inflight.add(id);work.queue.push({button,key,id,jobId:job.id});}
    }pump();
  },{rootMargin:'120px'});
  for(const button of all('[data-result-thumbnail]',byId('result-table-body')))state.resultPreviewObserver.observe(button);
}

function renderTemplatePreview() {
  const data = state.templatePreview;
  if (!data) return;
  updatePreviewTranslationControls();
  const sourceOnly = state.previewSourceOnly === true;
  const renderer = window.TowerTemplatePreview;
  updatePreviewLayerControls();
  const layer=Number(byId("preview-layer").value);
  const options = {mode: layer===0?"all":byId("preview-single-layer").checked?"single":"through", layer, markChanges: byId("preview-mark-changes").checked, editLayer: Boolean(state.manualDraft)};
  byId("preview-source-id").textContent = data.source.template_id;
  byId("preview-output-id").textContent = data.output?.template_id || "未生成";
  for (const [side, board] of [["source", data.source], ["output", state.manualDraft?.board || data.output]]) {
    const issues=side==="output"&&state.manualDraft?(window.TowerManualEdit?.issues?.(board,state.manualDraft.translationOrigin,state.manualDraft.translation)||[]).filter(issue=>state.manualDraft.validationRequested||issue.blocking===false):[];
    byId(`preview-${side}-board`).innerHTML = renderer.boardMarkup(board, {...options,issues});
    const view = board ? renderer.boardView(board, options) : null;
    byId(`preview-${side}-summary`).textContent = view
      ? `显示 ${view.tiles.length} 张 · 完全无遮盖 ${view.highlighted} 张${sourceOnly ? "" : ` · 新增 ${view.added} 张 · 挪动 ${view.changed - view.added} 张`}`
      : sourceOnly ? "" : "当前没有可用的新模板。";
  }
  const addedCount = data.output?.tiles.filter(tile => tile.added).length || 0;
  byId("preview-feedback").textContent = sourceOnly ? "原模板只读，可按层查看阵型。"
    : data.manual_recovery ? `已删除全部塔及塔内牌 ${data.deleted_count || 0} 张，尚未保存。点“去编辑”可补牌，也可直接保存清塔结果；最终仍需独立验收。`
    : data.output ? `新增 ${addedCount} 张，删除 ${data.deleted_count || 0} 张，挪动 ${Math.max(0, data.changed_count - addedCount)} 张 Tile${data.layer_offset ? `；原非塔层序上移 ${data.layer_offset} 层，原塔不动` : ""}。` : "仅显示原模板；当前没有可用的新模板。";
  const originals = new Map(data.source.tiles.map(t => [t.index, t]));
  const originalOnly=sourceOnly||!data.output;
  const inheritedWarnings=originalOnly?data.source_geometry_warnings||data.source.geometry_warnings||[]:data.output_geometry_warnings||data.output?.geometry_warnings||[];
  if(inheritedWarnings.length&&!state.manualDraft){
    byId("preview-feedback").textContent=`${originalOnly?'原模板存在一压一':'原模板一压一已保留'}：${inheritedWarnings.map(w=>`第${w.lower_z+1}、${w.upper_z+1}层(${w.x}, ${w.y})`).join('；')}。${originalOnly?'生成时可原样保留，红框可定位。':'红框标出，可编辑修正；不影响保存导出。'}`;
  }
  byId("preview-feedback").classList?.toggle("is-error",Boolean(inheritedWarnings.length&&!state.manualDraft));
  const moves = (data.output?.tiles || []).filter(t => t.changed);
  byId("preview-moves-body").innerHTML = moves.length ? moves.map(t => {
    const from = originals.get(t.source_index === undefined ? t.index : t.source_index);
    const kind = t.tile_id === -10 ? "A" : t.tile_id === -11 ? "B" : t.tile_id;
    const fromKind = !from ? null : from.tile_id === -10 ? "A" : from.tile_id === -11 ? "B" : from.tile_id;
    const typeChange = !from ? `新增 ${kind}` : fromKind === kind ? kind : `${fromKind} → ${kind}`;
    return `<tr><td>${t.index}</td><td>${escapeHTML(typeChange)}</td><td>${from ? `${from.x}, ${from.y}, ${from.z}` : "—"}</td><td>${t.x}, ${t.y}, ${t.z}</td></tr>`;
  }).join("") : '<tr><td colspan="4">没有挪动 Tile。</td></tr>';
  if (window.TowerManualEdit) {renderManualControls();updateManualCursor();}
}

function translationPending() {
  return Boolean(state.translationBusy && state.translationBusy.jobId === state.currentJobId);
}

function renderManualControls() {
  const panel=byId("preview-manual");
  if(!panel)return;
  const draft=state.manualDraft,busy=translationPending();
  panel.hidden=!draft;
  byId("manual-feedback").hidden=!draft;
  byId("manual-tools").hidden=!draft;
  byId("preview-all-layers").textContent=draft?(draft.history.length||draft.recovery?"保存编辑":"退出编辑"):"去编辑";
  byId("preview-all-layers").classList.toggle("is-save",Boolean(draft?.history.length||draft?.recovery));
  byId("preview-output-board").classList.toggle("is-editing",Boolean(draft));
  const undo=byId("manual-undo");
  if(undo){undo.disabled=busy||!(draft?draft.history.length:state.templatePreview?.can_undo);undo.title=draft?"撤回上一步编辑":"撤回上一次已保存修改";undo.setAttribute("aria-label",undo.title);}
  if(!draft)return;
  const c=window.TowerManualEdit.counts(draft);
  for(const [kind,type] of [["a",-10],["b",-11]]){
    const total=c[kind.toUpperCase()],missing=(3-total%3)%3;
    const button=byId(`manual-brush-${kind}`),count=byId(`manual-brush-${kind}-count`);
    count.textContent=`${total} (${missing})`;
    count.classList.toggle("is-incomplete",missing!==0);
    button.setAttribute("aria-label",`${kind.toUpperCase()} Tile 笔刷，共 ${total} 张，距离3的倍数还差 ${missing} 张`);
    button.setAttribute("aria-pressed",String(!byId("manual-erase").checked&&Number(byId("manual-tile-type").value)===type));
  }
  const problem=draft.validationRequested?window.TowerManualEdit.saveProblem(draft):"";
  const layer=Number(byId("preview-layer").value),layerHint=layer?`第 ${layer} 层增删` : "无限制 · 自动落层";
  byId("manual-counts").textContent=`原模板 / 新模板 Tile 总数：${state.templatePreview?.source?.tiles.length??"—"} / ${c.total} · ${layerHint}${draft.history.length||draft.recovery?" · 未保存":""}`;
  const issues=(window.TowerManualEdit.issues?.(draft.board,draft.translationOrigin,draft.translation)||[]).filter(issue=>draft.validationRequested||issue.blocking===false);
  const inherited=issues.filter(issue=>issue.blocking===false);
  const inheritedMessage=[...new Set(inherited.map(issue=>issue.message))].join('；');
  const hint=layer?`增删仅在第 ${layer} 层进行；左键新增，右键删除。`:"显示全部层；左键在落点最高牌上方新增，右键删除点中的牌。";
  const fullMessage=[state.manualMessage,problem&&!state.manualMessage?.includes(problem)?problem:"",inheritedMessage].filter(Boolean).join(" ") || hint;
  const feedback=byId("manual-status");
  feedback.textContent=problem?.startsWith("保存时总牌量")?"数量未成组，请增删或撤销":problem||state.manualError&&state.manualMessage||inheritedMessage||state.manualMessage||hint;
  feedback.setAttribute("title",fullMessage);
  feedback.setAttribute("aria-label",fullMessage);
  byId("manual-status-detail").textContent=fullMessage;
  byId("manual-status").classList.toggle("is-error",Boolean(problem)||Boolean(state.manualError)||Boolean(inherited.length));
  byId("manual-status").setAttribute("role",problem||state.manualError?"alert":"status");
  const locate=byId("manual-locate-issue");
  if(locate){locate.hidden=!issues.length;locate.disabled=busy;locate.textContent=`定位问题 Tile（${new Set(issues.map(item=>item.index)).size}）`;}
  for(const control of all("button,input,select",byId("manual-tools")))control.disabled=busy;
  byId("manual-undo").disabled=busy||!draft.history.length;
}

function selectManualBrush(type) {
  if(!state.manualDraft||translationPending()||![-10,-11].includes(type))return;
  byId("manual-tile-type").value=String(type);
  byId("manual-erase").checked=false;
  renderManualControls();
  updateManualCursor();
}

function manualAction(action,x,y,tileIndex=null) {
  if(!state.manualDraft||translationPending())return;
  state.manualDraft.validationRequested=false;
  const layer=Number(byId("preview-layer").value);
  const engine=window.TowerManualEdit;
  const target=engine.target(state.manualDraft.board,action,x,y,layer,tileIndex);
  const result=action==="delete"&&!target?{error:layer?`点击处第 ${layer} 层没有牌，未删除其他层。`:"点击处没有牌。"}:engine.apply(state.manualDraft,action==="delete"?{action,index:target.index}:{action,...target,tile_id:Number(byId("manual-tile-type").value)});
  state.manualMessage=result.error||"草稿已更新，尚未保存。";
  state.manualError=Boolean(result.error);
  if(result.draft)state.manualDraft=result.draft;
  renderTemplatePreview();
}

function updateManualCursor(event=null) {
  if(event)state.manualPointer={x:event.clientX,y:event.clientY,right:Boolean(event.buttons&2)};
  const host=byId("preview-output-board"),pointer=state.manualPointer;
  const old=host?.querySelector?.(".manual-tile-cursor");
  if(!state.manualDraft||translationPending()||!pointer){old?.remove();return;}
  const under=document.elementFromPoint(pointer.x,pointer.y);
  const surface=under?.closest(".editor-board");
  if(!surface||!host.contains(surface)){old?.remove();return;}
  const engine=window.TowerManualEdit,board=state.manualDraft.board;
  const point=engine.point(pointer.x,pointer.y,surface.getBoundingClientRect());
  const tile=under.closest("[data-tile-index]"),deleting=pointer.right||byId("manual-erase").checked;
  const target=engine.target(board,deleting?"delete":"add",point.x,point.y,Number(byId("preview-layer").value),tile?Number(tile.dataset.tileIndex):null);
  if(!target){old?.remove();return;}
  const tower=board.towers.find(s=>s.x===target.x&&s.y===target.y);
  const box=window.TowerTemplatePreview.footprint(board,{...target,tower_index:target.tower_index??tower?.index});
  const cursor=old||document.createElement("span");
  cursor.className=`manual-tile-cursor${deleting?" is-delete":""}${box.left>80?" is-right-edge":""}`;
  cursor.setAttribute("aria-hidden","true");
  for(const key of ["left","top","width","height"])cursor.style[key]=`${box[key]}%`;
  for(const key of ["x","y","z"])cursor.dataset[key]=String(target[key]);
  cursor.dataset.label=`${deleting?"删除":Number(byId("manual-tile-type").value)===-11?"B":"A"} · 第 ${target.z+1} 层`;
  cursor.textContent="";
  if(!old)surface.append(cursor);
}

async function saveManualDraft() {
  if(state.batchCenter)return false;
  if(!state.manualDraft||translationPending())return false;
  state.manualDraft.validationRequested=true;
  const problem=window.TowerManualEdit.saveProblem(state.manualDraft);
  if(problem){const hasIssues=window.TowerManualEdit.issues(state.manualDraft.board,state.manualDraft.translationOrigin,state.manualDraft.translation).some(issue=>issue.blocking!==false);state.manualMessage=`未保存：${problem}${hasIssues?" 请修正红框牌后重试；":" "}草稿已保留。`;state.manualError=true;locateManualIssue();renderTemplatePreview();return false;}
  const jobId=state.currentJobId,key=state.previewTemplateKey,request=state.previewRequest;
  const draft=state.manualDraft,operation={jobId,key};
  const current=()=>state.currentJobId===jobId&&state.previewTemplateKey===key&&state.previewRequest===request;
  const recovery=state.templatePreview.manual_recovery===true;
  const payload={...(recovery?{recovery_token:state.templatePreview.recovery_token}:{artifact_id:state.templatePreview.artifact_id}),...window.TowerManualEdit.payload(draft)};
  state.translationBusy=operation;state.manualError=false;state.manualMessage="正在独立验收并保存…";
  invalidatePlans("微调正在保存");renderTemplatePreview();
  try{
    const result=await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/${recovery?"manual-recovery":"edit"}/${encodeURIComponent(key)}`,{method:"POST",json:payload});
    if(state.currentJobId===jobId){invalidatePlans("微调已保存，请重新预览导出或同步");await loadJob(jobId,false,true);}
    if(!current())return false;
    const row=state.currentJob?.templates.find(item=>item.key===key);
    if(!row?.artifacts.some(item=>item.id===result.artifact_id)){
      state.manualMessage="产物已被其他窗口更新。草稿仍保留，请放弃后重新打开，核对新版本再编辑。";
      return false;
    }
    state.templatePreview=result;state.manualDraft=null;state.manualMessage="";
    state.translationFeedback="微调已验收并保存";
    return true;
  }catch(error){
    if(!current())return;
    state.manualMessage=`未保存：${error.message}。草稿和原验收结果均保留。`;
    state.manualError=true;
    return false;
  }finally{
    if(state.translationBusy===operation)state.translationBusy=null;
    if(current()){
      renderTemplatePreview();
      if(!state.manualDraft && byId("template-preview-dialog").open && !byId("manual-close-dialog").open)byId("preview-all-layers").focus();
    }
    updateNamingRegenerationControl();
  }
}

function exitPreviewEditing() {
  if(translationPending())return;
  if(state.manualDraft?.history.length||state.manualDraft?.recovery){closeDialog(byId("template-preview-dialog"));return;}
  state.manualDraft=null;state.manualMessage="";renderTemplatePreview();byId("preview-all-layers").focus();
}

function requestLayerDeletion(mode) {
  if(!previewTranslationAvailable()||translationPending()||!['layer','above'].includes(mode))return;
  const layer=Number(byId("preview-layer").value);
  if(layer<1)return;
  const board=state.manualDraft?.board||state.templatePreview.output;
  const count=board.tiles.filter(t=>mode==='layer'?t.z===layer-1:t.z>layer-1).length;
  if(!count)return;
  state.layerDeletion={request:state.previewRequest,key:state.previewTemplateKey,layer,mode,count};
  byId("manual-layer-delete-title").textContent=mode==='layer'?`删除第 ${layer} 层？`:`删除第 ${layer} 层以上的牌？`;
  byId("manual-layer-delete-description").textContent=`将从新模板草稿中删除 ${count} 张 Tile${mode==='above'?"（不含所选层，只删更高层，含当前未显示的牌）":""}。原模板不变，可撤回；点击“保存编辑”并验收后才生效。`;
  byId("manual-layer-delete-confirm").textContent=`删除 ${count} 张 Tile`;
  openDialog(byId("manual-layer-delete-dialog"));
}

function confirmLayerDeletion() {
  const pending=state.layerDeletion;
  if(!pending||pending.request!==state.previewRequest||pending.key!==state.previewTemplateKey||translationPending()||!previewTranslationAvailable())return;
  if(!state.manualDraft){state.manualDraft=window.TowerManualEdit.create(state.templatePreview.output);state.manualDraft.recovery=state.templatePreview.manual_recovery===true;}
  const result=window.TowerManualEdit.clearLayer(state.manualDraft,pending.layer,pending.mode);
  if(result.draft)state.manualDraft=result.draft;
  state.manualMessage=result.error||`已删除 ${result.deletedCount} 张 Tile，可撤销；尚未保存。`;
  state.manualError=Boolean(result.error);state.layerDeletion=null;
  closeDialog(byId("manual-layer-delete-dialog"),true);renderTemplatePreview();
  byId("preview-all-layers").focus({preventScroll:true});
}

function locateManualIssue() {
  if(!state.manualDraft)return;
  const issues=window.TowerManualEdit.issues(state.manualDraft.board,state.manualDraft.translationOrigin,state.manualDraft.translation).filter(issue=>state.manualDraft.validationRequested||issue.blocking===false);
  const issue=issues.find(item=>item.blocking!==false)||issues[0];
  if(issue)byId("preview-layer").value=String(issue.z+1);
}

function bindManualEditor() {
  const toolbar=byId("preview-translation");
  byId("preview-comparison").before(toolbar);
  toolbar.insertAdjacentHTML("afterbegin",'<div class="preview-translation-header"></div>');
  toolbar.querySelector(".preview-translation-header").append(toolbar.querySelector(".preview-translation-actions"),byId("preview-translation-status"),byId("preview-all-layers"));
  toolbar.querySelector(".preview-translation-actions > span").textContent="平移";
  toolbar.querySelector(".preview-translation-actions").insertAdjacentHTML("afterend",'<div class="preview-rotation-actions" role="group" aria-label="整体旋转新模板"><span>旋转</span><button class="icon-button" type="button" data-rotate-preview="ccw" aria-label="新模板整体逆时针旋转90度" title="逆时针90° · 绕画布中心" disabled><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 10a8 8 0 1 1 1 8M4 4v6h6"/></svg></button><button class="icon-button" type="button" data-rotate-preview="cw" aria-label="新模板整体顺时针旋转90度" title="顺时针90° · 绕画布中心" disabled><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M20 10a8 8 0 1 0-1 8M20 4v6h-6"/></svg></button></div>');
  byId("preview-mark-changes").parentElement.lastChild.textContent="标记挪动";
  byId("manual-layer-delete-confirm").addEventListener("click",confirmLayerDeletion);
  byId("preview-delete-layer").addEventListener("click",()=>requestLayerDeletion("layer"));
  byId("preview-delete-above").addEventListener("click",()=>requestLayerDeletion("above"));
  for(const button of all("[data-rotate-preview]",toolbar))button.addEventListener("click",()=>rotateTemplatePreview(button.dataset.rotatePreview));
  toolbar.insertAdjacentHTML("beforeend",`<div id="preview-manual" class="preview-manual" hidden><div id="manual-tools" hidden><div class="manual-actions"><input id="manual-tile-type" type="hidden" value="-10"><div class="manual-brushes" role="group" aria-label="新增 Tile 笔刷，括号内为补到3的倍数还差的张数"><button type="button" class="manual-brush-choice" id="manual-brush-a" data-brush-type="-10" aria-pressed="true"><span class="manual-brush-count" id="manual-brush-a-count">0 (0)</span><span class="manual-brush-face" aria-hidden="true">A</span></button><button type="button" class="manual-brush-choice" id="manual-brush-b" data-brush-type="-11" aria-pressed="false"><span class="manual-brush-count" id="manual-brush-b-count">0 (0)</span><span class="manual-brush-face" aria-hidden="true">B</span></button></div><label class="check-label"><input id="manual-erase" type="checkbox">删除</label><button type="button" class="icon-button" id="manual-undo" aria-label="撤销上一步编辑" title="撤销上一步编辑"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M9 5 4 10l5 5M4 10h10a6 6 0 0 1 0 12"/></svg></button></div><details class="manual-coordinate"><summary aria-label="按坐标增删" title="按坐标增删"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 5h14v14H5zM9 2v6m6-6v6M2 9h6m-6 6h6m8-6h6m-6 6h6"/></svg></summary><div class="manual-actions"><label for="manual-x">X</label><input id="manual-x" type="number" step="1" value="8"><label for="manual-y">Y</label><input id="manual-y" type="number" step="1" value="9"><button type="button" class="secondary-button" id="manual-add-coordinate">增加 Tile</button><button type="button" class="secondary-button" id="manual-delete-coordinate">删除 Tile</button></div></details></div></div><div id="manual-feedback" class="manual-feedback" hidden><p id="manual-counts"></p><p id="manual-status" role="status" aria-live="polite"></p></div>`);
  toolbar.querySelector(".preview-translation-header").insertBefore(byId("preview-manual"),byId("preview-all-layers"));
  toolbar.querySelector(".preview-translation-header").insertBefore(byId("manual-undo"),byId("preview-all-layers"));
  for(const kind of ["a","b"])byId(`manual-brush-${kind}`).addEventListener("click",event=>selectManualBrush(Number(event.currentTarget.dataset.brushType)));
  byId("manual-feedback").insertAdjacentHTML("beforeend",'<button type="button" class="row-action" id="manual-locate-issue" hidden>定位问题 Tile</button>');
  byId("manual-status").outerHTML='<details class="manual-status-details"><summary aria-label="查看完整编辑提示"><span id="manual-status" role="status" aria-live="polite"></span></summary><p id="manual-status-detail"></p></details>';
  byId("manual-locate-issue").addEventListener("click",()=>{locateManualIssue();renderTemplatePreview();byId("preview-output-board").querySelector(".is-invalid")?.scrollIntoView({block:"center",behavior:"auto"});});
  byId("manual-erase").addEventListener("change",()=>{renderManualControls();updateManualCursor();});
  byId("preview-all-layers").addEventListener("click",()=>{
    if(!previewTranslationAvailable()||translationPending())return;
    if(state.manualDraft){if(state.manualDraft.history.length||state.manualDraft.recovery)saveManualDraft();else exitPreviewEditing();return;}
    state.manualDraft=window.TowerManualEdit.create(state.templatePreview.output);state.manualDraft.validationRequested=false;state.manualMessage="";state.manualError=false;
    state.manualDraft.recovery=state.templatePreview.manual_recovery===true;
    renderTemplatePreview();byId(Number(byId("manual-tile-type").value)===-11?"manual-brush-b":"manual-brush-a").focus({preventScroll:true});
  });
  byId("manual-close-discard").addEventListener("click",()=>{
    state.manualDraft=null;state.manualMessage="";
    closeDialog(byId("manual-close-dialog"),true);closeDialog(byId("template-preview-dialog"),true);
  });
  byId("manual-close-save").addEventListener("click",async()=>{
    const dialog=byId("manual-close-dialog");setDialogPending(dialog,true);
    try{
      if(await saveManualDraft()){closeDialog(dialog,true);closeDialog(byId("template-preview-dialog"),true);}
      else byId("manual-close-feedback").textContent=state.manualMessage||"未能保存，草稿仍保留。可返回继续编辑。";
    }finally{setDialogPending(dialog,false);}
  });
  byId("manual-undo").addEventListener("click",undoTemplatePreview);
  for(const action of ["add","delete"])byId(`manual-${action}-coordinate`).addEventListener("click",()=>manualAction(action,Number(byId("manual-x").value),Number(byId("manual-y").value)));
  const board=byId("preview-output-board");
  board.addEventListener("pointermove",updateManualCursor);
  board.addEventListener("pointerup",updateManualCursor);
  board.addEventListener("pointerleave",()=>{state.manualPointer=null;updateManualCursor();});
  const hideCursor=()=>{state.manualPointer=null;updateManualCursor();};
  byId("template-preview-dialog").addEventListener("scroll",hideCursor,true);
  window.addEventListener("resize",hideCursor);
  board.addEventListener("contextmenu",event=>{if(state.manualDraft)event.preventDefault();});
  board.addEventListener("pointerdown",event=>{
    if(!state.manualDraft||translationPending()||![0,2].includes(event.button))return;
    const surface=event.target.closest(".editor-board");if(!surface)return;
    event.preventDefault();
    state.manualPointer={x:event.clientX,y:event.clientY,right:event.button===2};
    const point=window.TowerManualEdit.point(event.clientX,event.clientY,surface.getBoundingClientRect());
    const deleting=event.button===2||byId("manual-erase").checked;
    const tile=event.target.closest("[data-tile-index]");
    manualAction(deleting?"delete":"add",point.x,point.y,tile?Number(tile.dataset.tileIndex):null);
  });
  window.addEventListener("beforeunload",event=>{if(state.manualDraft?.history.length||state.manualDraft?.recovery){event.preventDefault();event.returnValue="";}});
}

function previewTranslationAvailable() {
  if(state.batchCenter)return false;
  const row = state.currentJob?.templates.find(item => item.key === state.previewTemplateKey);
  return Boolean(state.previewJobId && state.previewJobId === state.currentJobId
    && !state.previewSourceOnly && state.templatePreview?.output
    && row
    && TERMINAL_JOB_STATES.has(state.currentJob.status)
    && (state.templatePreview.manual_recovery===true && Boolean(state.templatePreview.recovery_token) && ["failed","interrupted"].includes(row.status) && !row.artifacts.length
      || ["verified", "succeeded", "exported"].includes(row.status) && state.templatePreview.artifact_id && row.artifacts.some(item => item.id === state.templatePreview.artifact_id)));
}

function updatePreviewTranslationControls() {
  const available = previewTranslationAvailable();
  byId("preview-all-layers").disabled = !available || translationPending();
  const toolbar = byId("preview-translation");
  toolbar.hidden = !available;
  if (!available) return;
  const draft=state.manualDraft;
  const offset = draft ? {...(draft.translation||{dx:0,dy:0})} : state.templatePreview.translation || {dx: 0, dy: 0};
  const busy = translationPending();
  toolbar.setAttribute("aria-busy", String(busy));
  for(const button of all("[data-rotate-preview]",toolbar))button.disabled=busy||state.templatePreview.manual_recovery===true&&!draft;
  const directions = {up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0], reset: [-offset.dx, -offset.dy]};
  for (const button of all("[data-translate-preview]", toolbar)) {
    const [x, y] = directions[button.dataset.translatePreview];
    const dx = offset.dx + x, dy = offset.dy + y;
    const outside = !draft&&(dx < offset.min_dx || dx > offset.max_dx || dy < offset.min_dy || dy > offset.max_dy);
    button.disabled = busy || outside || state.templatePreview.manual_recovery===true&&!draft || button.dataset.translatePreview === "reset" && !offset.dx && !offset.dy;
    button.title = outside ? "已到安全边界，不能继续向该方向移动"
      : draft ? "移动整个草稿，点击保存编辑后生效" : button.dataset.translatePreview === "reset" ? "复位到本次生成位置，验收通过后保存" : "每次移动整个新模板 1 格，验收通过后保存";
  }
  const status = byId("preview-translation-status");
  status.className = state.translationError ? "is-error" : "";
  const detail=`偏移 X ${offset.dx} / Y ${offset.dy} 格${busy?" · 正在验收保存…":draft?"":` · ${state.translationFeedback||"平移或旋转自动验收并保存"}`}`;
  status.textContent=`X ${offset.dx} / Y ${offset.dy}${busy?" · 保存中…":draft?draft.quarterTurns?` · ${draft.quarterTurns*90}°`:"":` · ${state.translationFeedback||"自动保存"}`}`;
  status.title=detail;status.setAttribute("aria-label",detail);
}

async function rotateTemplatePreview(direction) {
  if(!['cw','ccw'].includes(direction)||!previewTranslationAvailable()||translationPending())return;
  if(state.manualDraft){
    const result=window.TowerManualEdit.rotate(state.manualDraft,direction);
    if(result.draft)state.manualDraft=result.draft;
    state.manualMessage=result.error||`已${direction==='cw'?'顺':'逆'}时针旋转90°，尚未保存。`;state.manualError=Boolean(result.error);
    renderTemplatePreview();return;
  }
  if(state.templatePreview.manual_recovery)return;
  const jobId=state.currentJobId,key=state.previewTemplateKey,request=state.previewRequest,generation=state.requestGeneration;
  const operation={jobId,key},artifactId=state.templatePreview.artifact_id;
  const currentJob=()=>state.currentJobId===jobId&&state.requestGeneration===generation;
  const current=()=>currentJob()&&state.previewTemplateKey===key&&state.previewRequest===request;
  state.translationBusy=operation;state.translationError=false;state.translationFeedback="";
  invalidatePlans("正在旋转阵型");renderTemplatePreview();updateNamingRegenerationControl();
  try{
    const result=await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/rotate/${encodeURIComponent(key)}`,{method:"POST",json:{artifact_id:artifactId,direction}});
    if(!currentJob())return;
    invalidatePlans("旋转已保存，请重新预览导出或同步");await loadJob(jobId,false,true);
    if(!current())return;
    const row=state.currentJob?.templates.find(item=>item.key===key);
    if(!row?.artifacts.some(item=>item.id===result.artifact_id))throw new Error("产物已变化，请关闭后重新打开对比。");
    state.templatePreview=result;state.translationFeedback="旋转已保存";
  }catch(error){
    if(!current())return;
    state.translationError=true;state.translationFeedback=`旋转未保存：${error.message}`;
    if(['stale_artifact','artifact_changed','artifact_not_found','source_changed'].includes(error.code))state.templatePreview.artifact_id="";
  }finally{
    if(state.translationBusy===operation)state.translationBusy=null;
    if(current())renderTemplatePreview();
    if(currentJob())updateNamingRegenerationControl();
  }
}

async function undoTemplatePreview() {
  if(translationPending()||!previewTranslationAvailable())return;
  if(state.manualDraft){
    state.manualDraft=window.TowerManualEdit.undo(state.manualDraft);
    state.manualDraft.validationRequested=false;state.manualError=false;state.manualMessage="已撤回上一步。";
    renderTemplatePreview();return;
  }
  if(!state.templatePreview.can_undo)return;
  const jobId=state.currentJobId,key=state.previewTemplateKey,request=state.previewRequest,generation=state.requestGeneration;
  const currentJob=()=>state.currentJobId===jobId&&state.requestGeneration===generation;
  const current=()=>currentJob()&&state.previewTemplateKey===key&&state.previewRequest===request;
  const operation={jobId,key},artifactId=state.templatePreview.artifact_id;
  state.translationBusy=operation;state.translationError=false;state.translationFeedback="";
  invalidatePlans("正在撤回阵型修改");renderTemplatePreview();updateNamingRegenerationControl();
  try{
    const result=await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/undo/${encodeURIComponent(key)}`,{method:"POST",json:{artifact_id:artifactId}});
    if(!currentJob())return;
    invalidatePlans("已撤回修改，请重新预览导出或同步");await loadJob(jobId,false,true);
    if(!current())return;
    const row=state.currentJob?.templates.find(item=>item.key===key);
    if(!row?.artifacts.some(item=>item.id===result.artifact_id))throw new Error("模板已变化，请重新打开对比。");
    state.templatePreview=result;state.translationFeedback="已撤回并保存";
  }catch(error){
    if(!current())return;
    state.translationError=true;state.translationFeedback=`撤回未保存：${error.message}。请重新打开对比，核对当前结果。`;
  }finally{
    if(state.translationBusy===operation)state.translationBusy=null;
    if(current())renderTemplatePreview();
    if(currentJob())updateNamingRegenerationControl();
  }
}

async function translateTemplatePreview(direction) {
  if(state.batchCenter)return;
  if (translationPending() || !previewTranslationAvailable()) return;
  if(state.templatePreview.manual_recovery&&!state.manualDraft)return;
  const offsets = {up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0], reset: [0, 0]};
  if (!offsets[direction]) return;
  if(state.manualDraft){
    const draftOffset=state.manualDraft.translation||{dx:0,dy:0};
    const [x,y]=direction==="reset"?[-draftOffset.dx,-draftOffset.dy]:offsets[direction];
    const result=window.TowerManualEdit.translate(state.manualDraft,x,y);
    if(result.draft){state.manualDraft=result.draft;state.manualDraft.validationRequested=false;}
    state.manualMessage=result.error||"草稿已平移，尚未保存。";state.manualError=Boolean(result.error);
    renderTemplatePreview();return;
  }
  const offset = state.templatePreview.translation || {dx: 0, dy: 0};
  const [x, y] = offsets[direction];
  const dx = direction === "reset" ? 0 : offset.dx + x;
  const dy = direction === "reset" ? 0 : offset.dy + y;
  if (dx < offset.min_dx || dx > offset.max_dx || dy < offset.min_dy || dy > offset.max_dy) return;
  const jobId = state.currentJobId, key = state.previewTemplateKey, request = state.previewRequest;
  const generation = state.requestGeneration;
  const artifactId = state.templatePreview.artifact_id;
  const operation = {jobId, key};
  const currentJob = () => state.currentJobId === jobId && state.requestGeneration === generation;
  const currentPreview = () => currentJob() && state.previewRequest === request && state.previewTemplateKey === key;
  state.translationBusy = operation;
  state.translationFeedback = "";
  state.translationError = false;
  invalidatePlans("正在调整阵型");
  updatePreviewTranslationControls();
  updateNamingRegenerationControl();
  try {
    const payload = await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/translate/${encodeURIComponent(key)}`, {
      method: "POST", json: {artifact_id: artifactId, dx, dy},
    });
    if (!currentJob()) return;
    invalidatePlans("阵型已保存，请重新预览导出或同步");
    await loadJob(jobId, false, true);
    if (!currentPreview()) return;
    const row = state.currentJob?.templates.find(item => item.key === key);
    if (!row?.artifacts.some(item => item.id === payload.artifact_id)) {
      state.translationFeedback = "产物已变化，请关闭后重新打开阵型对比。";
      state.translationError = true;
      return;
    }
    state.templatePreview = payload;
    if(state.manualDraft)state.manualDraft=window.TowerManualEdit.create(payload.output);
    state.translationFeedback = "已验收并保存";
    renderTemplatePreview();
  } catch (error) {
    if (!currentPreview()) return;
    state.translationError = true;
    state.translationFeedback = error.code === "translation_out_of_bounds"
      ? `移动未保存：${error.message}。可换方向或复位。`
      : `未能确认保存：${error.message}。关闭后重新打开阵型对比，核对当前结果再重试。`;
    if (error.code !== "translation_out_of_bounds") {
      state.templatePreview.artifact_id = "";
      byId("preview-feedback").textContent = state.translationFeedback;
    }
  } finally {
    if (state.translationBusy === operation) state.translationBusy = null;
    if (currentJob()) updateNamingRegenerationControl();
    if (currentJob() && state.templatePreview) {
      updatePreviewTranslationControls();
      if(currentPreview() && state.manualDraft)renderManualControls();
      if (!currentPreview() && state.previewJobId === jobId && state.previewTemplateKey === key
        && !previewTranslationAvailable()) {
        byId("preview-feedback").textContent = "阵型已变化，请关闭后重新打开对比，查看当前保存结果。";
      }
    }
  }
}

function updatePreviewLayerControls() {
  const data = state.templatePreview;
  const levels = [...(data?.source?.tiles || []), ...(state.manualDraft?.board.tiles || data?.output?.tiles || [])].map(tile => tile.z + 1);
  const maximum = Math.min(256, levels.reduce((maximum, value) => Math.max(maximum, value), 1) + (state.manualDraft ? 1 : 0));
  const range = byId("preview-layer");
  range.min = "0";
  range.max = String(maximum);
  range.disabled = !data;
  const value = Math.min(maximum, Math.max(0, Number(range.value) || 0));
  range.value = String(value);
  const single=byId("preview-single-layer");
  single.disabled=!data||value===0;
  if(value===0)single.checked=false;
  byId("preview-layer-unlimited").disabled=!data;
  byId("preview-layer-unlimited").setAttribute("aria-pressed",String(value===0));
  byId("preview-layer-prefix").textContent = "层级";
  byId("preview-layer-value").textContent = value?`${value} 层`:"全部层";
  range.setAttribute("aria-valuetext", value?`第 ${value} 层增删，${single.checked?"仅看当前层":"显示第 1 层至当前层"}`:"无限制，显示全部层，自动落层");
  byId("preview-layer-minus").disabled = !data || value === 0;
  byId("preview-layer-plus").disabled = !data || value >= maximum;
  byId("preview-all-layers").setAttribute("aria-pressed", String(Boolean(state.manualDraft)));
  const canEdit=Boolean(state.manualDraft)||(typeof previewTranslationAvailable==='function'&&previewTranslationAvailable());
  const board=state.manualDraft?.board||data?.output;
  for(const [id,above] of [["preview-delete-layer",false],["preview-delete-above",true]]){
    const count=value>0?(board?.tiles||[]).filter(t=>above?t.z>value-1:t.z===value-1).length:0;
    byId(id).disabled=!canEdit||translationPending()||!count;
    byId(id).title=value===0?"先选择具体层":`${above?'只删除更高层，不含本层':'只删除本层'} · ${count} 张 · 可撤回，保存后生效`;
  }
}

function setPreviewLayer(value) {
  if (!state.templatePreview || !Number.isFinite(Number(value))) return;
  const range = byId("preview-layer");
  updatePreviewLayerControls();
  range.value = String(Math.min(Number(range.max), Math.max(0, Math.round(Number(value)))));
  renderTemplatePreview();
}

function stepPreviewLayer(delta) {
  setPreviewLayer(Number(byId("preview-layer").value) + delta);
}

function showAllPreviewLayers() {
  if (!state.templatePreview) return;
  updatePreviewLayerControls();
  byId("preview-layer").value = "0";
  renderTemplatePreview();
}

async function openTemplatePreview(key, origin = "job", ordinaryOptions = null) {
  if(origin==="job")state.previewReturnPosition={key,jobId:state.currentJobId,thumbnail:Boolean(document.activeElement?.dataset?.resultThumbnail),x:window.scrollX,y:window.scrollY,tableX:byId("result-table-body").closest?.(".table-scroll")?.scrollLeft||0};
  const sourceOnly = origin === "import";
  const ordinary = origin === "ordinary";
  const recovery = origin === "recovery";
  if (ordinary) {
    const invalid = ORDINARY.validate(ordinaryOptions);
    if (invalid) {showToast(invalid.message, "error");return;}
  }
  const imported = sourceOnly || ordinary;
  const identity = imported ? state.importId : state.currentJobId;
  const fingerprint = ordinary ? JSON.stringify(ordinaryOptions) : "";
  const registered = imported ? state.importedFiles.some(row => row.fileId === key)
    : state.currentJob?.templates.some(row => row.key === key);
  if (!identity || !registered) return;
  state.previewController?.abort();
  const controller = new AbortController();
  state.previewController = controller;
  const request = ++state.previewRequest;
  const isCurrent = () => !controller.signal.aborted && request === state.previewRequest
    && identity === (imported ? state.importId : state.currentJobId)
    && (!ordinary || state.mode === "ordinary_add" && fingerprint === JSON.stringify(state.ordinaryOverrides[key] || state.ordinaryDefaults));
  state.templatePreview = null;
  state.manualDraft = null;
  state.manualPointer = null;
  state.manualMessage = "";
  state.previewJobId = imported ? "" : identity;
  state.previewTemplateKey = key;
  state.translationFeedback = "";
  state.translationError = false;
  byId("preview-translation").hidden = true;
  state.previewSourceOnly = sourceOnly;
  updatePreviewLayerControls();
  byId("preview-mark-changes").checked = true;
  byId("preview-layer").disabled = true;
  byId("preview-mark-changes").disabled = true;
  byId("template-preview-title").textContent = sourceOnly ? "原模板预览" : "原模板与新模板对比";
  byId("template-preview-description").textContent = sourceOnly ? "按编辑器坐标查看原阵型，可切换显示层。" : recovery ? "原模板只读；右侧为清塔草稿，可手动补牌，也可仅清塔后保存验收。" : "相同编辑器坐标和比例，左右同步切换显示层。";
  byId("preview-output-side").hidden = sourceOnly;
  byId("preview-change-control").hidden = sourceOnly;
  byId("preview-moves").hidden = sourceOnly;
  byId("preview-move-legend").hidden = sourceOnly;
  byId("preview-comparison").setAttribute("data-source-only", String(sourceOnly));
  for (const side of ["source", "output"]) {
    byId(`preview-${side}-id`).textContent = "";
    byId(`preview-${side}-board`).innerHTML = '<div class="preview-empty">正在读取模板…</div>';
    byId(`preview-${side}-summary`).textContent = "";
  }
  byId("preview-feedback").className = "preview-feedback";
  byId("preview-feedback").textContent = sourceOnly ? "正在读取原模板。" : "正在读取原模板和已验收的新模板。";
  byId("preview-comparison").setAttribute("aria-busy", "true");
  openDialog(byId("template-preview-dialog"));
  try {
    const path = recovery ? `/api/jobs/${encodeURIComponent(identity)}/manual-recovery/${encodeURIComponent(key)}` : ordinary ? "/api/ordinary-add/preview" : sourceOnly ? `/api/imports/${encodeURIComponent(identity)}/preview/${encodeURIComponent(key)}`
      : `/api/jobs/${encodeURIComponent(identity)}/preview/${encodeURIComponent(key)}`;
    const payload = asObject(await apiRequest(path, {signal: controller.signal,
      ...(recovery ? {method:"POST",json:{}} : ordinary ? {method:"POST", json:{import_id:identity,file_id:key,ordinary_add:ordinaryOptions}} : {})}));
    if (!isCurrent()) return;
    state.templatePreview = payload;
    const layers = [...new Set([...(payload.source?.tiles || []), ...(payload.output?.tiles || [])].map(t => t.z + 1))].sort((a,b) => a-b);
    byId("preview-layer").max = String(layers[layers.length - 1] || 1);
    byId("preview-layer").value = "0";
    renderTemplatePreview();
    if (ordinary) {
      byId("template-preview-description").textContent = "当前参数的只读试算，尚未生成可导出的文件。";
      if (payload.error) {
        byId("preview-feedback").className = "preview-feedback is-error";
        byId("preview-feedback").textContent = payload.error.message;
      }
    }
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) return;
    byId("preview-feedback").className = "preview-feedback is-error";
    byId("preview-feedback").textContent = `预览失败：${error.message}。关闭后点击预览入口重试。`;
    for (const side of ["source", "output"]) byId(`preview-${side}-board`).innerHTML = '<div class="preview-empty">未能读取预览。</div>';
  } finally {
    if (isCurrent()) {
      byId("preview-comparison").setAttribute("aria-busy", "false");
      byId("preview-mark-changes").disabled = !state.templatePreview;
    }
  }
}

function cancelTemplatePreview() {
  state.previewController?.abort();
  state.previewController = null;
  state.previewRequest += 1;
  state.templatePreview = null;
  state.previewSourceOnly = false;
  state.previewJobId = "";
  state.previewTemplateKey = "";
}

async function loadJob(jobId, navigate = true, silent = false) {
  if (!jobId) return;
  const switching = state.currentJobId && state.currentJobId !== jobId;
  if (switching) {
    cancelBatchRegeneration();
    cancelPolling();
    invalidatePlans("已切换任务");
    state.pendingJobId = jobId;
  }
  const generation = state.requestGeneration;
  try {
    const payload = await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}`, {signal: state.pollController?.signal});
    const previous = state.currentJob;
    const next = normalizeJob(payload);
    if (!VM.shouldAcceptJobResponse({
      requestJobId: jobId,
      currentJobId: state.pendingJobId || state.currentJobId || jobId,
      requestGeneration: generation,
      activeGeneration: state.requestGeneration,
      currentJobIdForRevision: previous?.id || "",
      currentRevision: previous?.revision,
      responseRevision: next.revision,
    })) return false;
    if (state.exportPlan || state.syncPlan) {
      const changed = previous && (previous.id !== next.id || previous.revision !== next.revision
        || JSON.stringify(previous.artifacts) !== JSON.stringify(next.artifacts));
      if (changed) invalidatePlans("任务或产物已变化");
    }
    const mode = next.templates.some(row => row.operation === "ordinary_add" || row.parameters.mode === "ordinary_add") ? "ordinary_add"
      : next.templates.some(row => row.operation === "increase" || row.parameters.mode === "increase") ? "increase" : "reduce";
    const group = mode === "ordinary_add" ? "ordinary" : "tower";
    if (navigate && state.workbenchGroup !== group && !switchWorkbench(group)) {state.pendingJobId = "";return false;}
    state.currentJobId = jobId;
    state.currentJob = next;
    state.pendingJobId = "";
    state.workbenchGroup = group;
    if (state.mode !== mode) setWorkspaceMode(mode);
    if (previous?.id !== next.id && !byId("export-naming-rule").dataset.edited) {
      byId("export-naming-rule").value = asText(next.templates[0]?.parameters.naming_rule, currentNamingRule(mode));
    }
    safeStorage.set("tower-reducer-current-job", jobId);
    if (navigate) {
      routeTo(mode === "ordinary_add" ? "ordinary_add" : "workspace");
      showStep(3);
    }
    renderJob(state.currentJob);
    return true;
  } catch (error) {
    if (state.pendingJobId === jobId) state.pendingJobId = "";
    if (error.name === "AbortError") return false;
    if (!silent) showToast(`读取任务失败：${error.message}`, "error");
    throw error;
  }
}

function cancelPolling(incrementGeneration = true) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (state.pollController) state.pollController.abort();
  state.pollController = null;
  if (incrementGeneration) state.requestGeneration += 1;
}

function cancelBatchRegeneration() {
  if (state.batchController) state.batchController.abort();
  state.batchController = null;
  state.namingRegenerationActive = false;
}

function schedulePoll(delay = 900) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  if (!state.currentJobId) return;
  const jobId = state.currentJobId;
  const generation = state.requestGeneration;
  state.pollTimer = window.setTimeout(async () => {
    if (generation !== state.requestGeneration || jobId !== state.currentJobId) return;
    state.pollController = new AbortController();
    try {
      await loadJob(jobId, false, true);
      state.pollFailures = 0;
      if (state.currentJob && !TERMINAL_JOB_STATES.has(state.currentJob.status)) schedulePoll(900);
    } catch (_error) {
      state.pollFailures += 1;
      const nextDelay = VM.nextPollDelay(state.pollFailures);
      if (state.pollFailures >= 6) {
        setServiceStatus("is-offline", "本地服务连接中断");
        showFeedback("export-feedback", "连续重连失败，已停止自动刷新。请确认服务窗口正在运行，再从运行记录打开任务。", "error");
        return;
      }
      schedulePoll(nextDelay);
    }
  }, delay);
}

function rerunIncreasePreviewFile(row) {
  if (!state.importId || !window.towerIncreasePreview) return null;
  return state.importedFiles?.find(item => item.sourceSha256 && item.sourceSha256 === row.sourceSha256) || null;
}

function mountRerunIncreaseEditor(row) {
  const context = state.rerunAdviceContext, session = state.rerunSession;
  const file = rerunIncreasePreviewFile(row);
  const current = () => state.rerunKey === row.key && state.rerunAdviceContext === context && state.rerunSession === session
    && (byId("rerun-dialog").open || state.rerunPreviewActive)
    && increaseAdviceContext(state.currentJob?.templates.find(item => item.key === row.key) || {}) === context;
  INCREASE.mountEditor(byId("rerun-increase-fields"), {prefix: "rerun-increase", options: state.rerunIncrease,
    onChange(next) {state.rerunIncrease = next;},
    onPreview: file ? next => {
      if (!current()) return showFeedback("rerun-feedback", "当前模板或任务已更新，请关闭后重新打开调整窗口。", "warning");
      state.rerunPreviewActive = true;
      closeDialog(byId("rerun-dialog"));
      window.towerIncreasePreview.open({label: row.templateId, import_id: state.importId, file_id: file.fileId, options: next,
        onChange(options) {
          if (!current()) return;
          state.rerunIncrease = structuredClone(options);
          mountRerunIncreaseEditor(row);
        },
        onClose() {
          queueMicrotask(() => {
            if (!current()) return;
            state.rerunPreviewActive = false;
            mountRerunIncreaseEditor(row);
            openDialog(byId("rerun-dialog"));
            byId("rerun-increase-fields").querySelector?.("[data-increase-open]")?.focus();
          });
        },
      });
    } : null,
  });
}

function openRerunEditor(key, keys = [key]) {
  if(state.batchCenter)return showToast("请等待批量居中完成，再修改参数。","warning");
  const row = state.currentJob?.templates.find((item) => item.key === key);
  if (!row) return;
  const options = {...collectDefaults(), ...row.parameters};
  state.rerunKey = key;
  state.rerunKeys = [...keys];
  const bulk = keys.length > 1;
  state.rerunBatch = bulk;
  byId("rerun-dialog-title").textContent = bulk ? `批量调整 ${keys.length} 个模板` : "调整参数并重新验收";
  byId("rerun-dialog-description").textContent = bulk
    ? `以 ${row.templateId} 的参数为起点，统一应用到选中的 ${keys.length} 个模板；保留各自命名与高级逐塔设置。后台排队处理，不影响其他模板。`
    : "只重跑当前模板，其他模板不受影响。其他模板运行时也可提交，后台按队列处理。";
  byId("confirm-rerun-button").textContent = bulk ? `提交 ${keys.length} 个模板重跑` : "重新生成并验收";
  state.rerunSession = (state.rerunSession || 0) + 1;
  state.rerunPreviewActive = false;
  const increase = row.operation === "increase" || row.parameters.mode === "increase";
  const ordinary = row.operation === "ordinary_add" || row.parameters.mode === "ordinary_add";
  state.rerunIncrease = increase ? structuredClone({...INCREASE.defaults(), ...row.parameters.increase}) : null;
  state.rerunOrdinary = ordinary ? structuredClone({...ORDINARY.defaults(),...row.parameters.ordinary_add}) : null;
  state.rerunAdviceContext = increase || ordinary ? increaseAdviceContext(row) : "";
  let increaseFields = byId("rerun-increase-fields");
  if (!increaseFields) {
    increaseFields = document.createElement("div");increaseFields.id = "rerun-increase-fields";
    byId("rerun-form").prepend(increaseFields);
  }
  increaseFields.hidden = !increase;
  let ordinaryFields = byId("rerun-ordinary-fields");
  if (!ordinaryFields) {
    ordinaryFields = document.createElement("div");ordinaryFields.id = "rerun-ordinary-fields";
    byId("rerun-form").prepend(ordinaryFields);
  }
  ordinaryFields.hidden = !ordinary;
  if (ordinary) ORDINARY.mountEditor(ordinaryFields, {prefix:"rerun-ordinary",options:state.rerunOrdinary,
    onChange(next) {state.rerunOrdinary = next;},
  });
  for (const id of ["rerun-min-ratio", "rerun-ratio", "rerun-replenish-type", "rerun-strategy", "rerun-layer-limit", "rerun-max-layers", "rerun-seed", "rerun-retained-counts"]) {
    const field = byId(id).closest?.(".field");if (field) field.hidden = increase || ordinary;
  }
  byId("rerun-recommendations").hidden = bulk || !["failed", "interrupted", "no_op"].includes(row.status);
  if (increase) mountRerunIncreaseEditor(row);
  byId("rerun-template-key").value = key;
  byId("rerun-min-ratio").value = asNumber(row.parameters.min_ratio, 0) * 100;
  byId("rerun-ratio").value = asNumber(options.ratio, 0.25) * 100;
  byId("rerun-replenish-type").value = asText(row.parameters.replenish_type, "auto");
  updateReplenishGuidance("rerun");
  byId("rerun-strategy").value = asText(options.strategy, "top_first");
  byId("rerun-layer-limit").value = asNumber(options.max_tiles_per_layer, 20);
  byId("rerun-max-layers").value = asNumber(options.max_added_layers, 2);
  byId("rerun-seed").value = asNumber(options.seed, 0);
  byId("rerun-new-id").value = row.outputId === "—" ? "" : row.outputId;
  byId("rerun-retained-counts").value = asArray(options.retained_counts).join(",");
  const nameField = byId("rerun-new-id").closest?.(".field");
  const retainedField = byId("rerun-retained-counts").closest?.(".field");
  if (nameField) nameField.hidden = bulk;
  if (retainedField) retainedField.hidden = bulk || increase || ordinary;
  if (bulk) {byId("rerun-new-id").value = "";byId("rerun-retained-counts").value = "";}
  const sourceCounts = asArray(row.sourceTowerCounts);
  byId("rerun-retained-help").textContent = sourceCounts.length
    ? `留空表示自动分配；填写时必须按顺序提供 ${sourceCounts.length} 个整数，每堆上限依次为 ${sourceCounts.join(" / ")}。`
    : "当前模板没有可配置的塔堆，请留空。";
  byId("rerun-recommendations").innerHTML = renderParameterAdvice(row, true);
  clearFeedback("rerun-feedback");
  openDialog(byId("rerun-dialog"));
}

function applyIncreaseAdvice(key, index, context) {
  const row = state.currentJob?.templates.find(item => item.key === key);
  if (!row || increaseAdviceContext(row) !== context) return showToast("当前模板的建议已更新，请查看最新结果后再应用。", "warning");
  if (!VM.parameterAdviceView(row).schemes[index]) return;
  openRerunEditor(key);
  fillAdviceScheme(index);
}

function fillAdviceScheme(index) {
  const row = state.currentJob?.templates.find((item) => item.key === state.rerunKey);
  if (!row) return;
  const increase = row.operation === "increase" || row.parameters.mode === "increase";
  const ordinary = row.operation === "ordinary_add" || row.parameters.mode === "ordinary_add";
  if ((increase || ordinary) && increaseAdviceContext(row) !== state.rerunAdviceContext) return showFeedback("rerun-feedback", "当前模板的建议已更新，请关闭后重新打开调整窗口；已填参数保持不变。", "warning");
  const scheme = VM.parameterAdviceView(row).schemes[index];
  if (!scheme) return;
  if (ordinary) {
    state.rerunOrdinary = structuredClone(scheme.parameters.ordinary_add);
    ORDINARY.mountEditor(byId("rerun-ordinary-fields"), {prefix:"rerun-ordinary",options:state.rerunOrdinary,
      onChange(next) {state.rerunOrdinary=next;}});
    clearInvalid(byId("rerun-form"));
    showFeedback("rerun-feedback", "已应用推荐参数，仅当前模板的调整窗口已更新；请点击“重新生成并验收”。", "success");
    byId("confirm-rerun-button").focus();
    return;
  }
  if (increase) {
    state.rerunIncrease = structuredClone(scheme.parameters.increase);
    mountRerunIncreaseEditor(row);
    clearInvalid(byId("rerun-form"));
    const nextAction = rerunIncreasePreviewFile(row) ? "可先预览，再点击“重新生成并验收”。" : "请点击“重新生成并验收”，完成后查看阵型对比。";
    showFeedback("rerun-feedback", `已应用推荐参数，仅当前模板的调整窗口已更新；${nextAction}`, "success");
    byId("confirm-rerun-button").focus();
    return;
  }
  const p = scheme.parameters;
  if (scheme.changedFields.includes("min_ratio")) byId("rerun-min-ratio").value = Number((p.min_ratio * 100).toFixed(3));
  byId("rerun-ratio").value = Number((p.ratio * 100).toFixed(3));
  if (scheme.changedFields.includes("replenish_type")) byId("rerun-replenish-type").value = p.replenish_type;
  byId("rerun-strategy").value = p.strategy;
  byId("rerun-layer-limit").value = p.max_tiles_per_layer;
  byId("rerun-max-layers").value = p.max_added_layers;
  byId("rerun-seed").value = p.seed;
  byId("rerun-retained-counts").value = (p.retained_counts || []).join(",");
  updateReplenishGuidance("rerun");
  clearInvalid(byId("rerun-form"));
  showFeedback("rerun-feedback", `已填入方案${index === 0 ? "一" : "二"}，请点击“重新生成并验收”。`, "success");
}

function rerunDefaults(form) {
  clearInvalid(form);
  if (state.rerunOrdinary) {
    const invalid = ORDINARY.validate(state.rerunOrdinary);
    if (invalid) {setInvalid(byId("rerun-ordinary-fields").querySelector(`[data-ordinary-field="${invalid.field}"]`), invalid.message);focusFirstInvalid(form);return null;}
    const newId = byId("rerun-new-id").value.trim();
    if (newId && !/^[A-Za-z0-9_-]+$/.test(newId)) {setInvalid(byId("rerun-new-id"), "新模板 ID 仅允许字母、数字、下划线和连字符。");focusFirstInvalid(form);return null;}
    const row = state.currentJob?.templates.find(item => item.key === state.rerunKey);
    return {mode:"ordinary_add",ordinary_add:structuredClone(state.rerunOrdinary),naming_rule:asText(row?.parameters.naming_rule,currentNamingRule("ordinary_add")),...(newId ? {new_id:newId} : {})};
  }
  if (state.rerunIncrease) {
    const error = INCREASE.validate(state.rerunIncrease);
    if (error) {const input = byId("rerun-increase-fields").querySelector(`[data-increase-field="${INCREASE.invalidField(state.rerunIncrease)}"]`);const disclosure = input.closest("details");if (disclosure) disclosure.open = true;setInvalid(input, error);focusFirstInvalid(form);return null;}
    const row = state.currentJob?.templates.find(item => item.key === state.rerunKey);
    const newId = byId("rerun-new-id").value.trim();
    if (newId && !/^[A-Za-z0-9_-]+$/.test(newId)) {setInvalid(byId("rerun-new-id"), "新模板 ID 仅允许字母、数字、下划线和连字符。");focusFirstInvalid(form);return null;}
    return {mode: "increase", increase: INCREASE.normalizeOptions(state.rerunIncrease), naming_rule: asText(row?.parameters.naming_rule, currentNamingRule("increase")), ...(newId ? {new_id: newId} : {})};
  }
  const minimum = Number(byId("rerun-min-ratio").value);
  const ratio = Number(byId("rerun-ratio").value);
  const replenish = byId("rerun-replenish-type").value;
  const layer = Number(byId("rerun-layer-limit").value);
  const levels = Number(byId("rerun-max-layers").value);
  const seed = Number(byId("rerun-seed").value);
  const newId = byId("rerun-new-id").value.trim();
  const retainedText = byId("rerun-retained-counts").value.trim();
  const retained = retainedText ? retainedText.split(",").map((part) => Number(part.trim())) : null;
  const sourceRow = state.currentJob?.templates.find((row) => row.key === state.rerunKey);
  const range = VM.validateRatioRangePercent(minimum, ratio,
    Number.isFinite(sourceRow?.oldRatio) ? sourceRow.oldRatio * 100 : null, sourceRow?.templateId);
  if (!range.ok) setInvalid(byId(range.field === "min_ratio" ? "rerun-min-ratio" : "rerun-ratio"), range.message);
  if (!["auto", "A", "B", "majority_A", "majority_B", "preserve"].includes(replenish)) setInvalid(byId("rerun-replenish-type"), "请选择补牌类型。");
  if (!Number.isInteger(layer) || layer < 1) setInvalid(byId("rerun-layer-limit"), "必须是正整数。");
  if (!Number.isInteger(levels) || levels < 0) setInvalid(byId("rerun-max-layers"), "必须是非负整数。");
  if (!Number.isInteger(seed)) setInvalid(byId("rerun-seed"), "必须是整数。");
  if (newId && !/^[A-Za-z0-9_-]+$/.test(newId)) setInvalid(byId("rerun-new-id"), "新模板 ID 仅允许字母、数字、下划线和连字符。");
  if (retained) {
    const sourceCounts = asArray(state.currentJob?.templates.find((row) => row.key === state.rerunKey)?.sourceTowerCounts);
    const validation = VM.validateRetainedCounts(retained, sourceCounts);
    if (!validation.ok) setInvalid(byId("rerun-retained-counts"), validation.message);
  }
  if (focusFirstInvalid(form)) return null;
  const defaults = {
    min_ratio: minimum / 100,
    ratio: ratio / 100,
    replenish_type: replenish,
    retained_counts: retained,
    strategy: byId("rerun-strategy").value,
    max_tiles_per_layer: layer,
    max_added_layers: levels,
    seed,
    naming_rule: asText(asObject(state.currentJob?.templates.find((row) => row.key === state.rerunKey)?.parameters).naming_rule, asText(state.settings.namingRule, "lessT{原模板ID}")),
  };
  if (newId) defaults.new_id = newId;
  return defaults;
}

async function submitRerun(event) {
  event.preventDefault();
  if (!state.currentJobId || !state.rerunKey) return;
  const defaults = rerunDefaults(event.currentTarget);
  if (!defaults) return showFeedback("rerun-feedback", "请修正标记的参数。", "error");
  const dialog = byId("rerun-dialog");
  const jobId = state.currentJobId;
  const keys = state.rerunKeys?.length ? [...state.rerunKeys] : [state.rerunKey];
  const bulk = Boolean(state.rerunBatch || keys.length > 1);
  const accepted = [], rejected = [];
  setDialogPending(dialog, true);
  try {
    invalidatePlans("模板正在重新生成");
    for (const key of keys) {
      const row = state.currentJob?.templates.find(item => item.key === key);
      const options = {...defaults};
      if (bulk) {
        delete options.new_id;
        const savedName = asText(row?.parameters.new_id, row?.outputId === "—" ? "" : asText(row?.outputId));
        if (savedName) options.new_id = savedName;
        options.naming_rule = asText(row?.parameters.naming_rule, defaults.naming_rule);
        options.retained_counts = row?.parameters.retained_counts ?? null;
      }
      try {
        await apiRequest(`/api/jobs/${encodeURIComponent(jobId)}/rerun/${encodeURIComponent(key)}`, {method:"POST",json:{defaults:options}});
        accepted.push(key);
      } catch (error) {rejected.push({key,message:error.message});}
    }
    if (state.currentJobId !== jobId) return;
    if (!rejected.length) {
      closeDialog(dialog, true);
      showToast(`已提交 ${accepted.length} 个模板重新生成，后台按队列处理。`, "success");
    } else {
      state.rerunKeys = rejected.map(item => item.key);
      state.rerunKey = state.rerunKeys[0];
      byId("confirm-rerun-button").textContent = `重试未提交的 ${rejected.length} 个模板`;
      showFeedback("rerun-feedback", `已提交 ${accepted.length} 个；${rejected.length} 个未确认提交：${rejected.map(item => item.message).join("；")}`, "error");
    }
    if (accepted.length) schedulePoll(100);
  } finally {
    setDialogPending(dialog, false);
  }
}

function renderHistory() {
  const rows = VM.normalizeHistoryRows(asObject(state.bootstrap).recent_jobs, state.currentJob);
  byId("history-table-body").innerHTML = rows.length ? rows.map((row) => `<tr><td>${escapeHTML(row.id)}</td><td>${escapeHTML(row.updated)}</td><td>${row.count}</td><td><span class="status-pill ${statusTone(row.status)}">${icon(statusTone(row.status) === "danger" ? "warning" : "status")}${escapeHTML(statusLabel(row.status))}</span></td><td><button class="row-action" type="button" data-open-job="${escapeHTML(row.id)}">查看任务</button></td></tr>`).join("") : '<tr><td colspan="5" class="table-empty">暂无任务记录。</td></tr>';
}

function exportOptions() {
  return {
    naming_rule: byId("export-naming-rule").value.trim(),
    export_root: byId("export-directory").value.trim(),
  };
}

function currentSourceTemplateIds() {
  return asArray(state.currentJob?.templates)
    .map((row) => asText(asObject(row).templateId))
    .filter((id) => id && id !== "—");
}

function validateNamingControl(control, originalIds = currentSourceTemplateIds()) {
  if (control === byId("export-naming-rule") && control?.dataset.edited !== "true") return {ok:true};
  const result = VM.validateNamingRule(control?.value, originalIds);
  if (!result.ok) setInvalid(control, result.message);
  return result;
}

function currentJobNamingRules() {
  if (!state.currentJob) return [];
  return [...new Set(state.currentJob.templates
    .filter(row => row.status !== "no_op")
    .map((row) => asText(asObject(row.parameters).naming_rule))
    .filter(Boolean))];
}

function deliverySelection() {
  const keys=state.resultSelection||[];
  const rows=state.currentJob?.templates;
  const records=(state.currentJob?.artifacts||[]).filter(item=>!rows||rows.some(row=>row.key===(item.template_key||item.templateKey)&&["verified","succeeded","exported"].includes(row.status)));
  const selected=keys.length?records.filter(item=>keys.includes(item.template_key||item.templateKey)):records;
  return {selected,keys:keys.length?[...new Set(selected.map(item=>item.template_key||item.templateKey))]:null};
}

function updateNamingRegenerationControl() {
  const requested = byId("export-naming-rule").value.trim();
  const rules = currentJobNamingRules();
  const differs = Boolean(state.currentJobId && requested && rules.some((rule) => rule !== requested));
  const scope=deliverySelection();
  const unavailable=state.batchCenter?"新模板正在批量居中，完成后可导出。":translationPending()?"阵型正在保存，完成后可导出。":state.namingRegenerationActive?"正在按新命名重新生成，请稍候。":!scope.selected.length?((state.resultSelection||[]).length?"所选模板中没有已验收产物，请勾选已通过项。":"尚无已验收产物可导出。"):"";
  byId("preview-export-button").disabled = Boolean(unavailable);
  byId("preview-export-bottom-button").disabled = byId("preview-export-button").disabled;
  const scopeText=(state.resultSelection||[]).length?`按勾选范围交付 ${scope.selected.length} 个已验收模板。`:`未勾选：交付全部 ${scope.selected.length} 个已验收模板。`;
  byId("export-availability-hint").textContent=unavailable||`${scopeText}${differs?" 命名变化只用于导出和ZIP，不重新生成阵型。":""}`;
  byId("download-zip-button").disabled=Boolean(unavailable)||state.zipBusy;
  byId("download-zip-button").textContent=state.zipBusy?"正在打包…":(state.resultSelection||[]).length?`下载所选已验收模板 ZIP（${scope.selected.length}）`:"下载全部已验收模板 ZIP";
  for (const id of ["preview-export-button", "preview-export-bottom-button"]) {
    byId(id).textContent = state.exportTask ? "重查上次写入结果" : state.exportPreviewRun ? "正在预览导出…" : state.exportPreviewTask ? "重查导出预览" : "预览导出";
    if (state.exportTask) byId(id).disabled = byId("export-preview-dialog").dataset.pending === "true";
    else if (state.exportPreviewTask) byId(id).disabled = Boolean(state.exportPreviewRun);
  }
  if (state.exportTask) byId("export-availability-hint").textContent = `上次写入结果待确认（${state.exportTask.exportRoot}），请先重查，再开始新的导出。`;
  if (differs) {
    setStatusPill(byId("export-status"), "新命名待导出预览", "neutral");
  }
}


function validateExport(form, checkNaming=true) {
  clearInvalid(form);
  const namingValidation = checkNaming && byId("export-naming-rule").dataset.edited === "true"
    ? validateNamingControl(byId("export-naming-rule")) : {ok:true};
  const exportDirectoryMissing = !byId("export-directory").value.trim();
  if (exportDirectoryMissing) setInvalid(byId("export-directory"), "请填写有效导出目录。");
  if (focusFirstInvalid(form)) {
    showFeedback("export-feedback", namingValidation.ok
      ? "请填写有效导出目录。"
      : namingValidation.message, "error");
    return false;
  }
  return true;
}

async function previewExport(event) {
  event.preventDefault();
  if (state.exportPreviewRun) return;
  if(state.batchCenter)return showFeedback("export-feedback","新模板正在批量居中，请完成后重新预览导出。","warning");
  if (state.exportTask) {
    if (byId("export-preview-dialog").dataset.pending === "true") return;
    renderExportPlan(state.exportTask.plan);
    openDialog(byId("export-preview-dialog"));
    return showFeedback("export-feedback", `请先重查上次向 ${state.exportTask.exportRoot} 的写入结果；此次不会开始新的导出。`, "warning");
  }
  if (translationPending()) return showFeedback("export-feedback", "阵型正在保存，请完成后重新预览导出。", "warning");
  clearFeedback("export-feedback");
  if (!state.currentJobId) return showFeedback("export-feedback", "当前没有可导出的任务。", "error");
  const requestedContext = planContextFingerprint();
  const requestedJobId = state.currentJobId;
  let operation = state.exportPreviewTask;
  if (operation && (operation.jobId !== requestedJobId || operation.context !== requestedContext)) {
    state.exportPreviewTask = null;
    operation = null;
  }
  if (!operation) {
    const options = exportOptions();
    if (!validateExport(event.currentTarget,true)) return;
    const scope=deliverySelection();
    if(!scope.selected.length)return showFeedback("export-feedback","所选范围没有已验收产物。","warning");
    operation = {jobId:requestedJobId,context:requestedContext,task_id:null,
      request:{action:"preview_start",request_id:globalThis.crypto?.randomUUID?.() || `preview-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        export_root:options.export_root,...(scope.keys?{template_keys:scope.keys}:{}),
        ...(byId("export-naming-rule").dataset.edited==="true"?{naming_rule:options.naming_rule}:{})},
      snapshot:{kind:"preview",phase:"preparing",status:"running",verified:0,verification_total:scope.selected.length}};
    state.exportPreviewTask = operation;
  }
  state.exportPreviewRun = operation;
  state.exportPlan = null;
  state.exportPlanContext = "";
  byId("export-plan-list").innerHTML='<p class="empty-copy">正在复核模板与导出目录…</p>';
  byId("export-plan-status").hidden=true;
  byId("export-plan-summary").textContent="预览只读，复核完成后可确认写入。";
  byId("commit-export-dialog-button").disabled=true;
  byId("commit-export-dialog-button").textContent="正在预览导出…";
  renderExportWriteProgress(operation.snapshot);
  openDialog(byId("export-preview-dialog"));
  setDialogPending(byId("export-preview-dialog"),true);
  updateNamingRegenerationControl();
  try {
    const task = operation.task_id ? operation.snapshot : asObject(await apiRequest(`/api/jobs/${encodeURIComponent(operation.jobId)}/export`, {
      method: "POST",
      json: operation.request,
    }));
    const payload = task.task_id ? await awaitExportPreview(operation,task) : task;
    if (state.exportPreviewTask !== operation) return;
    state.exportPreviewTask = null;
    if (!payload || state.exportTask || translationPending() || state.currentJobId !== requestedJobId || requestedContext !== planContextFingerprint()) {
      byId("export-write-progress").hidden=true;
      byId("export-plan-list").innerHTML='<p class="empty-copy">任务或导出选项已变化，请重新预览。</p>';
      return showFeedback("export-feedback","任务或导出选项已变化，请重新预览。","warning");
    }
    state.exportPlan = payload;
    state.exportPlanContext = planContextFingerprint();
    const items = asArray(payload.items);
    const renamed = items.filter(row => row.renamed).length;
    setStatusPill(byId("export-status"), renamed ? `${renamed} 个自动顺延名称` : "预览完成", "success");
    renderExportPlan(payload);
    openDialog(byId("export-preview-dialog"));
    showFeedback("export-feedback", `预览完成：${items.filter(row => row.classification === "create").length} 个文件等待写入${renamed ? `，其中 ${renamed} 个因同名已加版本后缀` : ""}。不会覆盖已有文件。`, "neutral");
  } catch (error) {
    if (operation.finished || (!operation.task_id && [400,403,404,409,429,503].includes(error.status))) state.exportPreviewTask=null;
    const message=state.exportPreviewTask===operation?`导出预览暂未确认：${error.message}。请点击“重查导出预览”恢复。`:`预览导出失败：${error.message}`;
    showFeedback("export-feedback",message,"error");
    byId("export-plan-status").hidden=false;
    byId("export-plan-status").querySelector("span").textContent=message;
  } finally {
    if (state.exportPreviewRun === operation) {
      state.exportPreviewRun=null;
      setDialogPending(byId("export-preview-dialog"),false);
      updateNamingRegenerationControl();
      updateExportCommitAvailability();
    }
  }
}

async function awaitExportPreview(operation,task) {
  let failures=0;
  while (true) {
    operation.task_id=task.task_id;
    operation.snapshot=task;
    if(state.exportPreviewTask!==operation || state.currentJobId!==operation.jobId || operation.context!==planContextFingerprint())return null;
    renderExportWriteProgress(task);
    if(task.status!=="running")break;
    await abortableDelay(300);
    try {
      task=asObject(await apiRequest(`/api/jobs/${encodeURIComponent(operation.jobId)}/export`,{
        method:"POST",json:{action:"status",task_id:operation.task_id}}));
      failures=0;
    } catch(error) { if(++failures>=3)throw error; }
  }
  operation.finished=true;
  if(task.result)return task.result;
  const error=new Error(task.error?.message||"导出预览未完成，请重新预览。");
  error.code=task.error?.code;
  throw error;
}

function conflictDecisions() {
  const decisions = {};
  all("[data-export-conflict]").forEach((select) => {
    if (select.value) decisions[select.dataset.exportConflict] = select.value;
  });
  return decisions;
}

function renderExportPlan(plan) {
  byId("export-write-progress").hidden=true;
  const items = asArray(asObject(plan).items);
  byId("export-plan-list").innerHTML = items.length ? items.map((raw, index) => {
    const row = asObject(raw);
    const classification = asText(row.classification, "ready");
    const conflict = classification === "conflict";
    const same = ["same", "identical", "same_sha"].includes(classification);
    const blocked = classification === "blocked";
    const noop = classification === "no_op";
    const description = row.renamed ? `名称 ${asText(row.original_filename)} 已被占用，已自动顺延为此名称；内部模板 ID 同步更新。` : noop ? "原模板无塔牌，跳过，不生成文件。" : conflict
      ? "目标位置已有不同内容"
      : same
        ? "内容相同，已存在"
        : blocked
          ? asText(row.error, "当前文件不可导出")
          : "新文件，可以写入";
    const sourceTemplate = asArray(asObject(state.currentJob).templates).find(item => item.key === row.template_key);
    const filename = asText(row.filename, asText(row.relative_name, asText(asObject(sourceTemplate).templateId, `文件 ${index + 1}`)));
    const outcome = noop ? '<span class="status-pill neutral">已跳过</span>' : blocked
      ? '<span class="status-pill danger">不可写入</span>'
      : conflict
        ? `<label for="export-conflict-${index}">处理方式<select id="export-conflict-${index}" data-export-conflict="${escapeHTML(asText(row.conflict_id, String(index)))}"><option value="skip">跳过此冲突</option><option value="backup_and_replace">备份并替换</option></select></label>`
        : `<span class="status-pill ${same ? "neutral" : "success"}">${same ? "将跳过" : "将写入"}</span>`;
    return `<article class="sync-item"><div><strong>${escapeHTML(filename)}</strong><p>${escapeHTML(description)}</p></div>${outcome}</article>`;
  }).join("") : '<p class="empty-copy">计划中没有可导出文件。</p>';
  byId("export-plan-status").hidden = true;
  if (state.exportTask?.snapshot) renderExportWriteProgress(state.exportTask.snapshot);
  updateExportCommitAvailability();
}

function updateExportCommitAvailability() {
  const selects = all("[data-export-conflict]");
  if(state.exportPreviewTask) {
    byId("commit-export-dialog-button").textContent=state.exportPreviewRun?"正在预览导出…":"重查导出预览";
    byId("commit-export-dialog-button").disabled=Boolean(state.exportPreviewRun);
    return;
  }
  if (state.exportTask) {
    byId("export-plan-summary").textContent = `上次写入目录：${state.exportTask.exportRoot}。请先重查结果，再开始新的导出。`;
    byId("commit-export-dialog-button").textContent = "重查上次写入结果";
    byId("commit-export-dialog-button").disabled = byId("export-preview-dialog").dataset.pending === "true";
    selects.forEach(select => { select.disabled = true; });
    return;
  }
  const items = asArray(asObject(state.exportPlan).items);
  const actionable = items.some((raw) => !["blocked", "no_op"].includes(asText(asObject(raw).classification, "ready")));
  const decisions=conflictDecisions();
  const count=items.filter(row=>!["blocked","no_op","same","identical","same_sha"].includes(row.classification) && (row.classification!=="conflict"||decisions[row.conflict_id]==="backup_and_replace")).length;
  byId("export-plan-summary").textContent=`共 ${items.length} 项 · 待写入 ${count} 个文件 · 其余 ${items.length-count} 项跳过或不可写入`;
  byId("commit-export-dialog-button").textContent=state.exportTask?"重查写入结果":`确认写入 ${count} 个文件`;
  byId("commit-export-dialog-button").disabled = !state.exportPlan || !actionable || selects.some((select) => !["skip", "backup_and_replace"].includes(select.value));
}

function renderExportWriteProgress(task) {
  const preview=task.kind==="preview";
  const phases={preparing:preview?"准备导出预览":"准备导出",preflight:"写入前复核",verifying:preview?"预览导出":"逐项复核并写入",writing:"正在写入",completed:preview?"预览完成":"写入完成",failed:preview?"预览未完成":"写入未全部完成"};
  const completed=Number(task.completed)||0,total=Number(task.total)||0;
  const verifying=preview || task.phase==="preflight";
  const known=task.verification_total!=null && Number.isFinite(Number(task.verification_total));
  const verified=Number(task.verified)||0,verificationTotal=Number(task.verification_total)||0;
  const count=task.phase==="preparing"?"正在启动任务…":verifying&&known?`已复核 ${verified} / ${verificationTotal} 个模板${preview?"":" · 尚未写入文件"}`:`已处理 ${completed} / ${total} 项`;
  byId("export-write-progress").hidden=false;
  byId("export-write-label").textContent=`${phases[task.phase]||"处理中"} · ${count}${task.current_filename?` · ${task.current_filename}`:""}`;
  const bar=byId("export-write-bar");bar.max=Math.max(verifying&&known?verificationTotal:total,1);
  if(task.phase!=="preparing" && (verifying&&known || ["verifying","writing","completed","failed"].includes(task.phase)))bar.value=verifying&&known?verified:completed;
  else bar.removeAttribute("value");
}

async function awaitExportWrite(jobId,task) {
  const path=`/api/jobs/${encodeURIComponent(jobId)}/export`;
  let failures=0;
  while(task.status==="running"){
    state.exportTask.task_id=task.task_id;state.exportTask.snapshot=task;renderExportWriteProgress(task);
    await abortableDelay(300);
    try{task=asObject(await apiRequest(path,{method:"POST",json:{action:"status",task_id:task.task_id}}));failures=0;}
    catch(error){if(++failures>=3)throw error;}
  }
  renderExportWriteProgress(task);state.exportTask=null;
  if(task.result)return task.result;
  const error=new Error(task.error?.message||"写入结果未返回，请重新预览核对目录。");error.code=task.error?.code;throw error;
}

function summarizeCommitItems(items) {
  return VM.summarizeCommitItems(items);
}

async function commitExport() {
  if(state.batchCenter)return;
  if(state.exportPreviewTask)return previewExport({preventDefault(){},currentTarget:byId("export-form")});
  if ((!state.exportTask && translationPending()) || byId("export-preview-dialog").dataset.pending === "true") return;
  const recovery = state.exportTask;
  const plan = asObject(recovery?.plan || state.exportPlan);
  if (!recovery && (!state.currentJobId || !plan.plan_id || !plan.confirmation_token)) return;
  if (!recovery && state.exportPlanContext !== planContextFingerprint()) {
    invalidatePlans("任务、命名、目录或产物已变化");
    closeDialog(byId("export-preview-dialog"));
    return showFeedback("export-feedback", "当前导出计划已失效，请重新预览。", "warning");
  }
  const button = byId("preview-export-button");
  const dialogButton = byId("commit-export-dialog-button");
  button.disabled = true;
  dialogButton.disabled = true;
  byId("preview-export-bottom-button").disabled = true;
  setDialogPending(byId("export-preview-dialog"), true);
  // Retain the exact confirmed request before sending it: a lost response
  // must recover this operation, never create another version from a new plan.
  const operation = recovery || {
    jobId: state.currentJobId, planId: plan.plan_id, exportRoot: plan.export_root,
    plan, task_id: null, snapshot: null,
    request: {action:"start", plan_id:plan.plan_id, confirmation_token:plan.confirmation_token,
      conflict_decisions:conflictDecisions()},
  };
  state.exportTask = operation;
  operation.snapshot ||= {kind:"write",phase:"preparing",completed:0,total:asArray(plan.items).length};
  renderExportWriteProgress(operation.snapshot);
  try {
    const task = operation.task_id ? operation.snapshot : asObject(await apiRequest(`/api/jobs/${encodeURIComponent(operation.jobId)}/export`, {
      method: "POST",
      json: operation.request,
    }));
    const payload=task.task_id?await awaitExportWrite(operation.jobId,task):task;
    state.exportTask=null;
    const summary = summarizeCommitItems(payload.items);
    const failed = summary.failed + summary.rolledBack;
    if (state.exportPlan?.plan_id === operation.planId) {
      state.exportPlan = null;
      state.exportPlanContext = "";
    }
    setStatusPill(byId("export-status"), failed ? "导出部分失败" : "导出已处理", failed ? "warning" : "success");
    const renamed = asArray(payload.items).filter(row => row.renamed && row.status === "exported").map(row => asText(row.filename));
    showFeedback("export-feedback", `目录 ${operation.exportRoot}：写入 ${summary.written} 个，跳过 ${summary.skipped} 个，失败 ${summary.failed} 个。${renamed.length ? `同名已顺延为：${renamed.join("、")}。` : ""}未覆盖已有文件。`, failed ? "warning" : "neutral");
    closeDialog(byId("export-preview-dialog"), true);
    if (state.currentJobId === operation.jobId) {
      state.syncPlan = null;
      state.syncPlanContext = "";
      try { await loadJob(operation.jobId, false, true); }
      catch (_) { showToast("写入结果已确认，任务显示刷新失败，可稍后重新打开任务。", "warning"); }
    }
    if (!failed && payload.ok === true && summary.written > 0) {
      showDeliveryCompletion("导出成功", `已导出 ${summary.written} 个模板至 ${operation.exportRoot}。`,
        asArray(payload.directories).length ? payload.directories : [{label: "输出目录", path: operation.exportRoot}], "preview-export-button");
    } else if (failed) {
      showToast("导出部分失败", "warning", {placement: "center",
        detail: `已写入 ${summary.written} 个，失败 ${summary.failed} 个。请查看页面中的逐项结果。`});
    }
  } catch (error) {
    if (!recovery && !operation.task_id && error.status && [400,403,404,409,429,503].includes(error.status)) state.exportTask=null;
    if (!state.exportTask && state.exportPlan?.plan_id === operation.planId) {
      state.exportPlan=null;state.exportPlanContext="";
    }
    const message = state.exportTask
      ? `向 ${operation.exportRoot} 的写入结果暂未确认：${error.message}。请点击“重查上次写入结果”；不会重复开始导出。`
      : error.code === "plan_expired" || error.code === "target_changed" ? "计划已过期或目标已变化，请重新预览导出。" : `导出失败：${error.message}`;
    showFeedback("export-feedback", message, "error");
    const planStatus = byId("export-plan-status");
    planStatus.hidden = false;
    planStatus.querySelector("span").textContent = message;
  } finally {
    setDialogPending(byId("export-preview-dialog"), false);
    updateNamingRegenerationControl();
    updateExportCommitAvailability();
  }
}

function selectedBindings() {
  return all("input[name='sync_targets']:checked").map((input) => input.value);
}

function renderSyncPlan(plan) {
  const rows = asArray(plan.rows || plan.items);
  const entries = VM.normalizeSyncPlanEntries(rows);
  const bindingLabel = {template: "模板仓", tile: "Tile 仓"};
  byId("sync-plan-list").innerHTML = entries.length ? entries.map((row, index) => {
    const classification = asText(row.classification, asText(row.status, "ready"));
    const conflict = classification === "conflict";
    const same = ["same", "identical", "same_sha"].includes(classification);
    const blocked = classification === "blocked";
    const binding = asText(row.binding_key, asText(row.target));
    const targetName = bindingLabel[binding] || binding || "同步目标";
    const path = asText(row.binding_path, asText(row.path));
    const description = conflict
      ? `${targetName}已有不同内容`
      : same
        ? `${targetName}内容相同，已存在`
        : blocked
          ? asText(asObject(row.error).message, asText(asObject(row.parentError).message, `${targetName}当前不可写入`))
          : `${targetName}可以同步`;
    const pathCopy = path ? `<p>${escapeHTML(path)}</p>` : "";
    const outcome = blocked
      ? '<span class="status-pill danger">不可同步</span>'
      : conflict
        ? `<label for="sync-conflict-${index}">处理方式<select id="sync-conflict-${index}" data-sync-conflict="${escapeHTML(asText(row.conflict_id, String(index)))}"><option value="">暂不处理</option><option value="backup_and_replace">备份并替换</option></select></label>`
        : `<span class="status-pill ${same ? "neutral" : "success"}">${same ? "将跳过" : "将写入"}</span>`;
    return `<article class="sync-item"><div><strong>${escapeHTML(asText(row.filename, `文件 ${index + 1}`))} · ${escapeHTML(targetName)}</strong><p>${escapeHTML(description)}</p>${pathCopy}</div>${outcome}</article>`;
  }).join("") : '<p class="empty-copy">计划中没有可同步文件。</p>';
  const actionable = entries.some((row) => asText(row.classification, asText(row.status, "ready")) !== "blocked");
  byId("commit-sync-button").disabled = !actionable || entries.some((row) => asText(row.classification, asText(row.status)) === "conflict");
}

async function previewSync() {
  if(state.batchCenter)return showFeedback("export-feedback","新模板正在批量居中，请完成后重新预览同步。","warning");
  if (translationPending()) return showFeedback("export-feedback", "阵型正在保存，请完成后重新预览同步。", "warning");
  if (!state.currentJobId) return showFeedback("export-feedback", "当前没有可同步的任务。", "error");
  const bindings = selectedBindings();
  if (!bindings.length) return showFeedback("export-feedback", "请至少选择模板仓或 Tile 仓。", "error");
  const requestedContext = planContextFingerprint();
  const requestedJobId = state.currentJobId;
  try {
    const plan = asObject(await apiRequest("/api/sync/plan", {
      method: "POST",
      json: {job_id: state.currentJobId, bindings},
    }));
    if (translationPending() || state.currentJobId !== requestedJobId || requestedContext !== planContextFingerprint()) return;
    state.syncPlan = plan;
    state.syncPlanContext = planContextFingerprint() + JSON.stringify(bindings.slice().sort());
    renderSyncPlan(plan);
    byId("sync-recovery-status").hidden = true;
    openDialog(byId("sync-dialog"));
  } catch (error) {
    showFeedback("export-feedback", error.code === "plan_expired" ? "计划已过期，请重新预览同步。" : `预览同步失败：${error.message}`, "error");
  }
}

function updateSyncCommitAvailability() {
  const selects = all("[data-sync-conflict]");
  const rows = asArray(asObject(state.syncPlan).rows || asObject(state.syncPlan).items);
  const entries = VM.normalizeSyncPlanEntries(rows);
  const actionable = entries.some((row) => asText(row.classification, "ready") !== "blocked");
  byId("commit-sync-button").disabled = !state.syncPlan || !actionable || selects.some((select) => !select.value);
}

async function commitSync() {
  if(state.batchCenter)return;
  if (translationPending()) return;
  const plan = asObject(state.syncPlan);
  if (!plan.plan_id || !plan.confirmation_token) return;
  const currentContext = planContextFingerprint() + JSON.stringify(selectedBindings().slice().sort());
  if (state.syncPlanContext !== currentContext) {
    invalidatePlans("任务、产物或同步目标已变化");
    closeDialog(byId("sync-dialog"));
    return showFeedback("export-feedback", "当前同步计划已失效，请重新预览。", "warning");
  }
  const conflictConfirmations = {};
  all("[data-sync-conflict]").forEach((select) => {
    if (select.value) conflictConfirmations[select.dataset.syncConflict] = select.value;
  });
  const button = byId("commit-sync-button");
  button.disabled = true;
  setDialogPending(byId("sync-dialog"), true);
  try {
    const result = asObject(await apiRequest("/api/sync/commit", {
      method: "POST",
      json: {plan_id: plan.plan_id, confirmation_token: plan.confirmation_token, conflict_confirmations: conflictConfirmations},
    }));
    const outcome = VM.interpretSyncReport(result);
    const summary = outcome.summary;
    const recovery = byId("sync-recovery-status");
    if (outcome.confirmationRequired) {
      recovery.hidden = false;
      const missing = outcome.requiredConflictIds.length ? `（仍缺 ${outcome.requiredConflictIds.length} 项）` : "";
      recovery.querySelector("span").textContent = `仍有冲突未确认${missing}；已保留当前预览和确认令牌，请补齐处理方式后重试。`;
      return;
    }
    if (outcome.consumePlan) {
      state.syncPlan = null;
      state.syncPlanContext = "";
    }
    if (outcome.auditFailed) {
      recovery.hidden = false;
      recovery.querySelector("span").textContent = `文件写入可能已完成：写入 ${summary.written} 项、跳过 ${summary.skipped} 项、回滚 ${summary.rolledBack} 项，但审计记录未落盘；同步已锁定。请停止操作并人工核对任务编号、目标文件、日志和备份位置。`;
      return;
    }
    if (outcome.recoveryRequired) {
      recovery.hidden = false;
      recovery.querySelector("span").textContent = `同步需要人工处理：写入 ${summary.written} 项、跳过 ${summary.skipped} 项、失败 ${summary.failed} 项、回滚 ${summary.rolledBack} 项。请停止操作，保留任务编号、日志和备份位置。`;
      return;
    }
    if (outcome.allWriteAttemptsRolledBack) {
      recovery.hidden = false;
      recovery.querySelector("span").textContent = `同步失败，写入尝试均已回滚；写入 ${summary.written} 项、跳过 ${summary.skipped} 项、失败 ${summary.failed} 项、回滚 ${summary.rolledBack} 项，没有保留部分同步结果。请修复原因后重新预览。`;
      return;
    }
    const failed = summary.failed + summary.rolledBack;
    if (outcome.closeDialog) closeDialog(byId("sync-dialog"), true);
    const mixedNote = outcome.hasMixedCommittedAndRolledBack ? "部分模板已同步，另有模板已回滚；请按逐项结果核对。" : "";
    showFeedback("export-feedback", `同步写入 ${summary.written} 项，跳过 ${summary.skipped} 项，失败 ${summary.failed} 项，回滚 ${summary.rolledBack} 项，生成备份 ${summary.backups} 个。${mixedNote}工具未执行 Git 操作。`, failed ? "warning" : "neutral");
    if (!failed && outcome.status === "committed" && summary.written + summary.skipped > 0) {
      const paths = [...new Set(VM.normalizeSyncPlanEntries(asArray(plan.rows || plan.items))
        .filter(row => row.classification !== "blocked")
        .map(row => asText(row.binding_path, asText(row.path))).filter(Boolean))];
      showDeliveryCompletion(summary.written ? "同步成功" : "同步完成",
        summary.written ? `已同步写入 ${summary.written} 项，内容相同跳过 ${summary.skipped} 项。` : "目标内容已相同，无需重复写入。",
        asArray(result.directories).length ? result.directories : paths.map(path => ({label: "仓库", path})), "preview-sync-button");
    } else if (failed) {
      showToast("同步部分失败", "warning", {placement: "center",
        detail: `已写入 ${summary.written} 项，失败 ${summary.failed} 项，回滚 ${summary.rolledBack} 项。请查看页面中的逐项结果。`});
    }
  } catch (error) {
    const recovery = byId("sync-recovery-status");
    recovery.hidden = false;
    if (error.code === "plan_expired" || error.code === "target_changed") {
      recovery.querySelector("span").textContent = "计划已过期，请关闭弹窗并重新预览同步。";
    } else if (error.code === "rollback_failed") {
      recovery.querySelector("span").textContent = "回滚失败，需要人工处理。请停止操作，保留任务编号、日志和备份位置。";
    } else {
      recovery.querySelector("span").textContent = `同步失败：${error.message}`;
    }
    button.disabled = false;
  } finally {
    setDialogPending(byId("sync-dialog"), false);
    if (state.syncPlan) updateSyncCommitAvailability();
  }
}

async function downloadBundle() {
  if(state.batchCenter)return;
  if (translationPending()) return showToast("阵型正在保存，请完成后再下载。", "warning");
  if (!state.currentJobId||state.zipBusy) return;
  const scope=deliverySelection();
  if(!scope.selected.length)return showToast("所选范围没有已验收产物。","warning");
  state.zipBusy=true;updateNamingRegenerationControl();
  try{
    const blob=await apiRequest(`/api/jobs/${encodeURIComponent(state.currentJobId)}/download/bundle`,{method:"POST",json:{...(scope.keys?{template_keys:scope.keys}:{}),...(byId("export-naming-rule").dataset.edited==="true"?{naming_rule:byId("export-naming-rule").value.trim()}:{})},binary:true});
    const url=URL.createObjectURL(blob),link=document.createElement("a");
    link.href=url;link.download="tower-templates.zip";link.click();
    window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){showToast(`下载失败：${error.message}`,"error");}
  finally{state.zipBusy=false;updateNamingRegenerationControl();}
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  clearInvalid(form);
  const tileRoot = byId("settings-tile-path").value.trim();
  const templateRoot = byId("settings-template-path").value.trim();
  const exportRoot = byId("settings-export-directory").value.trim();
  const namingRule = byId("settings-naming-rule").value.trim();
  const increaseNamingRule = byId("settings-increase-naming-rule").value.trim();
  if (tileRoot && !tileRoot.endsWith(TILE_PATH_SUFFIX)) setInvalid(byId("settings-tile-path"));
  if (templateRoot && !templateRoot.endsWith(TEMPLATE_PATH_SUFFIX)) setInvalid(byId("settings-template-path"));
  if (!exportRoot) setInvalid(byId("settings-export-directory"));
  const namingValidation = validateNamingControl(byId("settings-naming-rule"), []);
  const increaseNamingValidation = validateNamingControl(byId("settings-increase-naming-rule"), []);
  if (focusFirstInvalid(form)) {
    showToast(namingValidation.ok && increaseNamingValidation.ok
      ? "请检查路径后缀和导出目录。"
      : !namingValidation.ok ? namingValidation.message : increaseNamingValidation.message, "error");
    return;
  }
  try {
    const body = {
      tile_root: tileRoot,
      template_root: templateRoot,
      config_root: byId("settings-config-root")?.value.trim() || "",
      export_root: exportRoot,
      naming_rule: namingRule,
      increase_naming_rule: increaseNamingRule,
      default_sync_targets: all("input[name='default_sync_targets']:checked").map(input => input.value),
      tile_confirmed: Boolean(tileRoot),
      template_confirmed: Boolean(templateRoot),
    };
    if (state.settings.loadError) body.recover = true;
    state.settings = normalizeSettings(await apiRequest("/api/settings", {method: "PUT", json: body}));
    populatePaths(state.settings, true);
    window.towerParameterUI?.contextChanged();
    showToast("设置已保存，两个模板库路径已确认。");
  } catch (error) {
    showToast(`保存设置失败：${error.message}`, "error");
  }
}

async function confirmBindings() {
  const tileRoot = candidateChoice("tile") || VM.resolveCandidate(state.settings.candidates.tile, state.settings.tileRoot);
  const templateRoot = candidateChoice("template") || VM.resolveCandidate(state.settings.candidates.template, state.settings.templateRoot);
  if (!tileRoot && !templateRoot) {
    closeDialog(byId("binding-dialog"));
    routeTo("settings");
    showToast("未发现完整模板库路径，请手动绑定。", "error");
    return;
  }
  try {
    state.settings = normalizeSettings(await apiRequest("/api/settings", {
      method: "PUT",
      json: {tile_root: tileRoot, template_root: templateRoot, tile_confirmed: Boolean(tileRoot), template_confirmed: Boolean(templateRoot)},
    }));
    populatePaths(state.settings, true);
    closeDialog(byId("binding-dialog"));
    showToast("模板库路径已确认。");
  } catch (error) {
    showToast(`确认路径失败：${error.message}`, "error");
  }
}

async function rediscoverPaths() {
  try {
    const payload = await apiRequest("/api/settings");
    state.settings = normalizeSettings(payload);
    state.settings.tileRoot = VM.resolveCandidate(state.settings.candidates.tile, state.settings.tileRoot);
    state.settings.templateRoot = VM.resolveCandidate(state.settings.candidates.template, state.settings.templateRoot);
    populatePaths(state.settings);
    renderPathDiscovery(state.settings);
    openDialog(byId("binding-dialog"));
    showToast("已重新发现候选路径，多个候选不会自动选择。");
  } catch (error) {
    showToast(`重新发现路径失败：${error.message}`, "error");
  }
}

function restartWorkspace() {
  if (window.towerParameterUI?.workflow.writeLocked?.() || window.towerParameterUI?.workflow.state.pending === "commit") {
    const message = "正在追加关卡配置，请等待写入结果后再重新开始。";
    showFeedback("parameter-preview-feedback", message, "warning");
    showToast(message, "warning");
    return;
  }
  window.towerParameterUI?.reset();
  state.importController?.abort();
  state.importController = null;
  state.importEpoch += 1;
  setImportBusy(false);
  resetImportSession();
  cancelBatchRegeneration();
  cancelPolling();
  safeStorage.remove("tower-reducer-current-job");
  state.selectedFiles = [];
  state.importedFiles = [];
  state.importId = "";
  state.overrides = {};
  state.increaseOverrides = {};
  state.increaseDefaults = INCREASE.defaults();
  state.ordinaryOverrides = {};
  state.ordinaryDefaults = ORDINARY.defaults();
  state.workbenchContexts = {};
  clearTimeout(state.increaseDraftTimer);
  window.towerIncreaseDraft?.cancel();
  window.towerIncreasePreview?.cancel();
  delete byId("export-naming-rule").dataset.edited;
  byId("export-naming-rule").value = currentNamingRule();
  state.activeOverride = "";
  state.currentJob = null;
  state.currentJobId = "";
  applySyncDefaults(state.settings, true);
  state.exportPlan = null;
  state.syncPlan = null;
  state.namingRegenerationActive = false;
  byId("template-files").value = "";
  byId("template-ids").value = "";
  byId("import-results").hidden = true;
  byId("import-issues-summary").hidden = true;
  updateSelectedFiles([], false);
  renderConfigurationList([]);
  renderJob(normalizeJob({status: "unknown", templates: [], logs: []}));
  closeDialog(byId("restart-dialog"));
  routeTo(state.mode === "ordinary_add" ? "ordinary_add" : "workspace");
  showStep(1);
  showToast("已清空当前任务，可重新导入模板。");
}

function initializeHelp() {
  const topics = window.TowerReducerHelp || [];
  const groups = [...new Set(topics.map((topic) => topic.group))];
  byId("help-navigation").innerHTML = groups.map((group) => `<div class="help-nav-group"><strong>${escapeHTML(group)}</strong>${topics.filter((topic) => topic.group === group).map((topic) => `<button type="button" data-help-topic="${escapeHTML(topic.id)}">${escapeHTML(topic.title)}</button>`).join("")}</div>`).join("");
}

function renderHelpDocument() {
  const topics = window.TowerReducerHelp || [];
  byId("help-reading").innerHTML = topics.map((topic) => `<section class="help-chapter" id="help-section-${escapeHTML(topic.id)}" data-help-section="${escapeHTML(topic.id)}" aria-labelledby="help-title-${escapeHTML(topic.id)}"><h3 class="help-topic-title" id="help-title-${escapeHTML(topic.id)}" tabindex="-1">${escapeHTML(topic.title)}</h3><p class="help-topic-intro">${escapeHTML(topic.intro)}</p>${topic.body}</section>`).join("");
}

function setHelpReadingTopic(id) {
  const topic = (window.TowerReducerHelp || []).find((item) => item.id === id);
  if (!topic) return;
  state.helpTopic = id;
  byId("help-current-topic").textContent = topic.title;
  all("[data-help-topic]", byId("help-navigation")).forEach((button) => {
    const active = button.dataset.helpTopic === id;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "location");
    else button.removeAttribute("aria-current");
  });
}

function renderHelpTopic(id, focus = false) {
  if (!(window.TowerReducerHelp || []).some((topic) => topic.id === id)) return;
  byId("help-search-input").value = "";
  byId("clear-help-search").hidden = true;
  byId("help-search-summary").hidden = true;
  if (!byId(`help-section-${id}`)) renderHelpDocument();
  setHelpReadingTopic(id);
  const content = byId("help-content");
  const section = byId(`help-section-${id}`);
  const inset = parseFloat(getComputedStyle(content).paddingTop) || 0;
  const top = section.getBoundingClientRect().top - content.getBoundingClientRect().top + content.scrollTop - inset;
  content.scrollTo({top: Math.max(0, top), behavior: VM.navigationScrollBehavior(window.matchMedia("(prefers-reduced-motion: reduce)").matches)});
  if (focus) section.querySelector(".help-topic-title").focus({preventScroll: true});
}

function updateHelpReadingPosition() {
  if (byId("help-search-input").value.trim()) return;
  const sections = all("[data-help-section]", byId("help-reading"));
  if (!sections.length) return;
  const content = byId("help-content");
  const top = content.getBoundingClientRect().top + (parseFloat(getComputedStyle(content).paddingTop) || 0) + 24;
  let active = sections[0];
  for (const section of sections) {
    if (section.getBoundingClientRect().top > top) break;
    active = section;
  }
  setHelpReadingTopic(active.dataset.helpSection);
}

function openHelp() {
  const view = document.querySelector("[data-view]:not([hidden])")?.dataset.view;
  const step = document.querySelector("[data-flow]:not([hidden])")?.dataset.flow;
  const topic = view === "parameters" ? "parameters" : view === "settings" || view === "history" ? "settings" : state.mode === "ordinary_add" ? "ordinary_add" : step === "1" ? "import" : step === "2" ? "config" : TERMINAL_JOB_STATES.has(state.currentJob?.status) ? "export" : "generate";
  openDialog(byId("help-dialog"));
  renderHelpTopic(topic);
  byId("help-search-input").focus();
}

function searchHelp() {
  const query = byId("help-search-input").value.trim();
  byId("clear-help-search").hidden = !query;
  if (!query) return renderHelpTopic(state.helpTopic);
  const topics = VM.searchHelpTopics(window.TowerReducerHelp || [], query);
  byId("help-current-topic").textContent = "搜索结果";
  byId("help-search-summary").hidden = false;
  byId("help-search-summary").textContent = `找到 ${topics.length} 个相关章节`;
  byId("help-reading").innerHTML = topics.length ? topics.map((topic) => `<button class="help-search-result" type="button" data-help-topic="${escapeHTML(topic.id)}"><strong>${escapeHTML(topic.title)}</strong><span>${escapeHTML(topic.intro)}</span></button>`).join("") : '<p class="empty-copy">没有匹配的说明。可换用“逐塔”“导出”“命名”等关键词。</p>';
  byId("help-content").scrollTop = 0;
}

function bindEvents() {
  bindManualEditor();
  byId("completion-directories").addEventListener("click", event => {
    const button = event.target.closest("[data-directory-key]");
    if (button) openCompletedDirectory(button);
  });
  all("[data-route]").forEach((control) => control.addEventListener("click", () => {
    const enclosingDialog = control.closest("dialog");
    if (enclosingDialog) closeDialog(enclosingDialog);
    routeTo(control.dataset.route);
  }));
  all("[data-go-step]").forEach((button) => button.addEventListener("click", () => showStep(button.dataset.goStep)));
  all("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => closeDialog(byId(button.dataset.closeDialog))));
  all("dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (dialog.dataset.pending === "true") return;
    closeDialog(dialog);
  }));
  byId("brand-home").addEventListener("click", () => routeTo("workspace"));
  byId("help-button").addEventListener("click", openHelp);
  byId("help-search-input").addEventListener("input", searchHelp);
  byId("help-content").addEventListener("scroll", updateHelpReadingPosition, {passive: true});
  byId("clear-help-search").addEventListener("click", () => { renderHelpTopic(state.helpTopic); byId("help-search-input").focus(); });
  byId("help-dialog").addEventListener("click", (event) => {
    const button = event.target.closest("[data-help-topic]");
    if (button) renderHelpTopic(button.dataset.helpTopic, true);
  });
  all("[data-import-mode]").forEach((button) => {
    button.addEventListener("click", () => setImportMode(button.dataset.importMode));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const mode = event.key === "Home" ? "ids" : event.key === "End" ? "files" : state.importMode === "ids" ? "files" : "ids";
      setImportMode(mode);
      byId(`import-${mode}-tab`).focus();
    });
  });
  byId("template-ids").addEventListener("input", () => { state.importId = ""; resetImportSession(); updateImportControls(); });
  byId("source-repository").addEventListener("change", () => { state.importId = ""; resetImportSession(); updateImportControls(); });
  byId("restart-button").addEventListener("click", () => {
    if (window.towerParameterUI?.workflow.writeLocked?.() || window.towerParameterUI?.workflow.state.pending === "commit") restartWorkspace();
    else openDialog(byId("restart-dialog"));
  });
  byId("confirm-restart-button").addEventListener("click", restartWorkspace);
  byId("skip-bindings")?.addEventListener("click", () => {
    sessionStorage.setItem("tower.pathsDeferred", "1");
    closeDialog(byId("binding-dialog"));
  });
  byId("confirm-bindings-button").addEventListener("click", confirmBindings);
  byId("rediscover-button").addEventListener("click", rediscoverPaths);
  byId("template-files").addEventListener("change", (event) => {
    updateSelectedFiles(Array.from(event.target.files || []), true);
    event.target.value = "";
  });
  byId("drop-zone").addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("is-dragging"); });
  byId("drop-zone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("is-dragging"));
  byId("drop-zone").addEventListener("drop", (event) => {
    event.preventDefault();
    event.currentTarget.classList.remove("is-dragging");
    updateSelectedFiles(Array.from(event.dataTransfer?.files || []), true);
  });
  byId("file-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-file]");
    if (!button) return;
    const next = [...state.selectedFiles];
    next.splice(Number(button.dataset.removeFile), 1);
    updateSelectedFiles(next, false);
  });
  byId("import-form").addEventListener("submit", importTemplates);
  byId("configuration-form").addEventListener("submit", createJob);
  document.querySelectorAll("[data-pick-path]").forEach(button => {
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.pickPath);
      const feedback = button.closest("dialog")?.querySelector("[role=status]");
      button.disabled = true;
      if (feedback) feedback.hidden = true;
      try {
        const payload = await apiRequest("/api/dialog/directory", { method: "POST", json: {} });
        if (payload.path) {
          input.value = payload.path;
        }
      } catch (error) {
        if (feedback) {
          feedback.textContent = error.message;
          feedback.hidden = false;
        } else {
          showToast(error.message, "error");
        }
      } finally {
        button.disabled = false;
      }
    });
  });
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("export-form").addEventListener("submit", previewExport);
  byId("export-naming-rule").addEventListener("input", () => { byId("export-naming-rule").dataset.edited = "true";invalidatePlans("命名规则已变更"); updateNamingRegenerationControl(); });
  byId("export-directory").addEventListener("input", () => invalidatePlans("导出目录已变更"));
  byId("commit-export-dialog-button").addEventListener("click", commitExport);
  byId("preview-sync-button").addEventListener("click", previewSync);
  byId("commit-sync-button").addEventListener("click", commitSync);
  byId("download-zip-button").addEventListener("click", downloadBundle);
  byId("refresh-history").addEventListener("click", initialize);
  byId("placement-strategy").addEventListener("change", (event) => {
    byId("seed-field").hidden = event.target.value !== "seeded_random";
  });
  byId("tower-mode").addEventListener("change", (event) => {
    byId("advanced-tower-card").hidden = event.target.value !== "advanced";
  });
  byId("template-config-list").addEventListener("click", (event) => {
    const preview = event.target.closest("[data-preview-import]");
    if (preview) openTemplatePreview(preview.dataset.previewImport, "import");
    const button = event.target.closest("[data-override-mode]");
    if (button) setOverrideMode(button.dataset.fileId, button.dataset.overrideMode);
  });
  byId("parameter-override-editor").addEventListener("input", (event) => {
    const input = event.target.closest("[data-override-field]");
    if (!input) return;
    const fileId = input.dataset.fileId;
    state.overrides[fileId] = state.overrides[fileId] || {};
    state.overrides[fileId][input.dataset.overrideField] = input.dataset.valueKind === "ratio"
      ? Number(input.value) / 100
      : input.dataset.valueKind === "number" ? Number(input.value) : input.value;
    if (input.dataset.overrideField === "strategy") byId("parameter-override-editor").querySelector("[data-override-seed]").hidden = input.value !== "seeded_random";
    if (input.dataset.overrideField === "replenish_type") updateReplenishGuidance("override");
    updateParameterSummaries();
  });
  byId("parameter-override-editor").addEventListener("click", (event) => {
    const button = event.target.closest("[data-apply-replenish]");
    if (button) applyReplenishRecommendation("override");
  });
  byId("edit-batch-parameters").addEventListener("click", showBatchEditor);
  byId("restore-batch-parameters").addEventListener("click", () => setOverrideMode(state.activeOverride, "reset"));
  byId("result-table-body").addEventListener("click", (event) => {
    if (event.target.closest("[data-download-artifact]") && translationPending()) {
      event.preventDefault();
      return showToast("阵型正在保存，请完成后再下载。", "warning");
    }
    const advice = event.target.closest("[data-apply-increase-advice]");
    if (advice) return applyIncreaseAdvice(advice.dataset.adviceTemplate, Number(advice.dataset.applyIncreaseAdvice), advice.dataset.adviceContext);
    const rerun = event.target.closest("[data-rerun-template]");
    if (rerun) openRerunEditor(rerun.dataset.rerunTemplate);
    const recovery = event.target.closest("[data-manual-recovery]");
    if (recovery) openTemplatePreview(recovery.dataset.manualRecovery,"recovery");
    const preview = event.target.closest("[data-preview-template]");
    if (preview) openTemplatePreview(preview.dataset.previewTemplate);
    if (event.target.closest("[data-reimport-template]")) showStep(1);
  });
  byId("result-table-body").addEventListener("change", event => {
    const input = event.target.closest("[data-select-result-template]");
    if (!input) return;
    state.resultSelection = (state.resultSelection || []).filter(key => key !== input.dataset.selectResultTemplate);
    if (input.checked) state.resultSelection.push(input.dataset.selectResultTemplate);
    updateResultSelection();
  });
  byId("select-all-result-templates").addEventListener("change", event => {
    state.resultSelection = event.target.checked ? state.currentJob.templates.filter(row => VM.canRerunTemplate(row)).map(row => row.key) : [];
    updateResultSelection();
  });
  byId("select-failed-templates").addEventListener("click", () => {
    state.resultSelection = state.currentJob.templates.filter(row => VM.canRerunTemplate(row) && ["failed","interrupted"].includes(row.status)).map(row => row.key);
    updateResultSelection();
  });
  byId("clear-result-selection").addEventListener("click", () => {state.resultSelection=[];updateResultSelection();});
  byId("batch-rerun-button").addEventListener("click", openSelectedRerunEditor);
  byId("batch-center-button").addEventListener("click", centerAllTemplates);
  byId("history-table-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-job]");
    if (button) loadJob(button.dataset.openJob);
  });
  byId("preview-mark-changes").addEventListener("change", renderTemplatePreview);
  byId("preview-layer").addEventListener("input", event => setPreviewLayer(event.target.value));
  byId("preview-layer-minus").addEventListener("click", () => stepPreviewLayer(-1));
  byId("preview-layer-plus").addEventListener("click", () => stepPreviewLayer(1));
  byId("preview-layer-unlimited").addEventListener("click",showAllPreviewLayers);
  byId("preview-single-layer").addEventListener("change",renderTemplatePreview);
  byId("preview-translation").addEventListener("click", event => {
    const button = event.target.closest("[data-translate-preview]");
    if (button && !button.disabled) translateTemplatePreview(button.dataset.translatePreview);
  });
  byId("template-preview-dialog").addEventListener("close", cancelTemplatePreview);
  byId("sync-plan-list").addEventListener("change", updateSyncCommitAvailability);
  byId("export-plan-list").addEventListener("change", updateExportCommitAvailability);
  byId("rerun-form").addEventListener("submit", submitRerun);
  byId("rerun-replenish-type").addEventListener("change", () => updateReplenishGuidance("rerun"));
  byId("rerun-replenish-guidance").addEventListener("click", (event) => {
    const button = event.target.closest("[data-apply-replenish]");
    if (button) applyReplenishRecommendation("rerun");
  });
  byId("rerun-recommendations").addEventListener("click", (event) => {
    const button = event.target.closest("[data-fill-advice]");
    if (button) fillAdviceScheme(Number(button.dataset.fillAdvice));
  });
  all("input[name='sync_targets']").forEach((input) => input.addEventListener("change", () => {
    state.syncPlan = null;
    state.syncPlanContext = "";
  }));
  byId("configuration-form").addEventListener("input", () => { invalidatePlans(); updateParameterSummaries(); });
  byId("configuration-form").addEventListener("change", () => { invalidatePlans(); updateParameterSummaries(); });
  byId("path-candidate-list").addEventListener("change", (event) => {
    const input = event.target.closest("input[type='radio']");
    if (!input) return;
    if (input.name === "tile_candidate") state.settings.tileRoot = input.value;
    if (input.name === "template_candidate") state.settings.templateRoot = input.value;
    populatePaths(state.settings);
  });
}

if (window.TowerReducerParameters) {
  window.towerParameterUI = window.TowerReducerParameters.mount({document, request: apiRequest,
    batchImport: window.TowerReducerBatchImport, openDialog, closeDialog, setDialogPending,
    showDeliveryCompletion, setInvalid, clearInvalid, focusFirstInvalid, currentJob: () => state.currentJobId});
}
window.towerIncreasePreview = INCREASE.mountPreview({document, request: apiRequest, openDialog, closeDialog,
  renderer: window.TowerTemplatePreview, onApplied: () => {invalidatePlans();updateParameterSummaries();}});
window.towerIncreaseDraft = INCREASE.createPreviewFlow({request: apiRequest, onChange(draft) {
  if (state.mode !== "increase" || draft.input?.file_id !== state.activeOverride || draft.input?.import_id !== state.importId) return;
  const status = byId("increase-draft-status");if (!status) return;
  status.textContent = draft.pending ? "正在试算当前模板…" : draft.error || (draft.stale ? "参数已更新，等待试算。" : INCREASE.describeSummary(draft.schemes[0]?.summary || {}));
  status.classList.toggle("inspection-warning", Boolean(draft.error));
}});
bindEvents();
initializeHelp();
updateImportControls();
routeTo("workspace", false);
initialize();
