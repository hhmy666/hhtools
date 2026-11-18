"use strict";

const byId = id => document.getElementById(id);
const form = byId("simulation-form");
const runButton = byId("run-simulation");
const repoDialog = byId("repository-dialog");
const repositoryField = byId("repository");
const svgNamespace = "http://www.w3.org/2000/svg";
const storageKey = "simulation.repository";
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const percentage = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 2 });
const state = {
  configurationFile: null,
  templateFiles: new Map(),
  repository: "",
  busy: false,
  jobId: null,
  hasJob: false,
  connectionErrors: 0,
  costHoldoutWarning: "",
};

function icon(name) {
  const node = document.createElementNS(svgNamespace, "svg");
  node.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svgNamespace, "use");
  use.setAttribute("href", "#i-" + name);
  node.append(use);
  return node;
}

function element(tag, text) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  return node;
}

function note(id, message) {
  const output = byId(id);
  output.textContent = message;
  output.hidden = !message;
}

async function request(path, payload) {
  const response = await fetch(path, payload === undefined ? { cache: "no-store" } : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => null);
  if (!response.ok || !result) {
    const message = result?.message || result?.error?.message || result?.error || result?.detail;
    const error = new Error(typeof message === "string" ? message : "本地服务响应异常（HTTP " + response.status + "）。");
    error.status = response.status;
    throw error;
  }
  return result;
}

function displayRepository(path) {
  state.repository = path;
  const summary = byId("repository-summary");
  summary.textContent = path || "未设置模板仓库";
  summary.title = path;
}

function serviceStatus(online) {
  const status = byId("service-status");
  status.dataset.state = online ? "online" : "offline";
  status.lastElementChild.textContent = online ? "本地服务" : "本地服务未连接";
}

async function loadSettings() {
  try {
    const settings = await request("/api/settings");
    let savedRepository = "";
    try { savedRepository = localStorage.getItem(storageKey) || ""; } catch {}
    displayRepository(savedRepository || settings.repository || "");
    serviceStatus(true);
    if (typeof settings.cost_holdout_warning === "string") state.costHoldoutWarning = settings.cost_holdout_warning;
    const status = byId("model-status");
    status.dataset.state = settings.model_available ? "ready" : "unavailable";
    const modelDetails = ["玩家行为模型已加载", number.format(settings.profile_count || 0) + " 个训练画像"];
    if (settings.verified_replays) modelDetails.push(number.format(settings.verified_replays) + " 局用于拟合");
    if (settings.behavior_replays) {
      const levels = settings.behavior_level_range;
      const range = Array.isArray(levels) && levels.length === 2 ? "（" + levels.join("–") + " 关）" : "";
      modelDetails.push(number.format(settings.behavior_replays) + " 局用于行为校准" + range);
    }
    status.lastElementChild.textContent = settings.model_available
      ? modelDetails.join(" · ")
      : "未找到可用的线上玩家行为模型";
  } catch {
    serviceStatus(false);
    byId("model-status").lastElementChild.textContent = "无法读取玩家行为模型状态";
    if (!state.repository) displayRepository("");
  }
}

function openRepository() {
  repositoryField.value = state.repository;
  note("repository-error", "");
  repoDialog.showModal();
}

function updateRepositoryMode() {
  const manual = repoDialog.querySelector('input[name="repo-mode"]:checked').value === "manual";
  repositoryField.readOnly = !manual;
  byId("choose-directory").hidden = manual;
  note("repository-error", "");
}

byId("open-repository").addEventListener("click", openRepository);
byId("change-repository").addEventListener("click", openRepository);
byId("close-repository").addEventListener("click", () => repoDialog.close());
repoDialog.querySelector(".repo-mode-select").addEventListener("change", updateRepositoryMode);
byId("open-help").addEventListener("click", () => byId("help-dialog").showModal());

byId("choose-directory").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  note("repository-error", "");
  try {
    const response = await request("/api/choose-directory", {});
    if (response.repository) repositoryField.value = response.repository;
  } catch (error) {
    note("repository-error", error.message + " 可切换到「手动输入路径」。");
  } finally {
    button.disabled = false;
  }
});

