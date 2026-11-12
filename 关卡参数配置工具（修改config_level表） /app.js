const state = {
  csvRows: [],
  csvHeaderIndex: {},
  pasteRows: [],
  modifiedCsvText: "",
  verifyCsvText: "",
};

const elements = {
  importButton: document.getElementById("btn-import"),
  pasteButton: document.getElementById("btn-paste"),
  applyButton: document.getElementById("btn-apply"),
  verifyButton: document.getElementById("btn-verify"),
  ruleHelpButton: document.getElementById("btn-rule-help"),
  exportButton: document.getElementById("btn-export"),
  fileInput: document.getElementById("file-input"),
  importStatus: document.getElementById("import-status"),
  pasteStatus: document.getElementById("paste-status"),
  applyStatus: document.getElementById("apply-status"),
  verifyStatus: document.getElementById("verify-status"),
  exportStatus: document.getElementById("export-status"),
  backdrop: document.getElementById("modal-backdrop"),
  modalImport: document.getElementById("modal-import"),
  modalPaste: document.getElementById("modal-paste"),
  modalResult: document.getElementById("modal-result"),
  modalVerify: document.getElementById("modal-verify"),
  modalRuleSelect: document.getElementById("modal-rule-select"),
  modalRuleHelp: document.getElementById("modal-rule-help"),
  resultText: document.getElementById("result-text"),
  resultDetails: document.getElementById("result-details"),
  resultAdded: document.getElementById("result-added"),
  resultModified: document.getElementById("result-modified"),
  resultDetailsButton: document.getElementById("btn-result-details"),
  pasteBody: document.getElementById("paste-body"),
  addRowButton: document.getElementById("btn-add-row"),
  confirmPasteButton: document.getElementById("btn-confirm-paste"),
  verifyBeforeInput: document.getElementById("verify-before"),
  verifyAfterInput: document.getElementById("verify-after"),
  verifyRunButton: document.getElementById("btn-run-verify"),
  verifyExportButton: document.getElementById("btn-export-verify"),
  verifySummary: document.getElementById("verify-summary"),
  verifyHead: document.getElementById("verify-head"),
  verifyBody: document.getElementById("verify-body"),
  confirmRuleButton: document.getElementById("btn-confirm-rule"),
};

const REQUIRED_COLUMNS = [
  "LevelId",
  "TemplateId",
  "DupPara1",
  "DupPara2",
  "DupPara3",
  "DupPara4",
  "DupPara5",
  "DupPara6",
  "DupPara7",
  "DupPara8",
  "DupPara9",
  "DupPara10",
];

const DUP_COLUMNS = [
  "DupPara1",
  "DupPara2",
  "DupPara3",
  "DupPara4",
  "DupPara5",
  "DupPara6",
  "DupPara7",
  "DupPara8",
  "DupPara9",
  "DupPara10",
];

const DEFAULT_ROWS = 20;

function showModal(modal) {
  elements.backdrop.classList.remove("hidden");
  modal.classList.remove("hidden");
}

function hideModal(modal) {
  modal.classList.add("hidden");
  const anyOpen = !elements.modalImport.classList.contains("hidden")
    || !elements.modalPaste.classList.contains("hidden")
    || !elements.modalResult.classList.contains("hidden")
    || !elements.modalVerify.classList.contains("hidden")
    || !elements.modalRuleSelect.classList.contains("hidden")
    || !elements.modalRuleHelp.classList.contains("hidden");
  if (!anyOpen) {
    elements.backdrop.classList.add("hidden");
  }
}

function setStatus(el, text) {
  el.textContent = text;
}

function resetPasteTable() {
  elements.pasteBody.innerHTML = "";
  for (let i = 0; i < DEFAULT_ROWS; i += 1) {
    appendPasteRow();
  }
}

