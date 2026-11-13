(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const DEFAULT_PATHS = {
    template_path: "",
    config_path: "",
    history_ref: "historical",
    sequence_strategy: "dual_peak",
    level_label_method: "weighted_all_instances",
  };

  const SEQUENCE_STRATEGIES = {
    dual_peak: {
      name: "方案一｜双峰阶梯",
      pattern: ["爽", "简单", "简单", "普通", "普通难", "困难", "普通", "普通", "普通难", "超难"],
      description: "当前复盘口径：每十关在 5 和 9 形成两个峰值，6 处回落后再向超难关爬升。",
      rule: "全局风险升序十等份，依次进入尾号 0、1、2、6、3、7、4、8、5、9。",
    },
    spiral_flow: {
      name: "方案二｜十关螺旋心流",
      pattern: ["爽", "过渡", "轻谜题", "轻谜题", "多谜题", "高压挑战", "过渡", "过渡", "多谜题", "超难高危"],
      description: "以玩家实时行为定义位置：0 爽、2/3 轻谜题、4/8 多谜题、5 高压挑战、9 超难高危。",
      rule: "只有多维高压力与极端失败流失同时成立才暂不排位；其余异常关正常排位并保留风险备注。",
    },
    single_ramp: {
      name: "方案三｜单峰渐进",
      pattern: ["爽", "简单", "简单", "普通", "普通", "普通", "普通难", "困难", "高难", "超难"],
      description: "每十关持续升压，到尾号 9 达峰，下一循环从爽关重新开始。",
      rule: "风险十等份与尾号 0–9 一一对应；规律最清楚，但要重点观察 6–9 的连续疲劳。",
    },
    diversity_guard: {
      name: "方案四｜多样性保护",
      pattern: ["爽", "简单", "简单", "普通", "普通难", "困难", "普通", "普通", "普通难", "超难"],
      description: "保留方案一的双峰节奏，同时降低相邻同模板和连续高流失关卡。",
      rule: "只在同风险桶内确定性换位，不改变尾号难度角色与 5 / 9 峰值。",
    },
  };

  const BEHAVIOR_LABELS = {
    L4: "多维高负担",
    L3: "复合卡点",
    L2: "单点卡顿",
    L1: "稳定低负担",
    L0: "未见稳定过程差异",
    P: "待补样",
  };

  const DEFAULT_TAGS = [
    { code: "P0", name: "轻松爽关", description: "稳定低负担证据充足，适合尾号0" },
    { code: "P1", name: "平淡过渡型", description: "未见明显卡顿，但也缺少爽感证据" },
    { code: "P2", name: "轻度卡顿型", description: "局部卡顿或小谜题，适合尾号2/3" },
    { code: "P3", name: "多谜题思考型", description: "多处思考或复合卡点，适合尾号4/8" },
    { code: "P4", name: "高压挑战型", description: "过程高压但结果尚可控，适合尾号5" },
    { code: "P5", name: "超难高危型", description: "高压力并伴随明显失败风险，只适合尾号9候选" },
    { code: "PX", name: "极端负面待优化", description: "多维高压力与极端流失同时成立，建议优化后再排位" },
  ];

  const state = {
    serviceOk: false,
    csrfToken: "",
    bootstrap: null,
    currentStep: 1,
    settings: { ...DEFAULT_PATHS },
    player: {
      mode: "upload",
      files: [],
      payload: null,
      preflight: null,
      draftId: null,
      jobId: null,
      job: null,
      result: null,
      artifacts: {},
      pollTimer: null,
    },
    online: {
      mode: "upload",
      files: [],
      payload: null,
      preflight: null,
      draftId: null,
      jobId: null,
      job: null,
      result: null,
      artifacts: {},
      pollTimer: null,
    },
  };

  function escapeHTML(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function safeText(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function formatNumber(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return safeText(value);
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(numeric);
  }

  function formatBytes(bytes) {
    const numeric = Number(bytes) || 0;
    if (numeric < 1024) return `${numeric} B`;
    if (numeric < 1024 ** 2) return `${(numeric / 1024).toFixed(1)} KB`;
    return `${(numeric / 1024 ** 2).toFixed(1)} MB`;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function toast(message, kind = "info") {
    const region = $("#toast-region");
    const node = document.createElement("div");
    node.className = `toast${kind === "error" ? " error" : ""}`;
    node.textContent = message;
    region.append(node);
    window.setTimeout(() => node.remove(), 4200);
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 120000);
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...(options.body && state.csrfToken ? { "X-CSRF-Token": state.csrfToken } : {}),
          ...(options.headers || {}),
        },
      });
      const contentType = response.headers.get("content-type") || "";
      const body = contentType.includes("application/json")
        ? await response.json()
        : { message: await response.text() };
      if (!response.ok) {
        const error = new Error(body.message || body.error || `请求失败（${response.status}）`);
        error.details = body;
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("本地服务响应超时，请查看任务日志或稍后重试。");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    const labelNode = $("span", button);
    if (labelNode && label) labelNode.textContent = label;
  }

  function setServiceStatus(kind, text) {
    const pill = $("#service-status");
    pill.classList.toggle("is-checking", kind === "checking");
    pill.classList.toggle("is-offline", kind === "offline");
    pill.classList.toggle("is-online", kind === "online");
    $("#service-status-text").textContent = text;
  }

  function currentSettings() {
    return {
      template_path: $("#template-path").value.trim(),
      config_path: $("#config-path").value.trim(),
      history_ref: $("#history-ref").value.trim(),
      sequence_strategy: $("#sequence-strategy").value,
      level_label_method: $("#level-label-method").value,
    };
  }

  function applySettings(settings) {
    const normalized = Object.fromEntries(Object.entries(settings || {}).map(([key, value]) => [
      key,
      value && typeof value === "object" ? value.value ?? value.path ?? "" : value,
    ]));
    state.settings = { ...DEFAULT_PATHS, ...normalized };
    $("#template-path").value = state.settings.template_path;
    $("#config-path").value = state.settings.config_path;
    $("#history-ref").value = state.settings.history_ref || "";
    $("#sequence-strategy").value = SEQUENCE_STRATEGIES[state.settings.sequence_strategy]
      ? state.settings.sequence_strategy
      : "dual_peak";
    $("#level-label-method").value = ["weighted_all_instances", "dominant_online_duplicate"].includes(state.settings.level_label_method)
      ? state.settings.level_label_method
      : "weighted_all_instances";
    renderSequenceStrategy();
  }

  function renderSequenceStrategy() {
    const code = $("#sequence-strategy").value;
    const strategy = SEQUENCE_STRATEGIES[code] || SEQUENCE_STRATEGIES.dual_peak;
    $("#sequence-strategy-description").textContent = strategy.description;
    $("#sequence-strategy-rule").textContent = strategy.rule;
    $("#sequence-strategy-pattern").innerHTML = strategy.pattern.map((label, index) => {
      const emphasis = label.includes("超难") ? " peak" : label.includes("困难") || label.includes("高难") ? " hard" : label === "爽" || label === "简单" ? " easy" : "";
      return `<span class="strategy-slot${emphasis}"><b>${index}</b><small>${escapeHTML(label)}</small></span>`;
    }).join("");
  }

  function pathState(target, data, fallback) {
    const node = $(target);
    const dot = $(".state-dot", node);
    const text = $("span:last-child", node);
    const exists = typeof data === "object" ? data.status ? data.status === "ready" : data.exists ?? data.ready ?? data.ok : Boolean(data);
    const display = typeof data === "object" ? data.path || data.value || data.message : fallback;
    dot.className = `state-dot ${exists ? "ok" : data ? "warning" : "error"}`;
    text.textContent = display || fallback || "请到设置填写";
    text.title = text.textContent;
  }

  function updatePathStatus(paths = {}) {
    pathState("#template-path-status", paths.template || paths.template_path || paths.template_path_status, state.settings.template_path);
    pathState("#config-path-status", paths.config || paths.config_path || paths.config_path_status, state.settings.config_path);
    const historyNode = $("#history-ref-status");
    const historyData = paths.history || paths.history_ref_status;
    if (historyData) {
      pathState("#history-ref-status", historyData, state.settings.history_ref);
    } else if (state.settings.history_ref === "current") {
      $(".state-dot", historyNode).className = "state-dot warning";
      $("span:last-child", historyNode).textContent = "当前仓库检出版本（请确认与数据日期一致）";
    } else {
      $(".state-dot", historyNode).className = "state-dot ok";
      $("span:last-child", historyNode).textContent = "历史固定快照（4eac5d… / 5022d0…）";
    }
  }

  function route(name) {
    $$(".view").forEach((view) => view.classList.toggle("is-active", view.dataset.view === name));
    $$("[data-route]").forEach((button) => {
      const active = button.dataset.route === name;
      if (button.classList.contains("nav-item")) {
        button.classList.toggle("is-active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
    });
    if (name === "results") updateResultLinks();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function goStep(step, { force = false } = {}) {
    const requested = Number(step);
    const maxStep = state.online.result ? 5 : state.player.result ? 3 : state.player.preflight ? 2 : 1;
    if (!force && requested > maxStep) return;
    state.currentStep = requested;
    $$(".flow-page").forEach((page) => page.classList.toggle("is-active", Number(page.dataset.flow) === requested));
    $$("#stepper li").forEach((item) => {
      const itemStep = Number(item.dataset.step);
      item.classList.toggle("is-current", itemStep === requested);
      item.classList.toggle("is-done", itemStep < requested || (itemStep === 2 && Boolean(state.player.result)) || (itemStep === 4 && Boolean(state.online.result)));
      const button = $("button", item);
      button.disabled = itemStep > maxStep && itemStep !== requested;
    });
    route("workspace");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateResultLinks() {
    const hasBehavior = Boolean(state.player.result);
    const hasClassification = Boolean(state.online.result);
    const runningKind = ["online", "player"].find((kind) => {
      const status = state[kind].job && normalizeStatus(state[kind].job.status);
      return status === "queued" || status === "running";
    });
    $("#results-empty").classList.toggle("is-hidden", hasBehavior || hasClassification);
    $("#result-links").classList.toggle("is-hidden", !hasBehavior && !hasClassification);
    $("#open-behavior-result").classList.toggle("is-hidden", !hasBehavior);
    $("#open-classification-result").classList.toggle("is-hidden", !hasClassification);
    $("#open-config-result").classList.toggle("is-hidden", !hasClassification);
    if (!hasBehavior && !hasClassification) {
      $("#results-empty-title").textContent = runningKind ? "分析正在本机运行" : "还没有完成的分析";
      $("#results-empty-copy").textContent = runningKind
        ? `${runningKind === "player" ? "实时行为分析" : "线上三维分类"}已开始。可以返回工作台查看真实阶段与最近日志；切换页面不会中断任务。`
        : "先在分析工作台导入玩家数据并完成预检。";
      $("#results-empty-action").textContent = runningKind ? "查看运行进度" : "返回工作台";
    }
    $$('[data-download]').forEach((button) => {
      const kind = button.dataset.download;
      const context = kind.startsWith("behavior_") ? state.player : state.online;
      const available = Boolean(context.artifacts?.[kind]);
      button.disabled = !available;
      button.setAttribute("aria-disabled", String(!available));
      button.title = available ? "" : "当前任务尚未生成此文件";
    });
  }

  function setupTabs() {
    $$('[data-source-tab]').forEach((tab) => {
      tab.addEventListener("click", () => {
        const group = tab.dataset.sourceTab.startsWith("player") ? "player" : "online";
        const tabs = $$(`[data-source-tab^="${group}"]`);
        tabs.forEach((item) => {
          const selected = item === tab;
          item.classList.toggle("is-selected", selected);
          item.setAttribute("aria-pressed", String(selected));
        });
        $$(`[data-source-panel^="${group}"]`).forEach((panel) => {
          const active = panel.dataset.sourcePanel === tab.dataset.sourceTab;
          panel.classList.toggle("is-active", active);
          panel.hidden = !active;
        });
        state[group].mode = tab.dataset.sourceTab.endsWith("paste") ? "paste" : "upload";
        updateInputAvailability(group);
      });
    });
  }

  function setupDropZone(group) {
    const multiple = group === "player";
    const input = $(multiple ? "#player-files" : "#online-file");
    const zone = $(multiple ? "#player-drop-zone" : "#online-drop-zone");
    const acceptFiles = (fileList) => {
      const files = [...fileList];
      const invalid = files.filter((file) => !file.name.toLowerCase().endsWith(".csv"));
      if (invalid.length) {
        toast(`只接受 CSV：${invalid.map((file) => file.name).join("、")}`, "error");
      }
      const accepted = files.filter((file) => file.name.toLowerCase().endsWith(".csv"));
      if (!accepted.length) return;
      state[group].files = multiple ? mergeFiles(state[group].files, accepted) : [accepted[0]];
      renderFileList(group);
      updateInputAvailability(group);
    };
    input.addEventListener("change", () => {
      acceptFiles(input.files);
      input.value = "";
    });
    ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    }));
    ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
    }));
    zone.addEventListener("drop", (event) => acceptFiles(event.dataTransfer.files));
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });
  }

  function mergeFiles(existing, incoming) {
    const map = new Map(existing.map((file) => [`${file.name}:${file.size}:${file.lastModified}`, file]));
    incoming.forEach((file) => map.set(`${file.name}:${file.size}:${file.lastModified}`, file));
    return [...map.values()];
  }

  function renderFileList(group) {
    const list = $(group === "player" ? "#player-file-list" : "#online-file-list");
    list.replaceChildren();
    state[group].files.forEach((file, index) => {
      const item = document.createElement("div");
      item.className = "file-token";
      item.innerHTML = `<svg aria-hidden="true"><use href="#i-file"></use></svg><b title="${escapeHTML(file.name)}">${escapeHTML(file.name)}</b><span>${formatBytes(file.size)}</span><button type="button" aria-label="移除 ${escapeHTML(file.name)}"><svg aria-hidden="true"><use href="#i-x"></use></svg></button>`;
      $("button", item).addEventListener("click", () => {
        state[group].files.splice(index, 1);
        renderFileList(group);
        updateInputAvailability(group);
      });
      list.append(item);
    });
  }

  function setupPaste(group) {
    const textarea = $(group === "player" ? "#player-paste" : "#online-paste");
    const counter = $(group === "player" ? "#player-paste-count" : "#online-paste-count");
    textarea.addEventListener("input", () => {
      const content = textarea.value;
      const lineCount = content ? content.split(/\r?\n/).length : 0;
      counter.textContent = `${formatNumber(lineCount)} 行 · ${formatNumber(content.length)} 字符`;
      updateInputAvailability(group);
    });
  }

  function hasInput(group) {
    if (state[group].mode === "upload") return state[group].files.length > 0;
    const textarea = $(group === "player" ? "#player-paste" : "#online-paste");
    return textarea.value.trim().length > 0;
  }

  function updateInputAvailability(group) {
    const button = $(group === "player" ? "#player-preflight-button" : "#online-preflight-button");
    button.disabled = !state.serviceOk || !hasInput(group);
  }

  async function buildInputPayload(group) {
    const settings = currentSettings();
    if (state[group].mode === "paste") {
      const textarea = $(group === "player" ? "#player-paste" : "#online-paste");
      return { source: { kind: "paste", content: textarea.value }, settings };
    }
    const files = await Promise.all(state[group].files.map(async (file) => ({
      name: file.name,
      size: file.size,
      last_modified: file.lastModified,
      content_base64: await fileToBase64(file),
    })));
    return { source: { kind: "files", files }, settings };
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
      reader.onerror = () => reject(new Error(`读取文件失败：${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  function issueArray(result) {
    const audit = result.audit || result;
    return audit.issues || audit.errors || audit.problems || [];
  }

  function normalizeIssue(issue) {
    if (typeof issue === "string") return { title: issue, detail: "", suggestion: "" };
    const title = issue.title || issue.code || issue.message || "数据问题";
    return {
      title,
      detail: issue.detail || (title === issue.message ? "" : issue.message) || "",
      suggestion: issue.suggestion || issue.fix || "",
    };
  }

  function renderFeedback(group, valid, result) {
    const node = $(group === "player" ? "#player-feedback" : "#online-feedback");
    const issues = issueArray(result).map(normalizeIssue);
    const audit = result.audit || result;
    const warnings = Array.isArray(audit.warnings) ? audit.warnings.map(normalizeIssue) : [];
    node.classList.remove("is-hidden", "is-error", "is-success", "is-warning");
    node.classList.add(valid ? (warnings.length ? "is-warning" : "is-success") : "is-error");
    if (valid) {
      const warningHTML = warnings.length
        ? `<ul>${warnings.map((warning) => `<li>${escapeHTML(warning.title)}${warning.detail ? `：${escapeHTML(warning.detail)}` : ""}</li>`).join("")}</ul>`
        : "";
      const title = warnings.length ? "预检通过，部分数据已自动处理" : "预检通过";
      node.innerHTML = `<strong><svg aria-hidden="true"><use href="#i-check-circle"></use></svg> ${title}</strong>${escapeHTML(result.message || "字段、格式与覆盖检查通过。")}${warningHTML}`;
      return;
    }
    const issueHTML = issues.length
      ? `<ul>${issues.map((issue) => `<li><b>${escapeHTML(issue.title)}</b>${issue.detail ? `：${escapeHTML(issue.detail)}` : ""}${issue.suggestion ? `；建议：${escapeHTML(issue.suggestion)}` : ""}</li>`).join("")}</ul>`
      : `<p>${escapeHTML(result.message || "输入未通过预检，请检查文件和字段。")}</p>`;
    node.innerHTML = `<strong><svg aria-hidden="true"><use href="#i-alert"></use></svg> 需要先修复以下问题</strong>${issueHTML}`;
  }

  async function preflightPlayer() {
    const button = $("#player-preflight-button");
    setBusy(button, true, "正在读取并检查");
    try {
      const payload = await buildInputPayload("player");
      state.player.payload = payload;
      const result = await api("/api/preflight/player", { method: "POST", body: JSON.stringify(payload) });
      const audit = result.audit || result;
      const valid = audit.valid === true || audit.ok === true;
      renderFeedback("player", valid, result);
      if (!valid) {
        state.player.preflight = null;
        state.player.draftId = null;
        return;
      }
      const isReanalysis = Boolean(state.player.result || state.player.jobId);
      invalidateAnalysisAfterPlayerInputChange();
      state.player.preflight = result;
      state.player.draftId = result.draft_id || result.draftId || result.token || null;
      renderPlayerPreflight(audit.summary || audit, { reanalysis: isReanalysis });
      goStep(2, { force: true });
      const skippedReplayRows = Number((audit.summary || audit).skipped_empty_replay_rows || 0);
      if (isReanalysis) {
        toast(state.player.mode === "upload"
          ? `已合并并检查当前 ${formatNumber(state.player.files.length || 1)} 个文件；开始后将按全部数据重新分析。`
          : "已检查新的粘贴数据；开始后将替换当前结果并重新分析。");
      } else {
        toast(skippedReplayRows
          ? `玩家数据预检通过，已跳过 ${formatNumber(skippedReplayRows)} 行空回放数据。`
          : "玩家数据预检通过，可以开始行为分析。");
      }
    } catch (error) {
      renderFeedback("player", false, error.details || { message: error.message });
      toast(error.message, "error");
    } finally {
      setBusy(button, false, "检查玩家数据");
      updateInputAvailability("player");
    }
  }

  function invalidateAnalysisAfterPlayerInputChange() {
    window.clearTimeout(state.player.pollTimer);
    window.clearTimeout(state.online.pollTimer);
    localStorage.removeItem("playerBehavior.currentPlayerJob");
    localStorage.removeItem("playerBehavior.currentOnlineJob");
    localStorage.removeItem("playerBehavior.playerResultSnapshot");
    localStorage.removeItem("playerBehavior.onlineResultSnapshot");
    Object.assign(state.player, {
      jobId: null,
      job: null,
      result: null,
      artifacts: {},
      pollTimer: null,
    });
    Object.assign(state.online, {
      preflight: null,
      draftId: null,
      jobId: null,
      job: null,
      result: null,
      artifacts: {},
      pollTimer: null,
    });
    $("#player-launch-card").classList.remove("is-hidden");
    $("#player-job-panel").classList.add("is-hidden");
    $("#behavior-result").classList.add("is-hidden");
    $("#classification-progress").classList.add("is-hidden");
    $("#classification-result-content").classList.remove("is-hidden");
    setBusy($("#start-player-job"), false, "开始行为分析");
    setBehaviorPageState("ready");
    resetPlayerJobPresentation();
    renderConfigExport();
    updateResultLinks();
  }

  function resetPlayerJobPresentation() {
    $("#player-job-id").textContent = "—";
    $("#player-job-title").textContent = "正在准备输入快照";
    $("#player-job-message").textContent = "任务创建后可切换到其他页面，运行状态不会丢失。";
    $("#player-job-progress-label").textContent = "等待真实进度";
    $("#player-job-progress-count").textContent = "任务启动后显示已处理数量";
    $("#player-job-progress").removeAttribute("value");
    $("#player-stage-list").replaceChildren();
    $("#player-log-list").innerHTML = "<li>等待任务开始</li>";
  }

  function renderPlayerPreflight(summary, { reanalysis = false } = {}) {
    setBehaviorPageState("ready");
    $("#player-ready-title").textContent = reanalysis ? "玩家数据已更新，可以重新分析" : "玩家数据可以分析";
    const fileLevels = Array.isArray(summary.files)
      ? summary.files.flatMap((item) => [item.level_min, item.level_max]).filter((item) => item !== null && item !== undefined)
      : [];
    const levelRange = summary.level_range || rangeText(
      summary.level_min ?? (fileLevels.length ? Math.min(...fileLevels) : undefined),
      summary.level_max ?? (fileLevels.length ? Math.max(...fileLevels) : undefined),
    );
    const templateCoverage = summary.template_coverage;
    const coverageText = templateCoverage
      ? `${formatNumber(templateCoverage.found_template_count)}/${formatNumber(templateCoverage.required_template_count)}`
      : summary.instance_count ?? summary.instances;
    const items = [
      ["文件", summary.file_count ?? summary.source_file_count ?? (state.player.files.length || 1)],
      ["有效行", summary.valid_rows ?? summary.merged_rows ?? summary.row_count ?? summary.rows],
      ["唯一玩家", summary.unique_users ?? summary.unique_players ?? summary.user_count ?? summary.uids],
      ["关卡范围", levelRange],
      [templateCoverage ? "模板覆盖" : "关卡实例", coverageText],
    ];
    $("#player-preflight-summary").innerHTML = items.map(([label, value]) => `<div class="summary-item"><span>${escapeHTML(label)}</span><strong title="${escapeHTML(safeText(value))}">${escapeHTML(formatNumber(value))}</strong></div>`).join("");
    const parts = [];
    if (summary.unique_users || summary.unique_players || summary.user_count) parts.push(`${formatNumber(summary.unique_users ?? summary.unique_players ?? summary.user_count)} 名玩家`);
    if (levelRange !== "—") parts.push(`${levelRange} 关`);
    if (summary.duplicate_rows || summary.deduplicated_rows) parts.push(`已识别 ${formatNumber(summary.duplicate_rows ?? summary.deduplicated_rows)} 条重复记录`);
    if (summary.skipped_empty_replay_rows) parts.push(`已跳过 ${formatNumber(summary.skipped_empty_replay_rows)} 行空回放数据`);
    $("#player-ready-summary").textContent = parts.length
      ? `${parts.join(" · ")}。${reanalysis ? "开始后将替换当前结果并按全部文件重算" : "确认后读取配置与模板"}。`
      : reanalysis
        ? "开始后将按原数据与新增数据重新运行完整分析。"
        : "确认后将读取配置和模板，并启动本地后台任务。";
  }

  function rangeText(min, max) {
    if (min === null || min === undefined || max === null || max === undefined) return "—";
    return `${min}–${max}`;
  }

  async function startPlayerJob() {
    const button = $("#start-player-job");
    setBusy(button, true, "正在创建任务");
    try {
      const body = {
        draft_id: state.player.draftId,
        settings: currentSettings(),
        ...(!state.player.draftId ? state.player.payload : {}),
      };
      const result = await api("/api/jobs/player", { method: "POST", body: JSON.stringify(body) });
      state.player.jobId = result.job_id || result.id;
      if (!state.player.jobId) throw new Error("本地服务没有返回任务 ID。");
      localStorage.setItem("playerBehavior.currentPlayerJob", state.player.jobId);
      setBehaviorPageState("running");
      $("#player-launch-card").classList.add("is-hidden");
      $("#player-job-panel").classList.remove("is-hidden");
      $("#player-job-id").textContent = state.player.jobId;
      renderPlayerJob(result);
      pollJob("player");
    } catch (error) {
      toast(error.message, "error");
      setBusy(button, false, "开始行为分析");
    }
  }

  function normalizeStatus(value) {
    const status = String(value || "").toLowerCase();
    if (["succeeded", "success", "completed", "complete", "done"].includes(status)) return "succeeded";
    if (["failed", "error", "interrupted", "cancelled", "canceled"].includes(status)) return "failed";
    if (["queued", "pending"].includes(status)) return "queued";
    return "running";
  }

  function renderPlayerJob(job) {
    state.player.job = job;
    const status = normalizeStatus(job.status);
    const spinner = $("#player-job-spinner");
    const icon = $("#player-job-icon");
    spinner.classList.toggle("is-hidden", status === "succeeded" || status === "failed");
    icon.classList.toggle("is-hidden", status !== "succeeded" && status !== "failed");
    $("use", icon)?.setAttribute("href", status === "failed" ? "#i-alert" : "#i-check-circle");
    icon.closest(".job-state-mark")?.classList.toggle("is-error", status === "failed");
    $("#player-job-title").textContent = job.title || job.stage_name || phaseName(job.progress?.phase || job.stage) || (status === "queued" ? "任务已排队" : status === "failed" ? "行为分析未完成" : status === "succeeded" ? "行为分析已完成" : "正在分析玩家实时行为");
    $("#player-job-message").textContent = job.message || job.stage_detail || job.progress?.message || job.error?.message || (status === "failed" ? "输入和任务日志已保留，可修复后按原输入重试。" : "正在按后台实际处理状态推进。");
    renderPlayerProgress(job, status);
    renderStages(job.stages || job.steps || [], job.stage || job.current_stage);
    renderLogs(job.logs || job.recent_logs || []);
    updateResultLinks();
  }

  function renderPlayerProgress(job, status) {
    const progressData = job.progress || {};
    const hasPercent = progressData.percent !== null && progressData.percent !== undefined && progressData.percent !== "";
    const rawPercent = hasPercent ? Number(progressData.percent) : Number.NaN;
    const percent = Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, rawPercent)) : null;
    const current = Number(progressData.current);
    const total = Number(progressData.total);
    const hasCurrentTotal = progressData.current !== null && progressData.current !== undefined
      && progressData.total !== null && progressData.total !== undefined
      && Number.isFinite(current) && Number.isFinite(total) && total > 0;
    const progress = $("#player-job-progress");
    if (percent === null) progress.removeAttribute("value");
    else progress.value = status === "succeeded" ? 100 : percent;
    const displayPercent = status === "succeeded" ? 100 : percent;
    $("#player-job-progress-label").textContent = status === "failed"
      ? "任务在当前阶段停止"
      : displayPercent === null
        ? "正在等待后台返回进度"
        : `实际进度 ${formatNumber(displayPercent)}%`;
    $("#player-job-progress-count").textContent = hasCurrentTotal
      ? `已处理 ${formatNumber(current)} / ${formatNumber(total)}`
      : status === "succeeded"
        ? "全部分析步骤已完成"
        : "进度仅显示后台已确认的数据";
    progress.setAttribute("aria-valuetext", $("#player-job-progress-label").textContent);
  }

  function phaseName(value) {
    const names = {
      queued: "任务已排队",
      player: "正在分析玩家实时行为",
      player_completed: "行为分析已完成",
      player_failed: "行为分析未完成",
      online: "正在生成三维分类",
      online_completed: "三维分类已完成",
      online_failed: "三维分类未完成",
    };
    return names[String(value || "")] || "";
  }

  function renderStages(stages, currentStage) {
    const list = $("#player-stage-list");
    if (!stages.length) {
      list.replaceChildren();
      list.classList.add("is-hidden");
      return;
    }
    list.classList.remove("is-hidden");
    list.innerHTML = stages.map((stage, index) => {
      const status = normalizeStatus(stage.status);
      const className = status === "succeeded" ? "is-done" : status === "running" ? "is-running" : "";
      const indicator = status === "succeeded" ? `<svg aria-hidden="true"><use href="#i-check"></use></svg>` : index + 1;
      const rawName = stage.name || stage.title || `阶段 ${index + 1}`;
      const displayName = phaseName(rawName) || rawName;
      return `<li class="${className}"><span class="stage-indicator">${indicator}</span><span class="stage-copy"><b>${escapeHTML(displayName)}</b><span>${escapeHTML(stage.detail || stage.message || "")}</span></span><span class="stage-count">${escapeHTML(safeText(stage.count, ""))}</span></li>`;
    }).join("");
  }

  function renderLogs(logs) {
    const list = $("#player-log-list");
    if (!logs.length) {
      list.innerHTML = "<li>等待新的任务日志</li>";
      return;
    }
    list.innerHTML = logs.slice(-12).map((log) => `<li>${escapeHTML(typeof log === "string" ? log : log.message || JSON.stringify(log))}</li>`).join("");
  }

  async function pollJob(kind, { immediate = false } = {}) {
    const jobId = state[kind].jobId;
    if (!jobId) return;
    window.clearTimeout(state[kind].pollTimer);
    const run = async () => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const status = normalizeStatus(job.status);
        if (kind === "player") renderPlayerJob(job);
        else renderClassificationProgress(job);
        if (status === "succeeded") {
          state[kind].result = resultFromJob(job, kind);
          state[kind].artifacts = normalizeArtifacts(job.artifacts || state[kind].result.artifacts || {}, kind);
          if (kind === "player") finishPlayerJob(state[kind].result);
          else finishOnlineJob(state[kind].result);
          return;
        }
        if (status === "failed") {
          toast(job.message || `${kind === "player" ? "行为分析" : "三维分类"}失败，请查看日志。`, "error");
          handleJobFailure(kind, job);
          return;
        }
        state[kind].pollTimer = window.setTimeout(run, 1300);
      } catch (error) {
        toast(`读取任务状态失败：${error.message}`, "error");
        state[kind].pollTimer = window.setTimeout(run, 3500);
      }
    };
    if (immediate) await run();
    else state[kind].pollTimer = window.setTimeout(run, 450);
  }

  function handleJobFailure(kind, job) {
    if (kind === "player") {
      $("#player-launch-card").classList.remove("is-hidden");
      $("#player-ready-title").textContent = "任务未完成，可以按原输入重试";
      $("#player-ready-summary").textContent = job.error?.message || job.message || "输入和预检草稿仍保留；请先查看最近日志。";
      const button = $("#start-player-job");
      const retryable = Boolean(state.player.draftId || state.player.payload);
      setBusy(button, false, retryable ? "按原输入重试" : "返回步骤 1 重新预检");
      button.disabled = !retryable;
      if (!retryable) $("#player-ready-summary").textContent += " 本机未找到可重用的预检草稿，请返回步骤 1 重新导入并预检。";
      return;
    }
    const panel = $("#classification-progress");
    panel.classList.add("has-error");
    let retry = $("[data-retry-online]", panel);
    if (!retry) {
      retry = document.createElement("button");
      retry.type = "button";
      retry.className = "secondary-button";
      retry.dataset.retryOnline = "true";
      retry.textContent = "按原输入重试";
      retry.addEventListener("click", startOnlineJob);
      panel.append(retry);
    }
    retry.disabled = !(state.online.draftId || state.online.payload);
    if (retry.disabled) retry.title = "本机未找到可重用的线上预检草稿，请返回步骤 3 重新预检。";
  }

  function resultFromJob(job, kind) {
    const summary = job.summary?.[kind] || job.summary || {};
    const engine = job.engine_results?.[kind] || {};
    const preview = previewFromJob(job, kind);
    return { ...engine, ...summary, summary: { ...engine, ...summary }, preview, previews: job.previews || {}, artifacts: job.artifacts || [], source: job.source || {} };
  }

  function persistResultSnapshot(kind) {
    const context = state[kind];
    if (!context?.result) return;
    try {
      localStorage.setItem(`playerBehavior.${kind}ResultSnapshot`, JSON.stringify({
        jobId: context.jobId,
        result: context.result,
        artifacts: context.artifacts,
      }));
    } catch {
      // Browser storage is a convenience only; the local job directory remains the source of truth.
    }
  }

  function restoreResultSnapshot(kind) {
    if (state[kind].result) return true;
    try {
      const snapshot = JSON.parse(localStorage.getItem(`playerBehavior.${kind}ResultSnapshot`) || "null");
      if (!snapshot?.result) return false;
      state[kind].jobId ||= snapshot.jobId || null;
      state[kind].result = snapshot.result;
      state[kind].artifacts = normalizeArtifacts(snapshot.artifacts || snapshot.result.artifacts || {}, kind);
      if (kind === "player") {
        $("#player-launch-card").classList.add("is-hidden");
        $("#behavior-result").classList.remove("is-hidden");
        renderBehaviorResult(snapshot.result);
      } else {
        renderClassificationResult(snapshot.result);
      }
      return true;
    } catch {
      return false;
    }
  }

  function previewFromJob(job, kind) {
    const artifacts = Array.isArray(job.artifacts) ? job.artifacts : [];
    const preferred = artifacts.find((item) => {
      const label = `${item.label || ""} ${item.filename || ""}`;
      return kind === "player" ? /关卡分层明细|关卡号分层|levels/i.test(label) : /三维分类与推荐序列|recommendation/i.test(label);
    });
    const values = job.previews && typeof job.previews === "object" ? job.previews : {};
    return (preferred && values[preferred.id]) || Object.values(values).find((item) => Array.isArray(item?.rows)) || {};
  }

  function normalizeArtifacts(artifacts, stageKind = "player") {
    if (!Array.isArray(artifacts)) return artifacts || {};
    const mapped = {};
    artifacts.forEach((item) => {
      const key = String(item.kind || item.type || item.name || item.id || "").trim();
      if (key) mapped[key] = item;
      const text = `${item.label || ""} ${item.filename || ""}`;
      // Compatibility mapping for artifacts written by an older local service. Newer services
      // provide stable `kind` values, so every stage-one audit stays independently downloadable.
      if (!key) return;
      if (/\.xlsx$/i.test(item.filename || "")) {
        if (/三维|推荐序列|table2/i.test(text)) mapped.classification_workbook ||= item;
        else if (/玩家|行为|分层|table1/i.test(text) || stageKind === "player") mapped.behavior_workbook ||= item;
      }
      if (/config_level_list.*(copy|副本)|配置副本/i.test(text)) mapped.config_level_list_copy ||= item;
      if (/config.*(audit|审计)|映射审计/i.test(text)) mapped.config_level_list_audit ||= item;
      if (/关卡副本标签|duplicate/i.test(text)) mapped.classification_duplicates ||= item;
      if (/分类审计|audit/i.test(text) && stageKind === "online") mapped.classification_audit ||= item;
      if (stageKind === "player") {
        if (/关卡分层|关卡号分层|levels/i.test(text)) mapped.behavior_levels ||= item;
        if (/实例分层|instances/i.test(text)) mapped.behavior_instances ||= item;
        if (/行为维度|metrics/i.test(text)) mapped.behavior_metrics ||= item;
        if (/分析摘要|summary/i.test(text)) mapped.behavior_summary ||= item;
        if (/审计/i.test(text)) mapped.behavior_audit ||= item;
      }
    });
    return mapped;
  }

  const BEHAVIOR_ARTIFACT_LABELS = {
    behavior_levels: "关卡分层明细",
    behavior_instances: "实例行为明细",
    behavior_metrics: "过程指标明细",
    behavior_summary: "质量与稳定性摘要",
    behavior_audit: "综合审计数据",
  };

  function renderBehaviorArtifacts() {
    const container = $("#behavior-artifact-list");
    const items = Object.entries(state.player.artifacts || {})
      .filter(([key, item]) => key.startsWith("behavior_") && key !== "behavior_workbook" && item)
      .filter(([key], index, list) => list.findIndex(([candidate]) => candidate === key) === index);
    if (!items.length) {
      container.innerHTML = `<span class="support-copy">本次任务未返回单独审计文件；可从表 1 工作簿查看完整明细。</span>`;
      return;
    }
    container.innerHTML = items.map(([key, item]) => {
      const label = BEHAVIOR_ARTIFACT_LABELS[key] || item.label || item.filename || "审计数据";
      return `<button class="secondary-button" type="button" data-artifact-download="${escapeHTML(key)}"><svg aria-hidden="true"><use href="#i-download" /></svg>${escapeHTML(label)}</button>`;
    }).join("");
    $$('[data-artifact-download]', container).forEach((button) => button.addEventListener("click", () => downloadArtifact(button.dataset.artifactDownload)));
  }

  function finishPlayerJob(result) {
    $("#player-job-panel").classList.add("is-hidden");
    $("#behavior-result").classList.remove("is-hidden");
    renderBehaviorResult(result);
    persistResultSnapshot("player");
    goStep(2, { force: true });
    updateResultLinks();
    toast("实时行为分层已完成，可以下载表 1。");
  }

  function openPlayerSampleSupplement() {
    const retainedFileCount = state.player.files.length;
    goStep(1, { force: true });
    window.requestAnimationFrame(() => {
      $("#player-drop-zone")?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    toast(retainedFileCount > 0
      ? `已保留当前 ${formatNumber(retainedFileCount)} 个文件；继续追加后点击“检查玩家数据”，将按全部文件重新分析。`
      : "当前结果来自已恢复的历史任务；请重新选择原批次与新增批次文件，再按全部数据分析。");
  }

  function objectEntriesAsKpis(kpis, fallback = []) {
    if (Array.isArray(kpis)) return kpis;
    if (kpis && typeof kpis === "object") {
      return Object.entries(kpis).map(([label, value]) => typeof value === "object" ? { label, ...value } : { label, value });
    }
    return fallback;
  }

  function renderKpis(target, kpis) {
    $(target).innerHTML = kpis.map((item) => `<article class="kpi-card"><span>${escapeHTML(item.label || item.name || "指标")}</span><strong title="${escapeHTML(safeText(item.value))}">${escapeHTML(formatNumber(item.value))}${escapeHTML(item.unit || "")}</strong><small>${escapeHTML(item.note || item.description || "")}</small></article>`).join("");
  }

  function extractPreview(result) {
    const preview = result.preview || result.table || {};
    let rows = preview.rows || result.rows || result.levels || [];
    const columns = preview.columns || result.columns || (rows.length ? Object.keys(rows[0]) : []);
    if (rows.length && Array.isArray(rows[0])) {
      rows = rows.map((values) => Object.fromEntries(columns.map((column, index) => [column, values[index] ?? ""])));
    }
    return { columns, rows };
  }

  function behaviorLayer(row) {
    return row.first_level_behavior_label || row.layer || row.label || row.first_level_label || row.pressure_label || row.behavior_label || row["首局行为标签"] || row["分类后标签"] || row["行为分层"] || "";
  }

  function setBehaviorPageState(mode) {
    const completed = mode === "completed";
    $("#behavior-title").textContent = completed ? "实时行为分层已完成" : "还原实时过程并建立行为分层";
    $("#behavior-page-copy").textContent = completed
      ? "先看覆盖与稳定性，再查看哪些关卡或实例产生了稳定过程差异。"
      : "主分层只使用玩家实时行为；失败、消耗和模板静态特征不会直接定层。";
    $("#behavior-header-download").classList.toggle("is-hidden", !completed);
  }

  function fallbackAdditionalPlayers(currentPlayers, pendingLevels, totalLevels) {
    const pendingShare = totalLevels > 0 ? pendingLevels / totalLevels : 1;
    const expansion = pendingShare >= 0.5 ? 0.5 : pendingShare >= 0.2 ? 0.3 : 0.2;
    return Math.max(20, Math.ceil((Math.max(1, Number(currentPlayers) || 0) * expansion) / 10) * 10);
  }

  function renderBehaviorResult(result) {
    setBehaviorPageState("completed");
    const preview = extractPreview(result);
    const inputAudit = state.player.preflight?.audit || state.player.preflight || {};
    const sampleSummary = result.sample || result.summary?.sample || {};
    const levelCount = result.level_count ?? result.level_indices ?? result.summary?.level_count ?? result.summary?.level_indices ?? sampleSummary.level_indices ?? sampleSummary.target_level_count ?? inputAudit.unique_levels ?? preview.rows.length;
    const userCount = result.user_count ?? result.players ?? result.summary?.user_count ?? result.summary?.players ?? sampleSummary.players ?? inputAudit.unique_players;
    const distribution = result.first_level_label_counts || result.summary?.first_level_label_counts || result.distribution || countLabels(preview.rows, behaviorLayer);
    const stableFromDistribution = Object.entries(distribution).reduce((sum, [label, count]) => (
      /局部卡顿|复合卡点/.test(String(label)) ? sum + Number(count || 0) : sum
    ), 0);
    const stableCount = result.stable_level_count ?? result.summary?.stable_level_count ?? stableFromDistribution;
    const coverageRaw = result.replay_coverage ?? result.summary?.replay_coverage ?? result.coverage?.geometry_click_share ?? result.summary?.coverage?.geometry_click_share;
    const coverage = Number.isFinite(Number(coverageRaw)) && Number(coverageRaw) <= 1 ? Number(coverageRaw) * 100 : coverageRaw;
    const fallbackKpis = [
      { label: "分析关卡", value: levelCount, note: "进度位结果" },
      { label: "唯一玩家", value: userCount, note: "通过 UID 去重" },
      { label: "稳定差异关", value: stableCount, note: "跨玩家复刻" },
      { label: "几何点击覆盖", value: coverage, unit: coverage === null || coverage === undefined ? "" : "%", note: "详见质量审计" },
    ];
    renderKpis("#behavior-kpis", objectEntriesAsKpis(result.kpis, fallbackKpis));

    const total = Math.max(1, Object.values(distribution).reduce((sum, value) => sum + Number(value || 0), 0));
    const entries = Object.entries(distribution).filter(([, value]) => Number(value || 0) > 0);
    const rows = entries.length ? entries : [["暂无标签", 0]];
    $("#layer-distribution").innerHTML = rows.map(([label, count]) => {
      const labelText = BEHAVIOR_LABELS[label] ? `${label} · ${BEHAVIOR_LABELS[label]}` : label;
      const safeCount = Math.max(0, Number(count || 0));
      return `<div class="layer-row"><span class="layer-label"><b>${escapeHTML(labelText)}</b></span><progress class="layer-bar" max="${total}" value="${safeCount}" aria-label="${escapeHTML(`${labelText}：${formatNumber(count)} 关`)}"></progress><span class="layer-count">${formatNumber(count)}</span></div>`;
    }).join("");

    const pendingSampleCount = Object.entries(distribution).reduce((sum, [label, count]) => (
      /样本覆盖不足|待补样/.test(String(label)) ? sum + Number(count || 0) : sum
    ), 0);
    const recommendation = result.sample_recommendation || result.summary?.sample_recommendation || {};
    const recommendedAdditional = Number(recommendation.recommended_additional_players)
      || fallbackAdditionalPlayers(userCount, pendingSampleCount, levelCount);
    const nextButton = $("#behavior-next-button");
    const addSamplesButton = $("#behavior-add-samples");
    addSamplesButton.classList.toggle("is-hidden", pendingSampleCount > 0);
    nextButton.disabled = false;
    nextButton.dataset.goStep = pendingSampleCount > 0 ? "1" : "3";
    nextButton.innerHTML = pendingSampleCount > 0
      ? `返回玩家数据补样<svg aria-hidden="true"><use href="#i-arrow"></use></svg>`
      : `导入线上指标<svg aria-hidden="true"><use href="#i-arrow"></use></svg>`;
    $("#behavior-next-title").textContent = pendingSampleCount > 0
      ? "下一步：先补齐玩家样本"
      : "下一步：补齐线上指标数据";
    $("#behavior-next-copy").textContent = pendingSampleCount > 0
      ? `${formatNumber(pendingSampleCount)} 关仍为“样本覆盖不足”。按当前实例分配比例估算，建议至少再补 ${formatNumber(recommendedAdditional)} 名同口径、互不重复的玩家，重跑后再进入线上指标分析。`
      : "100 关均已得到可分类的行为标签，可以继续导入同关卡区间的线上副本数据，叠加人均消耗和失败流失。";

    const metricsArtifact = state.player.artifacts?.behavior_metrics;
    const metricsPreview = metricsArtifact?.id ? result.previews?.[metricsArtifact.id] : null;
    const metricRows = metricsPreview?.rows?.length
      ? (Array.isArray(metricsPreview.rows[0])
        ? metricsPreview.rows.map((values) => Object.fromEntries(metricsPreview.columns.map((column, index) => [column, values[index] ?? ""])))
        : metricsPreview.rows)
      : [];
    const derivedInsights = metricRows
      .filter((row) => row.role === "主分层" || !row.role)
      .sort((left, right) => Number(right.formal_signal_count || 0) - Number(left.formal_signal_count || 0))
      .slice(0, 6)
      .map((row) => ({
        name: `${row.axis || "行为维度"} · ${row.metric_label || row.metric || "指标"}`,
        description: `${row.stability_summary || row.interpretation || "详见过程指标明细"}${row.formal_signal_count !== undefined ? `；正式信号 ${formatNumber(row.formal_signal_count)} 个` : ""}`,
      }));
    const insights = result.insights || result.behavior_dimensions || derivedInsights;
    $("#behavior-insights").innerHTML = insights.length
      ? insights.slice(0, 6).map((item, index) => `<li><span class="insight-rank">${index + 1}</span><span class="insight-copy"><b>${escapeHTML(item.name || item.label || item.dimension || safeText(item))}</b><span>${escapeHTML(item.description || item.detail || item.evidence || "")}</span></span></li>`).join("")
      : `<li><span class="insight-rank">—</span><span class="insight-copy"><b>结果未提供维度摘要</b><span>请下载审计明细查看逐维度统计。</span></span></li>`;
    renderTable("behavior", preview.columns, preview.rows);
    renderBehaviorArtifacts();
  }

  function countLabels(rows, getter) {
    return rows.reduce((counts, row) => {
      const raw = String(getter(row) || "");
      const key = Object.keys(BEHAVIOR_LABELS).find((code) => raw.startsWith(code)) || raw;
      if (key) counts[key] = (counts[key] || 0) + 1;
      return counts;
    }, {});
  }

  async function preflightOnline() {
    const button = $("#online-preflight-button");
    setBusy(button, true, "正在检查并创建任务");
    try {
      const payload = await buildInputPayload("online");
      payload.job_id = state.player.jobId;
      state.online.payload = payload;
      const result = await api("/api/preflight/online", { method: "POST", body: JSON.stringify(payload) });
      const audit = result.audit || result;
      const valid = audit.valid === true || audit.ok === true;
      state.online.preflight = valid ? result : null;
      state.online.draftId = valid ? result.draft_id || result.draftId || result.token || null : null;
      renderFeedback("online", valid, result);
      if (!valid) return;
      await startOnlineJob();
    } catch (error) {
      renderFeedback("online", false, error.details || { message: error.message });
      toast(error.message, "error");
    } finally {
      setBusy(button, false, "检查并按所选方案生成");
      updateInputAvailability("online");
    }
  }

  async function startOnlineJob() {
    const body = {
      draft_id: state.online.draftId,
      settings: currentSettings(),
      ...(!state.online.draftId ? state.online.payload : {}),
    };
    const result = await api(`/api/jobs/${encodeURIComponent(state.player.jobId)}/online`, { method: "POST", body: JSON.stringify(body) });
    state.online.jobId = result.job_id || result.id || state.player.jobId;
    localStorage.setItem("playerBehavior.currentOnlineJob", state.online.jobId);
    $("#classification-result-content").classList.add("is-hidden");
    $("#classification-progress").classList.remove("is-hidden");
    $("#classification-job-id").textContent = state.online.jobId;
    renderClassificationProgress(result);
    goStep(4, { force: true });
    pollJob("online");
  }

  function renderClassificationProgress(job) {
    state.online.job = job;
    const status = normalizeStatus(job.status);
    $("#classification-spinner").classList.toggle("is-hidden", status === "succeeded" || status === "failed");
    const icon = $("#classification-status-icon");
    icon.classList.toggle("is-hidden", status !== "succeeded" && status !== "failed");
    $("use", icon)?.setAttribute("href", status === "failed" ? "#i-alert" : "#i-check-circle");
    icon.closest(".job-state-mark")?.classList.toggle("is-error", status === "failed");
    $("#classification-progress-title").textContent = job.title || job.stage_name || phaseName(job.progress?.phase || job.stage) || (status === "queued" ? "任务已排队" : status === "failed" ? "三维分类未完成" : status === "succeeded" ? "三维分类已完成" : "正在生成 T0–T7 与候选序列");
    $("#classification-progress-copy").textContent = job.message || job.stage_detail || job.progress?.message || job.error?.message || "先聚合线上指标，再计算稳健基线与排关约束。";
    updateResultLinks();
  }

  function finishOnlineJob(result) {
    $("#classification-progress").classList.add("is-hidden");
    $("#classification-result-content").classList.remove("is-hidden");
    $("[data-retry-online]", $("#classification-progress"))?.remove();
    renderClassificationResult(result);
    renderConfigExport();
    persistResultSnapshot("online");
    goStep(4, { force: true });
    updateResultLinks();
    toast("关卡压力分类与候选序列已完成。");
  }

  function renderClassificationResult(result) {
    const preview = extractPreview(result);
    const strategyCode = result.sequence_strategy || state.settings.sequence_strategy || "dual_peak";
    const strategy = SEQUENCE_STRATEGIES[strategyCode] || SEQUENCE_STRATEGIES.dual_peak;
    const strategyName = result.sequence_strategy_name || strategy.name;
    const labelMethodName = result.level_label_method_name || (state.settings.level_label_method === "dominant_online_duplicate" ? "线上进入人数最多副本代表" : "全部首局实例玩家加权");
    $("#classification-result-copy").textContent = `${strategyName}；关卡标签口径：${labelMethodName}。报表同时保留两种标签供对照。`;
    const plannerDefinitions = result.planner_tag_definitions || result.tag_definitions;
    const definitionCounts = Array.isArray(plannerDefinitions) ? Object.fromEntries(plannerDefinitions.map((item) => [item.code, item.count])) : {};
    const tagCounts = result.distribution || (Object.keys(definitionCounts).length ? definitionCounts : countLabels(preview.rows, (row) => row.planner_tag || row["排关压力标签"]));
    const replaceCount = result.optimize_count ?? result.summary?.optimize_count ?? preview.rows.filter((row) => String(row.action || row.recommendation || row["优化建议"] || "").match(/优化|替换|复盘/)).length;
    const movedCount = result.moved_count ?? result.summary?.moved_count ?? preview.rows.filter((row) => {
      const original = row.level_index ?? row["原关卡号"];
      const recommended = row.recommended_level_index ?? row["新推荐序列后关卡号"];
      return original !== undefined && recommended !== undefined && String(original) !== String(recommended);
    }).length;
    const fallbackKpis = [
      { label: "分类关卡", value: result.level_count ?? preview.rows.length, note: "进度位结果" },
      { label: "重点优化候选", value: replaceCount, note: "结合压力、消耗与流失" },
      { label: "建议调整位置", value: movedCount, note: "候选序列变化" },
      { label: "标签数量", value: Object.keys(tagCounts).length || 7, note: "排关压力标签" },
    ];
    renderKpis("#classification-kpis", objectEntriesAsKpis(result.kpis, fallbackKpis));
    renderTagLegend(result.legend || plannerDefinitions || DEFAULT_TAGS, tagCounts);
    renderTable("classification", preview.columns, preview.rows);
    renderConfigExport();
  }

  function renderConfigExport() {
    const copy = state.online.artifacts?.config_level_list_copy;
    const audit = state.online.artifacts?.config_level_list_audit;
    const status = $("#config-export-status");
    const copyText = $("#config-export-copy");
    if (copy) {
      status.textContent = "配置副本已准备好";
      copyText.textContent = `已生成独立副本${audit ? "及可读的重排对照" : ""}。合理关已排位，待优化/补样关的原位留空；源文件未被写入。`;
      return;
    }
    if (state.online.result) {
      const optimizeCount = Number(state.online.result.optimize_count || state.online.result.summary?.optimize_count || 0);
      status.textContent = "等待本地服务返回配置副本";
      copyText.textContent = optimizeCount ? `应生成含 ${optimizeCount} 个待优化空位的副本；请查看任务日志或重新运行。` : "分类已完成，但本地服务未返回配置副本；源 config_level_list.csv 不会被修改。";
      return;
    }
    status.textContent = "等待三维分类完成";
    copyText.textContent = "完成关卡分类后会生成副本；待优化关卡的原位留空。";
  }

  function renderTagLegend(legend, counts = {}) {
    const normalized = Array.isArray(legend)
      ? legend
      : Object.entries(legend).map(([code, value]) => typeof value === "object" ? { code, ...value } : { code, name: value });
    $("#tag-legend").innerHTML = normalized.map((item) => {
      const code = String(item.code || item.id || "").toUpperCase();
      const count = counts[code];
      return `<div class="legend-item tag-${escapeHTML(code.toLowerCase())}"><span class="legend-code">${escapeHTML(code)}</span><span><b>${escapeHTML(item.name || item.label || "未命名标签")}${count !== undefined ? ` · ${formatNumber(count)} 关` : ""}</b><span>${escapeHTML(item.description || item.definition || item.note || "")}</span></span></div>`;
    }).join("");
  }

  function renderTable(kind, columns, rows) {
    const head = $(`#${kind}-table-head`);
    const body = $(`#${kind}-table-body`);
    const footer = $(`#${kind}-table-footer`);
    const safeColumns = columns.slice(0, kind === "classification" ? 10 : 12);
    head.innerHTML = safeColumns.length ? `<tr>${safeColumns.map((column) => `<th scope="col" title="${escapeHTML(column)}">${escapeHTML(columnLabel(column))}</th>`).join("")}</tr>` : "";
    body.dataset.rows = "";
    body._allRows = rows;
    body._columns = safeColumns;
    renderFilteredTable(kind, "");
    footer.textContent = rows.length ? `预览 ${formatNumber(rows.length)} 行；完整字段与审计明细请下载工作簿。` : "结果中没有可预览的行。";
  }

  function renderFilteredTable(kind, query) {
    const body = $(`#${kind}-table-body`);
    const rows = body._allRows || [];
    const columns = body._columns || [];
    const needle = String(query || "").trim().toLowerCase();
    const filtered = needle ? rows.filter((row) => columns.some((column) => String(row[column] ?? "").toLowerCase().includes(needle))) : rows;
    const visible = filtered.slice(0, 200);
    if (!visible.length) {
      body.innerHTML = `<tr><td class="table-empty" colspan="${Math.max(columns.length, 1)}">${rows.length ? "没有匹配结果" : "当前结果没有预览数据"}</td></tr>`;
    } else {
      body.innerHTML = visible.map((row) => `<tr>${columns.map((column) => renderCell(row[column], column, kind)).join("")}</tr>`).join("");
    }
    const footer = $(`#${kind}-table-footer`);
    if (needle) footer.textContent = `匹配 ${formatNumber(filtered.length)} 行，当前最多展示 200 行。`;
  }

  function renderCell(value, column, kind) {
    let text = safeText(value);
    const numeric = Number(value);
    const isPercentage = /(?:_share|_rate|_ratio|占比|率)$/i.test(String(column));
    const isIdentity = /(?:^|_)(?:id|index|version)$/i.test(String(column)) || /关卡号|模板|版本/.test(String(column));
    if (text !== "" && Number.isFinite(numeric) && isPercentage) {
      const percent = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
      text = `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(percent)}%`;
    } else if (text !== "" && Number.isFinite(numeric) && !isIdentity) {
      text = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: Number.isInteger(numeric) ? 0 : 3 }).format(numeric);
    }
    const behaviorCode = Object.keys(BEHAVIOR_LABELS).find((code) => text.startsWith(code));
    const tagCode = text.match(/^(?:T[0-7]|P[0-5X])/i)?.[0]?.toUpperCase();
    if (behaviorCode && /标签|分层|layer|label/i.test(column)) {
      return `<td><span class="tag" data-layer="${behaviorCode}">${escapeHTML(text)}</span></td>`;
    }
    if (kind === "classification" && tagCode && /标签|分类|tag|label/i.test(column)) {
      return `<td><span class="tag result-tag tag-${tagCode.toLowerCase()}">${escapeHTML(text)}</span></td>`;
    }
    return `<td><span class="cell-ellipsis" title="${escapeHTML(text)}">${escapeHTML(text)}</span></td>`;
  }

  function columnLabel(column) {
    const labels = {
      level_index: "关卡号",
      level_id: "关卡 ID",
      level_template_id: "模板 ID",
      first_level_behavior_label: "首局综合标签",
      first_review_priority: "建议处理",
      first_instance_count: "首局实例数",
      first_instance_player_weight: "首局玩家权重",
      first_classified_player_weight_share: "已分层玩家占比",
      first_compound_high_burden_share: "复合高负担占比",
      first_any_friction_share: "任一卡顿占比",
      first_L4_share: "L4 占比",
      first_L3_share: "L3 占比",
      first_L2_share: "L2 占比",
      first_L1_share: "L1 占比",
      original_level_index: "原关卡号",
      recommended_level_index: "新推荐关卡号",
      classification_label: "三维分类标签",
      pressure_label: "压力标签",
      planner_tag: "排关压力标签",
      slot_guidance: "适合位置",
      named_tag: "内部三维诊断",
      risk_note: "风险备注",
      sequence_strategy: "排列方案",
      level_label_method_name: "关卡标签口径",
      weighted_planner_tag: "全实例加权标签",
      dominant_duplicate_planner_tag: "主副本标签",
      dominant_duplicate_id: "来源副本号",
    };
    return labels[column] || String(column).replaceAll("_", " ");
  }

  function downloadArtifact(kind) {
    const isBehavior = kind.startsWith("behavior");
    const context = isBehavior ? state.player : state.online;
    const artifact = context.artifacts[kind] || context.artifacts[kind.replace("behavior_", "").replace("classification_", "")];
    if (!context.jobId) {
      toast("当前没有可下载的任务结果。", "error");
      return;
    }
    if (!artifact) {
      toast("任务没有返回该导出文件，请查看任务审计或重新运行。", "error");
      return;
    }
    if (typeof artifact === "object" && artifact.download_url) {
      window.location.assign(artifact.download_url);
      return;
    }
    const artifactId = typeof artifact === "object" ? artifact.id || artifact.name || artifact.path : artifact;
    window.location.assign(`/api/download/${encodeURIComponent(context.jobId)}/${encodeURIComponent(artifactId)}`);
  }

  function renderHistory(jobs) {
    const container = $("#history-list");
    if (!jobs?.length) {
      container.innerHTML = `<div class="empty-state compact"><svg aria-hidden="true"><use href="#i-history"></use></svg><h2>暂无运行记录</h2><p>完成一次分析后会在这里显示任务状态和导出入口。</p></div>`;
      return;
    }
    container.innerHTML = `<div class="history-row header"><span>任务</span><span>创建时间</span><span>类型</span><span>状态</span><span>操作</span></div>${jobs.map((job) => {
      const status = normalizeStatus(job.status);
      const inferredType = String(job.stage || "").startsWith("online") ? "online" : (job.type || job.kind || "player");
      const typeLabel = inferredType === "online" ? "三维分类" : "行为分析";
      return `<div class="history-row"><b title="${escapeHTML(job.id || job.job_id)}">${escapeHTML(job.name || job.id || job.job_id)}</b><span>${escapeHTML(formatDate(job.created_at || job.updated_at))}</span><span>${escapeHTML(typeLabel)}</span><span class="history-state ${status}">${escapeHTML(statusText(status))}</span><button class="text-button" type="button" data-resume-job="${escapeHTML(job.id || job.job_id)}" data-job-type="${escapeHTML(inferredType)}">查看</button></div>`;
    }).join("")}`;
    $$('[data-resume-job]', container).forEach((button) => button.addEventListener("click", () => resumeJob(button.dataset.resumeJob, button.dataset.jobType)));
  }

  function statusText(status) {
    return { succeeded: "已完成", failed: "未完成", queued: "排队中", running: "运行中" }[status] || status;
  }

  async function resumeJob(jobId, jobType = "player") {
    const kind = String(jobType).toLowerCase().includes("online") || String(jobType).toLowerCase().includes("class") ? "online" : "player";
    state[kind].jobId = jobId;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const status = normalizeStatus(job.status);
      state[kind].job = job;
      state[kind].artifacts = normalizeArtifacts(job.artifacts || [], kind);
      if (status !== "succeeded") state[kind].result = null;
      state[kind].draftId ||= job.draft_id || job.draftId || job.source?.[kind]?.draft_id || job.source?.draft_id || job.source?.draftId || null;
      state[kind].payload ||= job.input || job.payload || job.source?.payload || null;
      if (kind === "player" && job.source?.player?.audit) {
        state.player.preflight = {
          draft_id: job.source.player.draft_id || state.player.draftId,
          audit: job.source.player.audit,
        };
      }
      const hasCompletedPlayerStage = Boolean(
        job.summary?.player
        && (String(job.stage || "").startsWith("online") || (job.artifacts || []).some((item) => String(item.kind || "").startsWith("behavior_")))
      );
      if (hasCompletedPlayerStage) {
        state.player.jobId = jobId;
        state.player.result = resultFromJob(job, "player");
        state.player.artifacts = normalizeArtifacts(job.artifacts || [], "player");
        renderBehaviorResult(state.player.result);
        $("#player-launch-card").classList.add("is-hidden");
        $("#player-job-panel").classList.add("is-hidden");
        $("#behavior-result").classList.remove("is-hidden");
        persistResultSnapshot("player");
      } else if (kind === "online" && !state.player.result) {
        restoreResultSnapshot("player");
        const upstreamJobId = job.player_job_id || job.parent_job_id || job.source?.player_job_id || job.source?.parent_job_id;
        if (!state.player.result && upstreamJobId && upstreamJobId !== jobId) await resumeJob(upstreamJobId, "player");
      }
      // A two-stage job can be globally failed/running because of phase two
      // while its phase-one result is already complete and downloadable.
      if (kind === "player" && hasCompletedPlayerStage) {
        updateResultLinks();
        goStep(2, { force: true });
        return;
      }
      if (kind === "player") {
        setBehaviorPageState("running");
        $("#player-launch-card").classList.add("is-hidden");
        $("#player-job-panel").classList.remove("is-hidden");
        $("#behavior-result").classList.add("is-hidden");
        $("#player-job-id").textContent = jobId;
        renderPlayerJob(job);
        goStep(2, { force: true });
      } else {
        $("#classification-result-content").classList.add("is-hidden");
        $("#classification-progress").classList.remove("is-hidden");
        renderClassificationProgress(job);
        goStep(4, { force: true });
      }
      if (status === "succeeded") {
        state[kind].result = resultFromJob(job, kind);
        state[kind].artifacts = normalizeArtifacts(job.artifacts || state[kind].result.artifacts || {}, kind);
        if (kind === "player") finishPlayerJob(state[kind].result);
        else finishOnlineJob(state[kind].result);
      } else if (status === "failed") {
        handleJobFailure(kind, job);
      } else {
        pollJob(kind);
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function refreshLocalPaths() {
    try {
      const paths = await api("/api/settings/paths", {
        method: "POST", body: JSON.stringify(state.settings)
      });
      updatePathStatus(paths);
      return paths;
    } catch {
      updatePathStatus({});
      return null;
    }
  }

  function showLocalPathSetup() {
    const dialog = $("#local-path-setup");
    if (localStorage.getItem("playerBehavior.pathsConfirmed") ||
        sessionStorage.getItem("playerBehavior.pathsDeferred")) return;
    const notice = $("#local-path-feedback");
    $("#setup-template").value = state.settings.template_path;
    $("#setup-config").value = state.settings.config_path;
    $("#local-path-confirm").onclick = async () => {
      const paths = { template_path: $("#setup-template").value.trim(),
                      config_path: $("#setup-config").value.trim() };
      const button = $("#local-path-confirm");
      button.disabled = true;
      try {
        const statuses = await api("/api/settings/paths", { method: "POST", body: JSON.stringify(paths) });
        if (statuses.template_path?.status !== "ready" || statuses.config_path?.status !== "ready") {
          notice.textContent = "请选择两个可读取的目录，或点击「稍后配置」。";
          notice.hidden = false;
          return;
        }
        applySettings({ ...state.settings, ...paths });
        localStorage.setItem("playerBehavior.settings", JSON.stringify(state.settings));
        localStorage.setItem("playerBehavior.pathsConfirmed", "1");
        updatePathStatus(statuses);
        dialog.close();
      } catch (error) {
        notice.textContent = error.message;
        notice.hidden = false;
      } finally {
        button.disabled = false;
      }
    };
    const defer = () => sessionStorage.setItem("playerBehavior.pathsDeferred", "1");
    $("#local-path-skip").onclick = () => { defer(); dialog.close(); };
    dialog.addEventListener("cancel", defer, { once: true });
    dialog.showModal();
  }

  async function loadBootstrap() {
    setServiceStatus("checking", "正在连接本地服务");
    try {
      const data = await api("/api/bootstrap", { timeout: 20000 });
      state.bootstrap = data;
      state.csrfToken = data.csrf_token || "";
      state.serviceOk = data.ok !== false && data.service?.ok !== false && data.status?.server !== "error";
      const saved = readSavedSettings();
      applySettings(saved || data.paths || data.settings || DEFAULT_PATHS);
      updatePathStatus(saved ? {} : (data.path_status || data.paths_status || data.paths || {}));
      setServiceStatus(state.serviceOk ? "online" : "offline", state.serviceOk ? "本地服务已连接" : (data.service?.message || data.message || "本地服务不可用"));
      renderHistory(data.recent_jobs || data.jobs || []);
      updateInputAvailability("player");
      updateInputAvailability("online");
      restoreResultSnapshot("player");
      restoreResultSnapshot("online");
      if (saved) await refreshLocalPaths();
      showLocalPathSetup();
      await restoreCurrentJobs();
      if (data.demo === true && data.demo_data) hydrateServerDemo(data.demo_data);
    } catch (error) {
      state.serviceOk = false;
      applySettings(readSavedSettings() || DEFAULT_PATHS);
      updatePathStatus({});
      setServiceStatus("offline", "本地服务未连接");
      updateInputAvailability("player");
      updateInputAvailability("online");
      toast("无法连接本地分析服务，请使用启动器打开工具。", "error");
    }
  }

  function readSavedSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem("playerBehavior.settings") || "null");
      return saved && typeof saved === "object" ? saved : null;
    } catch {
      return null;
    }
  }

  async function restoreCurrentJobs() {
    const playerJob = localStorage.getItem("playerBehavior.currentPlayerJob");
    const onlineJob = localStorage.getItem("playerBehavior.currentOnlineJob");
    if (playerJob) await resumeJob(playerJob, "player");
    if (onlineJob) await resumeJob(onlineJob, "online");
  }

  function hydrateServerDemo(demo) {
    if (demo.player_result) {
      state.player.jobId = demo.player_job_id || "demo-player";
      state.player.result = demo.player_result;
      state.player.artifacts = normalizeArtifacts(demo.player_artifacts || {});
      renderBehaviorResult(state.player.result);
      $("#player-launch-card").classList.add("is-hidden");
      $("#behavior-result").classList.remove("is-hidden");
    }
    if (demo.classification_result) {
      state.online.jobId = demo.online_job_id || "demo-online";
      state.online.result = demo.classification_result;
      state.online.artifacts = normalizeArtifacts(demo.online_artifacts || {});
      renderClassificationResult(state.online.result);
    }
    updateResultLinks();
  }

  function clearCurrentFlow() {
    window.clearTimeout(state.player.pollTimer);
    window.clearTimeout(state.online.pollTimer);
    localStorage.removeItem("playerBehavior.currentPlayerJob");
    localStorage.removeItem("playerBehavior.currentOnlineJob");
    localStorage.removeItem("playerBehavior.playerResultSnapshot");
    localStorage.removeItem("playerBehavior.onlineResultSnapshot");
    state.player = { mode: "upload", files: [], payload: null, preflight: null, draftId: null, jobId: null, job: null, result: null, artifacts: {}, pollTimer: null };
    state.online = { mode: "upload", files: [], payload: null, preflight: null, draftId: null, jobId: null, job: null, result: null, artifacts: {}, pollTimer: null };
    $("#player-paste").value = "";
    $("#online-paste").value = "";
    $("#player-paste-count").textContent = "0 行 · 0 字符";
    $("#online-paste-count").textContent = "0 行 · 0 字符";
    renderFileList("player");
    renderFileList("online");
    $("#player-feedback").className = "form-feedback is-hidden";
    $("#online-feedback").className = "form-feedback is-hidden";
    $("#player-launch-card").classList.remove("is-hidden");
    $("#player-job-panel").classList.add("is-hidden");
    $("#behavior-result").classList.add("is-hidden");
    setBehaviorPageState("ready");
    $("#classification-progress").classList.add("is-hidden");
    $("#classification-result-content").classList.remove("is-hidden");
    $("#classification-kpis").replaceChildren();
    $("#tag-legend").replaceChildren();
    $("#classification-table-head").replaceChildren();
    $("#classification-table-body").replaceChildren();
    $("#behavior-artifact-list").replaceChildren();
    renderConfigExport();
    updateInputAvailability("player");
    updateInputAvailability("online");
    updateResultLinks();
    goStep(1, { force: true });
    toast("已清空浏览器中的当前流程；历史任务和导出文件未删除。");
  }

  function bindEvents() {
    $$('[data-route]').forEach((button) => button.addEventListener("click", () => route(button.dataset.route)));
    $$("#stepper button").forEach((button) => button.addEventListener("click", () => goStep(button.closest("li").dataset.step)));
    $$('[data-step-back]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.stepBack, { force: true })));
    $$('[data-go-step]').forEach((button) => button.addEventListener("click", () => {
      if (button.id === "behavior-next-button" && button.dataset.goStep === "1") {
        openPlayerSampleSupplement();
        return;
      }
      goStep(button.dataset.goStep, { force: true });
    }));
    $$('[data-download]').forEach((button) => button.addEventListener("click", () => downloadArtifact(button.dataset.download)));

    setupTabs();
    setupDropZone("player");
    setupDropZone("online");
    setupPaste("player");
    setupPaste("online");
    $("#sequence-strategy").addEventListener("change", () => {
      state.settings.sequence_strategy = $("#sequence-strategy").value;
      renderSequenceStrategy();
    });
    $("#level-label-method").addEventListener("change", () => {
      state.settings.level_label_method = $("#level-label-method").value;
    });

    $("#player-preflight-button").addEventListener("click", preflightPlayer);
    $("#start-player-job").addEventListener("click", startPlayerJob);
    $("#behavior-add-samples").addEventListener("click", openPlayerSampleSupplement);
    $("#online-preflight-button").addEventListener("click", preflightOnline);
    $("#behavior-search").addEventListener("input", (event) => renderFilteredTable("behavior", event.target.value));
    $("#classification-search").addEventListener("input", (event) => renderFilteredTable("classification", event.target.value));

    $("#open-behavior-result").addEventListener("click", () => goStep(2, { force: true }));
    $("#open-classification-result").addEventListener("click", () => goStep(4, { force: true }));
    $("#open-config-result").addEventListener("click", () => goStep(5, { force: true }));
    $("#refresh-history").addEventListener("click", async () => {
      try {
        const data = await api("/api/bootstrap");
        renderHistory(data.recent_jobs || data.jobs || []);
        toast("运行记录已刷新。");
      } catch (error) {
        toast(error.message, "error");
      }
    });

  document.querySelectorAll("[data-pick-path]").forEach(button => {
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.pickPath);
      const feedback = button.closest("dialog")?.querySelector("[role=status]");
      button.disabled = true;
      if (feedback) feedback.hidden = true;
      try {
        const payload = await api("/api/dialog/directory", { method: "POST", body: "{}" });
        if (payload.path) {
          input.value = payload.path;
        }
      } catch (error) {
        if (feedback) {
          feedback.textContent = error.message;
          feedback.hidden = false;
        } else {
          toast(error.message, "error");
        }
      } finally {
        button.disabled = false;
      }
    });
  });
    $("#save-settings").addEventListener("click", async () => {
      state.settings = currentSettings();
      localStorage.setItem("playerBehavior.settings", JSON.stringify(state.settings));
      const paths = await refreshLocalPaths();
      if (paths?.template_path?.status === "ready" && paths?.config_path?.status === "ready") {
        localStorage.setItem("playerBehavior.pathsConfirmed", "1");
      } else {
        localStorage.removeItem("playerBehavior.pathsConfirmed");
      }
      toast("路径已保存在本浏览器。");
      route("workspace");
    });
    $("#reset-paths").addEventListener("click", () => {
      applySettings(state.bootstrap?.paths || DEFAULT_PATHS);
      localStorage.removeItem("playerBehavior.settings");
      localStorage.removeItem("playerBehavior.pathsConfirmed");
      updatePathStatus(state.bootstrap?.paths || {});
    });

    const helpDialog = $("#help-dialog");
    $("#open-help").addEventListener("click", () => helpDialog.showModal());
    $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
    helpDialog.addEventListener("click", (event) => {
      if (event.target === helpDialog) helpDialog.close();
    });

    const restartDialog = $("#restart-dialog");
    $("#restart-button").addEventListener("click", () => restartDialog.showModal());
    $("#confirm-restart").addEventListener("click", clearCurrentFlow);
  }

  bindEvents();
  loadBootstrap();
})();