byId("repository-form").addEventListener("submit", event => {
  event.preventDefault();
  const path = repositoryField.value.trim();
  displayRepository(path);
  try { localStorage.setItem(storageKey, path); } catch {}
  repoDialog.close();
});

function renderFiles() {
  const list = byId("file-list");
  list.replaceChildren();
  const allFiles = state.configurationFile ? [state.configurationFile] : [];
  allFiles.push(...state.templateFiles.values());

  for (const file of allFiles) {
    const entry = element("li");
    const name = element("span", file.name);
    name.title = file.name;
    const remove = element("button");
    remove.className = "button button-quiet icon-button";
    remove.type = "button";
    remove.setAttribute("aria-label", "移除 " + file.name);
    remove.append(icon("close"));
    remove.addEventListener("click", () => {
      const isConfiguration = file === state.configurationFile;
      if (isConfiguration) state.configurationFile = null;
      else state.templateFiles.delete(file.name);
      renderFiles();
      (list.querySelector("button") || byId(isConfiguration ? "config-file" : "template-files")).focus();
    });
    entry.append(name, remove);
    list.append(entry);
  }
  list.hidden = !allFiles.length;
}

function acceptFiles(files, configuration) {
  const selected = Array.from(files);
  if (!selected.length) return;
  const pattern = configuration ? /\.(?:csv|tsv|txt)$/i : /\.bytes$/i;
  if (selected.some(file => !pattern.test(file.name))) {
    note("import-error", configuration ? "请上传 CSV、TSV 或纯文本配置文件。" : "请上传 .bytes 模板文件。");
    return;
  }
  if (configuration && selected.length !== 1) {
    note("import-error", "请一次上传一个关卡配置文件。");
    return;
  }
  if (configuration) state.configurationFile = selected[0];
  else for (const file of selected) state.templateFiles.set(file.name, file);
  note("import-error", "");
  renderFiles();
}