function appendPasteRow(values = []) {
  const row = document.createElement("tr");
  for (let i = 0; i < 12; i += 1) {
    const cell = document.createElement("td");
    cell.setAttribute("contenteditable", "true");
    cell.textContent = values[i] || "";
    row.appendChild(cell);
  }
  elements.pasteBody.appendChild(row);
}

function parseCsv(file) {
  return new Promise((resolve, reject) => {
    Papa.parse(file, {
      skipEmptyLines: false,
      complete: (results) => resolve(results.data),
      error: reject,
    });
  });
}

function buildHeaderIndex(headerRow) {
  const map = {};
  headerRow.forEach((name, idx) => {
    map[String(name).trim()] = idx;
  });
  return map;
}

function validateHeaderIndex(headerIndex) {
  return REQUIRED_COLUMNS.every((key) => key in headerIndex);
}

function getPasteRows() {
  const rows = [];
  const tableRows = Array.from(elements.pasteBody.querySelectorAll("tr"));
  tableRows.forEach((row) => {
    const cells = Array.from(row.querySelectorAll("td")).map((cell) =>
      cell.textContent.trim()
    );
    if (!cells[0]) {
      return;
    }
    rows.push(cells);
  });
  return rows;
}

function parsePasteRow(row) {
  const levelId = String(row[0] ?? "").trim();
  const templateId = String(row[1] ?? "").trim();
  const dupParas = row.slice(2, 12).map((value) => String(value ?? "").trim());
  const hasAnyDup = dupParas.some((value) => value !== "");
  return {
    levelId,
    templateId,
    dupParas,
    hasAnyDup,
  };
}

function parseDupParam(value) {
  const parts = String(value ?? "").trim().split("|");
  if (parts.length !== 4) return null;
  const a = Number(parts[0]);
  const b = Number(parts[3]);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return {
    a,
    seed: parts[1],
    luck: parts[2],
    b,
  };
}

function formatDupParam(parts) {
  return `${parts.a}|${parts.seed}|${parts.luck}|${parts.b}`;
}

function applyRule0(existingDup, providedDup, providedFlags) {
  const nextDup = [...existingDup];
  const providedIndexes = providedFlags
    .map((flag, idx) => (flag ? idx + 1 : 0))
    .filter((value) => value > 0);
  if (providedIndexes.length === 0) {
    return nextDup;
  }
  providedIndexes.forEach((idx) => {
    nextDup[idx - 1] = providedDup[idx - 1];
  });

  let baseStart = 0;
  if (providedFlags[0]) {
    baseStart = 1;
  } else if (providedFlags[1]) {
    baseStart = 2;
  } else {
    baseStart = Math.min(...providedIndexes);
  }

  const baseValue = nextDup[baseStart - 1];
  const baseParts = parseDupParam(baseValue);
  if (!baseParts) {
    return nextDup;
  }

  const startA = baseStart <= 1 ? 2 : baseStart;
  const startB = baseStart <= 1 ? 3 : baseStart + 1;

  for (let i = baseStart; i <= 10; i += 1) {
    if (providedFlags[i - 1]) {
      continue;
    }
    if (i < startA) {
      nextDup[i - 1] = baseValue;
      continue;
    }
    const aDec = i < startA ? 0 : 1 + Math.floor((i - startA) / 2);
    const bDec = i < startB ? 0 : 1 + Math.floor((i - startB) / 2);
    const nextA =
      baseParts.a <= 4 ? baseParts.a : Math.max(4, baseParts.a - aDec);
    const nextB =
      baseParts.b <= 3 ? baseParts.b : Math.max(3, baseParts.b - bDec);
    nextDup[i - 1] = formatDupParam({
      a: nextA,
      seed: baseParts.seed,
      luck: baseParts.luck,
      b: nextB,
    });
  }
  return nextDup;
}