function bindUploads(inputId, dropId, configuration) {
  const input = byId(inputId);
  const drop = byId(dropId);
  input.addEventListener("change", () => {
    acceptFiles(input.files, configuration);
    input.value = "";
  });
  drop.addEventListener("dragover", event => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    drop.classList.add("is-over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", event => {
    event.preventDefault();
    drop.classList.remove("is-over");
    acceptFiles(event.dataTransfer.files, configuration);
  });
}

bindUploads("config-file", "configuration-upload", true);
bindUploads("template-files", "template-upload", false);

byId("fill-example").addEventListener("click", () => {
  const paste = byId("paste");
  const example = "xkd141\nxkd141\nmxd0069\nmxd0069\n\n15|3|100|4\n16|3|100|4\n12|2|100|4\n13|2|100|4";
  if (paste.value.trim()) {
    paste.focus();
    paste.setSelectionRange(paste.value.length, paste.value.length);
    return;
  }
  paste.value = example;
  paste.dispatchEvent(new Event("input"));
  paste.focus();
});

function markStep(index) {
  const steps = document.querySelectorAll(".workflow a");
  steps.forEach((step, stepIndex) => {
    if (stepIndex === index) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
    const completed = stepIndex < index;
    step.classList.toggle("is-complete", completed);
    const marker = step.firstElementChild;
    if (completed && !marker.firstElementChild) marker.replaceChildren(icon("check"));
    if (!completed) marker.textContent = stepIndex + 1;
  });
}

function showStep(index) {
  byId("configuration").hidden = index === 2;
  byId("import").hidden = index !== 0;
  byId("settings").hidden = index !== 1;
  byId("results").hidden = index !== 2;
  byId("page-title").parentElement.hidden = index !== 0;
  document.querySelector(".workspace").classList.toggle("is-results-view", index === 2);
  for (const id of ["configuration", "results"]) {
    const nav = byId("nav-" + id);
    const active = (id === "results") === (index === 2);
    nav.classList.toggle("is-active", active);
    if (active) nav.setAttribute("aria-current", "location");
    else nav.removeAttribute("aria-current");
  }
  markStep(index);
  const heading = byId(["import-title", "settings-title", "results-title"][index]);
  heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function advanceToSettings() {
  if (!byId("paste").value.trim() && !state.configurationFile && !state.templateFiles.size) {
    note("import-error", "请粘贴模板 ID 或副本配置，或上传关卡配置或模板。");
    byId("paste").focus();
    return;
  }
  note("import-error", "");
  showStep(1);
}

byId("next-settings").addEventListener("click", advanceToSettings);
byId("back-import").addEventListener("click", () => showStep(0));
byId("back-settings").addEventListener("click", advanceToSettings);
document.querySelectorAll('a[href="#configuration"],a[href="#import"],a[href="#settings"],a[href="#results"]').forEach(anchor => {
  anchor.addEventListener("click", event => {
    event.preventDefault();
    const href = anchor.getAttribute("href");
    if (href === "#settings") advanceToSettings();
    else if (href === "#results") {
      if (state.hasJob) showStep(2);
    } else showStep(0);
  });
});
byId("paste").addEventListener("input", () => {
  byId("fill-example").disabled = Boolean(byId("paste").value.trim());
  note("import-error", "");
});

const metric = byId("target-metric");
const target = byId("target-value");
metric.addEventListener("change", () => {
  byId("target-value-group").hidden = !metric.value;
  target.required = Boolean(metric.value);
  const isWinRate = metric.value === "naked_win_rate";
  byId("target-unit").textContent = isWinRate ? "%" : "";
  target.value = "";
  if (isWinRate) target.max = 100;
  else target.removeAttribute("max");
});
form.addEventListener("invalid", event => {
  if (event.target.closest("details")) byId("advanced").open = true;
}, true);

function readRanges() {
  const ranges = {};
  for (const [prefix, label] of [["a", "A 单图案组数"], ["b", "B 单图案组数"], ["luck", "Lucky"], ["seed", "随机种子"]]) {
    const min = byId(prefix + "_min");
    const max = byId(prefix + "_max");
    if (min.value && max.value && min.valueAsNumber > max.valueAsNumber) {
      byId("advanced").open = true;
      max.focus();
      throw new Error(label + "的最大值不能小于最小值。");
    }
    for (const field of [min, max]) if (field.value) ranges[field.id] = field.valueAsNumber;
  }
  return ranges;
}

function readTemplateFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve({ name: file.name, data: reader.result.split(",")[1] }));
    reader.addEventListener("error", () => reject(new Error("无法读取模板文件 " + file.name + "。")));
    reader.readAsDataURL(file);
  });
}

function setBusy(busy) {
  state.busy = busy;
  runButton.disabled = busy;
  runButton.replaceChildren(document.createTextNode(busy ? "正在模拟" : "开始模拟"), icon("arrow"));
}

function formatMetric(value, formatter = number) {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? formatter.format(Number(value)) : "—";
}