function applyRule1(existingDup, providedDup, providedFlags) {
  const nextDup = [...existingDup];
  let lastProvidedIndex = -1;
  for (let i = 0; i < providedDup.length; i += 1) {
    if (providedFlags[i]) {
      nextDup[i] = providedDup[i];
      lastProvidedIndex = i;
    }
  }
  if (lastProvidedIndex === -1) {
    return nextDup;
  }
  for (let i = lastProvidedIndex + 1; i < nextDup.length; i += 1) {
    nextDup[i] = nextDup[lastProvidedIndex];
  }
  return nextDup;
}

function applyRuleWithStep(existingDup, providedDup, providedFlags, step) {
  const nextDup = [...existingDup];
  const providedIndexes = providedFlags
    .map((flag, idx) => (flag ? idx + 1 : 0))
    .filter((value) => value > 0);
  if (providedIndexes.length === 0) {
    return nextDup;
  }
  providedIndexes.forEach((idx) => {
    nextDup[idx - 1] = providedDup[idx - 1];
  });

  let baseStart = 0;
  if (providedFlags[0]) {
    baseStart = 1;
  } else if (providedFlags[1]) {
    baseStart = 2;
  } else {
    baseStart = Math.min(...providedIndexes);
  }

  const baseValue = nextDup[baseStart - 1];
  const baseParts = parseDupParam(baseValue);
  if (!baseParts) {
    return nextDup;
  }

  if (baseStart === 1 && !providedFlags[1]) {
    nextDup[1] = baseValue;
  }

  const startA = baseStart <= 2 ? 3 : baseStart;
  const startB = baseStart <= 2 ? 4 : baseStart + 1;

  for (let i = baseStart; i <= 10; i += 1) {
    if (providedFlags[i - 1]) {
      continue;
    }
    if (i < startA) {
      nextDup[i - 1] = baseValue;
      continue;
    }
    const aDec = i < startA ? 0 : 1 + Math.floor((i - startA) / step);
    const bDec = i < startB ? 0 : 1 + Math.floor((i - startB) / step);
    const nextA =
      baseParts.a <= 4 ? baseParts.a : Math.max(4, baseParts.a - aDec);
    const nextB =
      baseParts.b <= 3 ? baseParts.b : Math.max(3, baseParts.b - bDec);
    nextDup[i - 1] = formatDupParam({
      a: nextA,
      seed: baseParts.seed,
      luck: baseParts.luck,
      b: nextB,
    });
  }
  return nextDup;
}

function applyChanges(ruleType) {
  const levelIndex = state.csvHeaderIndex.LevelId;
  const templateIndex = state.csvHeaderIndex.TemplateId;
  const dupIndices = DUP_COLUMNS.map((col) => state.csvHeaderIndex[col]);

  const csvRows = state.csvRows.map((row) => [...row]);
  const dataRows = csvRows.slice(3);

  const existingIds = new Set(
    dataRows
      .map((row) => String(row[levelIndex] ?? "").trim())
      .filter((value) => value !== "")
  );

  let addedRowCount = 0;
  let modifiedRowCount = 0;
  const addedIds = [];
  const modifiedIds = new Set();
  const addedIdSet = new Set();

  state.pasteRows.forEach((row) => {
    const parsed = parsePasteRow(row);
    if (!parsed.levelId) {
      return;
    }
    if (!existingIds.has(parsed.levelId)) {
      const newRow = Array(csvRows[0].length).fill("");
      newRow[levelIndex] = parsed.levelId;
      newRow[templateIndex] = parsed.templateId;
      csvRows.push(newRow);
      dataRows.push(newRow);
      existingIds.add(parsed.levelId);
      addedIds.push(parsed.levelId);
      addedIdSet.add(parsed.levelId);
      addedRowCount += 1;
    }
  });

  state.pasteRows.forEach((row) => {
    const parsed = parsePasteRow(row);
    if (!parsed.levelId || !parsed.hasAnyDup) {
      return;
    }
    const providedFlags = parsed.dupParas.map((value) => value !== "");
    dataRows.forEach((csvRow) => {
      if (String(csvRow[levelIndex]).trim() !== parsed.levelId) {
        return;
      }
      const existingDup = dupIndices.map((index) =>
        String(csvRow[index] ?? "").trim()
      );
      let nextDup = existingDup;
      if (ruleType === "rule0") {
        nextDup = applyRule0(existingDup, parsed.dupParas, providedFlags);
      } else if (ruleType === "rule2") {
        nextDup = applyRuleWithStep(existingDup, parsed.dupParas, providedFlags, 3);
      } else if (ruleType === "rule3") {
        nextDup = applyRuleWithStep(existingDup, parsed.dupParas, providedFlags, 1);
      } else if (ruleType === "rule4") {
        nextDup = applyRuleWithStep(existingDup, parsed.dupParas, providedFlags, 2);
      } else {
        nextDup = applyRule1(existingDup, parsed.dupParas, providedFlags);
      }

      let rowChanged = false;
      nextDup.forEach((value, idx) => {
        if (String(csvRow[dupIndices[idx]] ?? "").trim() !== value) {
          rowChanged = true;
        }
        csvRow[dupIndices[idx]] = value;
      });

      if (rowChanged && !addedIdSet.has(parsed.levelId)) {
        modifiedRowCount += 1;
        modifiedIds.add(parsed.levelId);
      }
    });
  });

  const csvText = Papa.unparse(csvRows, {
    quotes: false,
  });

  state.modifiedCsvText = csvText;
  setStatus(elements.applyStatus, "已完成修改");
  elements.exportButton.disabled = !state.modifiedCsvText;
  return {
    addedRowCount,
    modifiedRowCount,
    addedIds,
    modifiedIds: Array.from(modifiedIds),
  };
}

function downloadCsv() {
  const blob = new Blob([state.modifiedCsvText], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "config_level_副本.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

elements.importButton.addEventListener("click", () => {
  elements.fileInput.click();
});

elements.fileInput.addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;

  try {
    const rows = await parseCsv(file);
    const headerIndex = buildHeaderIndex(rows[0] || []);
    if (!validateHeaderIndex(headerIndex)) {
      alert("CSV 表头不包含 LevelId、TemplateId 或 DupPara1~10，请检查文件。");
      return;
    }
    state.csvRows = rows;
    state.csvHeaderIndex = headerIndex;
    elements.pasteButton.disabled = false;
    setStatus(elements.importStatus, "已导入");
    showModal(elements.modalImport);
  } catch (error) {
    alert("读取 CSV 失败，请检查文件格式。");
  }
});

elements.pasteButton.addEventListener("click", () => {
  resetPasteTable();
  showModal(elements.modalPaste);
});

elements.addRowButton.addEventListener("click", () => {
  appendPasteRow();
});

elements.confirmPasteButton.addEventListener("click", () => {
  const rows = getPasteRows();
  state.pasteRows = rows;
  if (rows.length === 0) {
    setStatus(elements.pasteStatus, "未粘贴");
    hideModal(elements.modalPaste);
    return;
  }
  setStatus(elements.pasteStatus, `已粘贴 ${rows.length} 行`);
  elements.applyButton.disabled = false;
  hideModal(elements.modalPaste);
});

elements.applyButton.addEventListener("click", () => {
  if (!state.csvRows.length) {
    alert("请先导入 config_level.csv。");
    return;
  }
  if (!state.pasteRows.length) {
    alert("请先粘贴关卡 id/模板 id 和参数配置。");
    return;
  }
  showModal(elements.modalRuleSelect);
});

elements.confirmRuleButton.addEventListener("click", () => {
  const selected = document.querySelector('input[name="apply-rule"]:checked');
  const ruleType = selected ? selected.value : "rule1";
  hideModal(elements.modalRuleSelect);
  const { addedRowCount, modifiedRowCount, addedIds, modifiedIds } =
    applyChanges(ruleType);
  elements.resultText.textContent =
    `新增行数：${addedRowCount}；修改行数：${modifiedRowCount}。`;
  elements.resultAdded.textContent = addedIds.length
    ? `新增关卡ID：${addedIds.join("、")}`
    : "新增关卡ID：无";
  elements.resultModified.textContent = modifiedIds.length
    ? `修改关卡ID：${modifiedIds.join("、")}`
    : "修改关卡ID：无";
  elements.resultDetails.classList.add("hidden");
  elements.resultDetailsButton.textContent = "详情";
  showModal(elements.modalResult);
});