const minutes = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function renderResults(rows) {
  const body = document.querySelector("#results-table tbody");
  const fragment = document.createDocumentFragment();
  function resultRow(row, rank) {
    const tr = element("tr");
    const template = element("th", row.template_id || "—");
    template.scope = "row";
    if (Number.isInteger(row.fuuu_profile)) {
      const profile = element("small", "FUUU 档位 " + number.format(row.fuuu_profile));
      profile.className = "fuuu-profile";
      template.append(" ", profile);
    }
    const parameters = element("td", row.parameters || "—");
    if (row.recommended) {
      const badge = element("span", rank === 1 ? "最接近目标" : "方案 " + rank);
      badge.className = "recommendation";
      parameters.append(badge);
      if (Number.isFinite(row.target_value) && Number.isFinite(row.target_delta)) {
        const win = row.target_metric === "naked_win_rate";
        const goal = win ? formatMetric(row.target_value, percentage) : formatMetric(row.target_value);
        const gap = (row.target_delta > 0 ? "+" : row.target_delta < 0 ? "−" : "")
          + (win ? formatMetric(Math.abs(row.target_delta * 100)) + " 个百分点" : formatMetric(Math.abs(row.target_delta)));
        const detail = element("small", "目标 " + goal + " · 差 " + gap);
        detail.className = "recommendation-detail";
        parameters.append(detail);
      }
    }
    tr.append(template, element("td", row.duplicate ?? "—"), parameters);

    const metrics = [
      [formatMetric(row.mean_cost)],
      [formatMetric(row.naked_win_rate, percentage), "metric-win"],
      [formatMetric(row.mean_duration_seconds == null ? null : row.mean_duration_seconds / 60, minutes)],
      [formatMetric(row.failure_fuuu, percentage)],
      [formatMetric(row.failure_dead_end_progress, percentage)],
      [formatMetric(row.win_fuuu, percentage)],
      [formatMetric(row.high_pressure_count)],
      [formatMetric(row.trials)],
    ];
    for (const [value, className] of metrics) {
      const cell = element("td", value);
      cell.classList.add("numeric");
      if (className) cell.classList.add(className);
      tr.append(cell);
    }
    return { tr, parameters };
  }
  for (const [index, row] of rows.entries()) {
    const { tr, parameters } = resultRow(row, 1);
    fragment.append(tr);
    const alternatives = Array.isArray(row.alternatives) ? row.alternatives : [];
    if (!alternatives.length) continue;
    const extraRows = alternatives.map((alternative, i) => {
      const { tr: extra } = resultRow({ ...row, ...alternative }, i + 2);
      extra.id = "alternative-" + index + "-" + i;
      extra.hidden = true;
      fragment.append(extra);
      return extra;
    });
    const toggle = element("button", "展开其余 " + extraRows.length + " 套");
    toggle.type = "button";
    toggle.className = "text-button alternative-toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", extraRows.map(extra => extra.id).join(" "));
    toggle.addEventListener("click", () => {
      const expand = toggle.getAttribute("aria-expanded") === "false";
      for (const extra of extraRows) extra.hidden = !expand;
      toggle.setAttribute("aria-expanded", String(expand));
      toggle.textContent = expand ? "收起备选" : "展开其余 " + extraRows.length + " 套";
    });
    parameters.append(toggle);
  }
  body.replaceChildren(fragment);
  byId("results-empty").hidden = rows.length > 0 || state.busy;
  byId("results-table-wrap").hidden = rows.length === 0;
  byId("results-count").textContent = rows.length ? number.format(rows.length) + " 项模拟结果" : "";
}

function renderWarnings(warnings, rows = [], costWarning = "") {
  const notice = byId("result-notice");
  const messages = new Set((Array.isArray(warnings) ? warnings : [warnings]).filter(Boolean));
  if (costWarning) messages.add(costWarning);
  const methods = new Set();
  for (const row of rows) {
    const notes = Array.isArray(row.notes) ? row.notes : [row.notes];
    for (const message of notes) {
      if (typeof message === "string" && message.trim() && !messages.has(message)) methods.add(message);
    }
  }
  const open = notice.querySelector("details")?.open ?? false;
  const listOf = items => {
    const list = element("ul");
    for (const message of items) {
      list.append(element("li", typeof message === "string" ? message : message.message || JSON.stringify(message)));
    }
    return list;
  };
  notice.replaceChildren();
  if (messages.size) {
    notice.append(element("strong", "结果说明"), listOf(messages));
  }
  if (methods.size) {
    const details = element("details");
    details.open = open;
    details.append(element("summary", "查看模拟口径与假设"), listOf(methods));
    notice.append(details);
  }
  notice.hidden = messages.size === 0 && methods.size === 0;
}

function renderProgress(label, message, value) {
  byId("job-status").hidden = false;
  for (const [id, content] of [["progress-label", label], ["progress-detail", message || ""]]) {
    if (byId(id).textContent !== content) byId(id).textContent = content;
  }
  const bar = byId("job-progress");
  bar.hidden = false;
  byId("progress-percent").hidden = false;
  if (Number.isFinite(value)) {
    bar.value = Math.round(Math.max(0, Math.min(100, value)));
    byId("progress-percent").textContent = bar.value + "%";
  } else {
    bar.removeAttribute("value");
    byId("progress-percent").textContent = "";
  }
}

function handleJob(job) {
  const rows = Array.isArray(job.rows) ? job.rows : [];
  if (job.status === "done" || job.status === "error") {
    setBusy(false);
    state.jobId = null;
    renderResults(rows);
    byId("results-empty").hidden = true;
    const costWarning = job.cost_holdout_warning || (rows.length ? state.costHoldoutWarning : "");
    renderWarnings(job.warnings || [], rows, costWarning);
    renderProgress(job.status === "done" ? "模拟完成" : "模拟失败", job.message, job.status === "done" ? 100 : job.progress);
    byId("job-progress").hidden = job.status === "done";
    byId("progress-percent").hidden = job.status === "done";
    if (job.status === "done" && !rows.length && !job.message) {
      byId("progress-detail").textContent = "没有可展示的副本结果，请检查关卡配置。";
    }
    return;
  }
  renderProgress("正在模拟", job.message, job.progress);
  window.setTimeout(pollJob, 900);
}

async function pollJob() {
  const jobId = state.jobId;
  if (!jobId) return;
  byId("retry-job").hidden = true;
  try {
    const job = await request("/api/jobs/" + encodeURIComponent(jobId));
    if (state.jobId !== jobId) return;
    state.connectionErrors = 0;
    serviceStatus(true);
    handleJob(job);
  } catch (error) {
    if (state.jobId !== jobId) return;
    if (error.status === 404) {
      serviceStatus(true);
      handleJob({ status: "error", message: error.message + " 可返回设置重新模拟。" });
      return;
    }
    const current = byId("job-progress").getAttribute("value");
    serviceStatus(false);
    state.connectionErrors++;
    const retry = state.connectionErrors < 4;
    renderProgress("无法读取进度", retry ? "正在尝试重新连接本地服务。" : error.message + " 可继续检查同一任务。", current === null ? undefined : Number(current));
    if (retry) window.setTimeout(pollJob, 2000);
    else byId("retry-job").hidden = false;
  }
}

byId("retry-job").addEventListener("click", () => {
  state.connectionErrors = 0;
  pollJob();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy || byId("settings").hidden || !form.reportValidity()) return;
  const paste = byId("paste").value.trim();
  const config = state.configurationFile;
  const templates = Array.from(state.templateFiles.values());
  if (!paste && !config && !templates.length) {
    showStep(0);
    advanceToSettings();
    return;
  }
  note("form-error", "");
  let ranges;
  try {
    ranges = readRanges();
  } catch (error) {
    note("form-error", error.message);
    return;
  }
  const payload = {
    paste,
    repository: state.repository,
    trials: byId("trials").valueAsNumber,
    target_metric: metric.value,
    target_value: metric.value ? target.valueAsNumber : null,
    ranges,
  };
  setBusy(true);
  try {
    payload.file_text = config ? await config.text() : "";
    if (payload.file_text.includes("\uFFFD")) throw new Error("关卡配置编码无法识别，请以 UTF-8 编码保存后重新上传。");
    payload.template_files = await Promise.all(templates.map(readTemplateFile));
    const job = await request("/api/simulate", payload);
    if (!job.job_id) throw new Error(job.message || "本地服务未返回任务编号。");
    state.jobId = job.job_id;
    state.connectionErrors = 0;
    renderResults([]);
    renderWarnings([]);
    renderProgress("正在启动模拟", "已创建任务，等待本地服务返回进度。");
    state.hasJob = true;
    for (const link of document.querySelectorAll('a[href="#results"]')) link.removeAttribute("aria-disabled");
    showStep(2);
    await pollJob();
  } catch (error) {
    setBusy(false);
    note("form-error", error.message);
    byId("form-error").scrollIntoView({ block: "nearest" });
  }
});

loadSettings();