elements.exportButton.addEventListener("click", () => {
  if (!state.modifiedCsvText) {
    alert("请先执行配置修改。");
    return;
  }
  downloadCsv();
  setStatus(elements.exportStatus, "已导出");
});

elements.ruleHelpButton.addEventListener("click", () => {
  showModal(elements.modalRuleHelp);
});

function normalizeRow(row, width) {
  const normalized = [];
  for (let i = 0; i < width; i += 1) {
    const value = row && row[i] !== undefined ? row[i] : "";
    normalized.push(String(value).trim());
  }
  return normalized;
}

function rowsEqual(a, b, width) {
  for (let i = 0; i < width; i += 1) {
    if (String(a[i] ?? "").trim() !== String(b[i] ?? "").trim()) {
      return false;
    }
  }
  return true;
}

function renderVerifyTable(headerRow, rows) {
  elements.verifyHead.innerHTML = "";
  elements.verifyBody.innerHTML = "";
  const headRow = document.createElement("tr");
  headerRow.forEach((cell) => {
    const th = document.createElement("th");
    th.textContent = cell;
    headRow.appendChild(th);
  });
  elements.verifyHead.appendChild(headRow);

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((cell) => {
      const td = document.createElement("td");
      td.textContent = cell;
      tr.appendChild(td);
    });
    elements.verifyBody.appendChild(tr);
  });
}

async function runVerify() {
  const beforeFile = elements.verifyBeforeInput.files[0];
  const afterFile = elements.verifyAfterInput.files[0];
  if (!beforeFile || !afterFile) {
    alert("请同时选择修改前和修改后的 CSV 文件。");
    return;
  }

  try {
    const [beforeRows, afterRows] = await Promise.all([
      parseCsv(beforeFile),
      parseCsv(afterFile),
    ]);

    const beforeHeader = beforeRows[0] || [];
    const afterHeader = afterRows[0] || [];
    const beforeIndex = buildHeaderIndex(beforeHeader);
    const afterIndex = buildHeaderIndex(afterHeader);

    if (!("LevelId" in beforeIndex) || !("LevelId" in afterIndex)) {
      alert("CSV 表头缺少 LevelId，无法校验。");
      return;
    }

    const beforeData = beforeRows.slice(3);
    const afterData = afterRows.slice(3);
    const beforeMap = new Map();
    const afterMap = new Map();

    beforeData.forEach((row) => {
      const id = String(row[beforeIndex.LevelId] ?? "").trim();
      if (id) beforeMap.set(id, row);
    });

    afterData.forEach((row) => {
      const id = String(row[afterIndex.LevelId] ?? "").trim();
      if (id) afterMap.set(id, row);
    });

    const reportHeader = [
      "变更类型",
      "LevelId",
      "TemplateId",
      ...DUP_COLUMNS.map((col) => `修改前${col}`),
      ...DUP_COLUMNS.map((col) => `修改后${col}`),
    ];
    const reportRows = [];
    let modifiedCount = 0;
    let addedCount = 0;

    afterMap.forEach((afterRow, id) => {
      const beforeRow = beforeMap.get(id);
      const beforeDup = DUP_COLUMNS.map((col) => {
        const idx = beforeIndex[col];
        return beforeRow && idx !== undefined ? String(beforeRow[idx] ?? "").trim() : "";
      });
      const afterDup = DUP_COLUMNS.map((col) => {
        const idx = afterIndex[col];
        return idx !== undefined ? String(afterRow[idx] ?? "").trim() : "";
      });
      const templateId =
        afterIndex.TemplateId !== undefined
          ? String(afterRow[afterIndex.TemplateId] ?? "").trim()
          : "";

      if (!beforeRow) {
        addedCount += 1;
        reportRows.push([
          "新增",
          id,
          templateId,
          ...Array(10).fill(""),
          ...afterDup,
        ]);
        return;
      }

      const isChanged = beforeDup.some((value, idx) => value !== afterDup[idx]);
      if (isChanged) {
        modifiedCount += 1;
        reportRows.push(["修改", id, templateId, ...beforeDup, ...afterDup]);
      }
    });

    const csvText = Papa.unparse([reportHeader, ...reportRows], {
      quotes: false,
    });
    state.verifyCsvText = csvText;
    elements.verifyExportButton.disabled = reportRows.length === 0;
    renderVerifyTable(reportHeader, reportRows);
    elements.verifySummary.textContent =
      `修改：${modifiedCount} 行；新增：${addedCount} 行；校验表行数：${reportRows.length}`;
    setStatus(elements.verifyStatus, "已生成校验表");
  } catch (error) {
    alert("校验失败，请检查两个 CSV 文件格式。");
  }
}

function downloadVerifyCsv() {
  const blob = new Blob([state.verifyCsvText], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "校验表.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

elements.verifyButton.addEventListener("click", () => {
  elements.verifyBeforeInput.value = "";
  elements.verifyAfterInput.value = "";
  elements.verifyHead.innerHTML = "";
  elements.verifyBody.innerHTML = "";
  elements.verifySummary.textContent = "";
  elements.verifyExportButton.disabled = true;
  state.verifyCsvText = "";
  showModal(elements.modalVerify);
});

elements.verifyRunButton.addEventListener("click", () => {
  runVerify();
});

elements.verifyExportButton.addEventListener("click", () => {
  if (!state.verifyCsvText) {
    alert("请先生成校验表。");
    return;
  }
  downloadVerifyCsv();
});

elements.backdrop.addEventListener("click", () => {
  [
    elements.modalImport,
    elements.modalPaste,
    elements.modalResult,
    elements.modalVerify,
    elements.modalRuleSelect,
    elements.modalRuleHelp,
  ].forEach((modal) => hideModal(modal));
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", (event) => {
    const targetId = event.currentTarget.getAttribute("data-close");
    const modal = document.getElementById(targetId);
    if (modal) hideModal(modal);
  });
});

elements.resultDetailsButton.addEventListener("click", () => {
  const hidden = elements.resultDetails.classList.contains("hidden");
  if (hidden) {
    elements.resultDetails.classList.remove("hidden");
    elements.resultDetailsButton.textContent = "收起";
  } else {
    elements.resultDetails.classList.add("hidden");
    elements.resultDetailsButton.textContent = "详情";
  }
});

elements.pasteBody.addEventListener("paste", (event) => {
  const text = event.clipboardData.getData("text");
  if (!text) return;
  const activeCell = document.activeElement;
  if (!activeCell || activeCell.tagName !== "TD") return;

  event.preventDefault();

  const rows = text
    .trimEnd()
    .split(/\r?\n/)
    .map((line) => line.split("\t"));

  const startRow = activeCell.parentElement.rowIndex - 1;
  const startCol = activeCell.cellIndex;
  const tableRows = elements.pasteBody.querySelectorAll("tr");

  rows.forEach((rowValues, rowOffset) => {
    const targetRowIndex = startRow + rowOffset;
    while (targetRowIndex >= elements.pasteBody.rows.length) {
      appendPasteRow();
    }
    const targetRow = tableRows[targetRowIndex] || elements.pasteBody.rows[targetRowIndex];
    if (!targetRow) return;
    rowValues.forEach((value, colOffset) => {
      const targetCell = targetRow.cells[startCol + colOffset];
      if (targetCell) {
        targetCell.textContent = value;
      }
    });
  });
});
