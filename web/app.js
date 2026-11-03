const $ = (id) => document.getElementById(id);
let selectedId = null;
let selectedItem = null;
let lastViewKey = "";
let latestResult = null;
let busy = false;
let authenticated = false;
let authMode = "login";
let legacyAvailable = false;
let currentEmail = "";

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : await response.text();
  if (response.status === 401 && !path.startsWith("/api/auth/")) { authMode = "login"; showAuth("登录已失效，请重新登录"); }
  if (!response.ok) {
    const detail = typeof data === "object" ? data.detail : data;
    throw new Error(typeof detail === "string" ? detail : "输入内容不符合要求，请检查必填项和长度限制");
  }
  return data;
}

function showAuth(message = "") {
  closeDeleteDialog(true);
  authenticated = false;
  $("appScreen").hidden = true;
  $("authScreen").hidden = false;
  $("authFormEyebrow").textContent = ({ login: "WELCOME BACK", register: "CREATE ACCOUNT", legacy: "ACCOUNT MIGRATION" })[authMode];
  $("authTitle").textContent = ({ login: "欢迎回来", register: "创建你的账号", legacy: "绑定旧账号" })[authMode];
  $("authHint").textContent = ({ login: "登录后，继续你的数据分析。", register: "使用邮箱注册，密码至少 8 位。", legacy: "输入旧用户名和密码，将原有数据转到邮箱账号。" })[authMode];
  $("authSubmit").textContent = ({ login: "登录", register: "注册并进入", legacy: "绑定邮箱并进入" })[authMode];
  $("authSwitch").textContent = authMode === "login" ? "没有账号？邮箱注册" : "已有账号？返回登录";
  $("legacySwitch").hidden = !legacyAvailable || authMode === "legacy";
  $("legacyWrap").hidden = authMode !== "legacy";
  $("legacyUsername").required = authMode === "legacy";
  $("confirmWrap").hidden = authMode !== "register";
  $("authConfirm").required = authMode === "register";
  $("authPassword").autocomplete = authMode === "register" ? "new-password" : "current-password";
  $("authError").textContent = message;
  $("authError").hidden = !message;
}

async function startApp(email) {
  if (currentEmail !== email) { selectedId = null; selectedItem = null; lastViewKey = ""; latestResult = null; resetQualityViews(); resetWorkspaceState(); $("workspace").hidden = true; $("emptyState").hidden = false; $("datasetList").replaceChildren(); }
  currentEmail = email;
  authenticated = true;
  $("authScreen").hidden = true;
  $("appScreen").hidden = false;
  $("accountName").textContent = email;
  $("authPassword").value = "";
  $("authConfirm").value = "";
  try {
    const health = await api("/api/health");
    $("modelBadge").textContent = health.model_ready ? `${health.provider} 已配置` : `需要配置 ${health.provider} 密钥`;
    $("modelBadge").className = `badge ${health.model_ready ? "ok" : "warn"}`;
    await refresh();
  } catch (error) { if (authenticated) notice(error.message, true); }
}

async function submitAuth(event) {
  event.preventDefault();
  const email = $("authEmail").value.trim();
  const password = $("authPassword").value;
  if (authMode === "register" && password !== $("authConfirm").value) {
    $("authError").textContent = "两次输入的密码不一致";
    $("authError").hidden = false;
    return;
  }
  $("authSubmit").disabled = true;
  try {
    const path = ({ login: "/api/auth/login", register: "/api/auth/register", legacy: "/api/auth/migrate" })[authMode];
    const account = await api(path, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ email, password, ...(authMode === "legacy" ? { username: $("legacyUsername").value.trim() } : {}) }),
    });
    if (authMode === "legacy") legacyAvailable = false;
    authMode = "login";
    await startApp(account.email);
  } catch (error) {
    $("authError").textContent = error.message;
    $("authError").hidden = false;
  } finally { $("authSubmit").disabled = false; }
}

async function boot() {
  try {
    const status = await api("/api/auth/status");
    legacyAvailable = status.legacy_available;
    if (status.authenticated) await startApp(status.email);
    else showAuth();
  } catch (error) { showAuth(`无法连接本机服务：${error.message}`); }
}

function notice(message, error = false) {
  const box = $("notice");
  box.textContent = message;
  box.className = error ? "notice error" : "notice";
  box.hidden = !message;
}

function formatBytes(value) {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function statusName(status) {
  return ({ uploading: "待续传 / 上传中", queued: "排队中", scanning: "读取中", awaiting_sheets: "待选工作表", importing: "导入中", ready: "已就绪", error: "失败", interrupted: "已中断", cancelling: "正在取消", cancelled: "已取消" })[status] || status;
}

async function refresh() {
  if (!authenticated) return;
  const email = currentEmail;
  try {
    const items = await api("/api/datasets");
    if (!authenticated || email !== currentEmail) return;
    const list = $("datasetList");
    list.replaceChildren();
    if (!items.length) {
      const p = document.createElement("p"); p.className = "muted"; p.textContent = "还没有数据集"; list.append(p);
    }
    for (const item of items) {
      const row = document.createElement("div"); row.className = "dataset-row";
      const button = document.createElement("button");
      button.type = "button";
      button.className = `dataset-item${item.id === selectedId ? " active" : ""}`;
      const title = document.createElement("strong"); title.textContent = item.name;
      const meta = document.createElement("small"); meta.textContent = `${statusName(item.status)} · ${formatBytes(item.size)}`;
      button.append(title, meta);
      button.onclick = () => { if (boardEditorDirty && item.id !== selectedId && !window.confirm("看板修改尚未保存，仍要切换数据集吗？")) return; selectedId = item.id; lastViewKey = ""; refresh(); };
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "dataset-delete";
      remove.textContent = deletingDatasets.has(item.id) ? "删除中" : "删除";
      remove.title = `删除 ${item.name}`;
      remove.setAttribute("aria-label", `删除文件 ${item.name}`);
      remove.disabled = deletingDatasets.has(item.id) || (busy && selectedId === item.id);
      remove.onclick = () => deleteDataset(item);
      row.append(button, remove); list.append(row);
    }
    if (!items.some((item) => item.id === selectedId)) selectedId = items[0]?.id || null;
    const summary = items.find((x) => x.id === selectedId) || null;
    const detailKey = summary ? JSON.stringify(summary) : "";
    if (!summary) selectedItem = null;
    else if (selectedItem?.id !== selectedId || selectedItem._summaryKey !== detailKey) {
      const itemId = selectedId;
      const detail = await api(`/api/datasets/${itemId}`);
      if (!authenticated || currentEmail !== email || selectedId !== itemId) return;
      selectedItem = { ...detail, _summaryKey: detailKey };
    }
    renderSelected();
  } catch (error) { notice(error.message, true); }
}

function renderSelected() {
  const item = selectedItem;
  $("emptyState").hidden = !!item;
  $("workspace").hidden = !item;
  if (!item) return;
  $("datasetTitle").textContent = item.name;
  $("datasetMeta").textContent = `${formatBytes(item.size)} · ${item.extension.toUpperCase()} · 本机占用 ${formatBytes(item.storage_bytes || 0)} · ${item.progress}`;
  updateDatasetTools(item);
  const pill = $("datasetStatus");
  pill.textContent = statusName(item.status);
  pill.className = `status-pill ${item.status === "ready" ? "ready" : item.status === "error" ? "error" : ""}`;
  if (item.status === "ready") renderQualityAndDictionary(item);
  const viewKey = `${item.id}:${item.status}:${(item.tables || []).length}`;
  if (viewKey === lastViewKey) return;
  lastViewKey = viewKey;
  $("sheetSection").hidden = item.status !== "awaiting_sheets";
  $("waitingSection").hidden = !["uploading", "queued", "scanning", "importing", "cancelling"].includes(item.status);
  $("errorSection").hidden = !["error", "interrupted", "cancelled"].includes(item.status);
  $("readySection").hidden = item.status !== "ready";
  $("answerSection").hidden = true;
  if (item.status === "awaiting_sheets") renderSheets(item.sheets || []);
  if (["uploading", "queued", "scanning", "importing", "cancelling"].includes(item.status)) {
    $("waitingTitle").textContent = statusName(item.status);
    $("waitingText").textContent = item.progress;
  }
  if (["error", "interrupted", "cancelled"].includes(item.status)) $("errorText").textContent = item.error || item.progress;
  if (item.status === "ready") {
    $("excelReadSection").hidden = false;
    if ([".xlsx", ".csv"].includes(item.extension)) {
      $("excelReadSummary").textContent = item.content_version >= 4
        ? "按原始内容读取。独立区域分别显示，日期、编号及文本保留原文；文本金额经过整列校验后可用于统计。请核对列名、预览及下方读取提示。"
        : "这是旧版读取结果。建议点击“重新读取”，生成一个新数据集，更新公式检查、文本金额识别和完整列读取；原数据集会保留。";
      $("excelReadWarnings").replaceChildren();
      (item.import_warnings || []).forEach((warning) => { const p = document.createElement("p"); p.textContent = warning; $("excelReadWarnings").append(p); });
    }
    renderTables(item.tables || []);
    $("dashboardSection").hidden = true;
    loadDashboard(item.id);
  }
}

function renderSheets(sheets) {
  const box = $("sheetList"); box.replaceChildren();
  sheets.forEach((sheet, index) => {
    const label = document.createElement("label"); label.className = "sheet-option";
    const input = document.createElement("input"); input.type = "checkbox"; input.value = sheet; input.checked = index === 0;
    const name = document.createElement("span"); name.textContent = sheet;
    label.append(input, name); box.append(label);
  });
}

function makeTable(rows) {
  const outer = document.createElement("div"); outer.className = "table-scroll";
  if (!rows.length) { outer.textContent = "没有数据"; return outer; }
  const table = document.createElement("table"); const thead = document.createElement("thead");
  const header = document.createElement("tr");
  const columns = Object.keys(rows[0]);
  columns.forEach((column) => { const th = document.createElement("th"); th.textContent = column; header.append(th); });
  thead.append(header); table.append(thead);
  const body = document.createElement("tbody");
  rows.forEach((row) => { const tr = document.createElement("tr"); columns.forEach((column) => { const td = document.createElement("td"); td.textContent = row[column] == null ? "" : String(row[column]); td.title = td.textContent; tr.append(td); }); body.append(tr); });
  table.append(body); outer.append(table); return outer;
}

function renderTables(tables) {
  const box = $("tableCards"); box.replaceChildren();
  tables.forEach((table) => {
    const card = document.createElement("div"); card.className = "table-card";
    const title = document.createElement("h3"); title.textContent = table.label;
    const subtitle = document.createElement("p"); subtitle.textContent = `数据表 ${table.name} · ${table.rows.toLocaleString()} 行 · ${table.columns.length} 列`;
    if (table.layout?.mode === "text") subtitle.textContent += " · 段落文字，保留原始行号";
    if (table.layout?.header_rows?.length) subtitle.textContent += ` · 表头位于原表第 ${table.layout.header_rows.join("、")} 行`;
    if (table.layout?.source_range) subtitle.textContent += ` · 原始区域 ${table.layout.source_range}`;
    const chips = document.createElement("div"); chips.className = "chips";
    table.columns.slice(0, 12).forEach((column) => { const chip = document.createElement("span"); chip.className = "chip"; chip.textContent = `${column.name} · ${{ date_text: "日期（原文）", numeric_text: "数值（保留原文）" }[column.semantic_type] || column.type}`; chips.append(chip); });
    card.append(title, subtitle, chips, makeTable(table.preview || [])); box.append(card);
  });
}

async function upload() {
  const file = $("fileInput").files[0];
  if (!file) return notice("请先选择文件", true);
  if (file.size > 1024 ** 3) return notice("文件超过 1GB 上限", true);
  if (!/\.(csv|xlsx)$/i.test(file.name)) return notice("只支持 CSV 和 XLSX", true);
  if (busy) return;
  busy = true; $("uploadBtn").disabled = true; $("uploadProgress").hidden = false; notice("");
  uploadPauseRequested = false; $("pauseUploadBtn").hidden = false;
  try {
    const resumeKey = `analysis-upload:${currentEmail}:${file.name}:${file.size}`;
    let id = localStorage.getItem(resumeKey);
    let received = 0;
    if (id) {
      try {
        const old = await api(`/api/datasets/${id}`);
        if (old.status === "uploading" && old.name === file.name && old.size === file.size) received = old.received;
        else id = null;
      } catch { id = null; }
    }
    if (!id) {
      const created = await api("/api/uploads", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: file.name, size: file.size, encoding: $("encoding").value }) });
      id = created.id; localStorage.setItem(resumeKey, id);
    }
    selectedId = id; await refresh();
    const chunkSize = 8 * 1024 * 1024;
    while (received < file.size) {
      if (uploadPauseRequested) throw new Error("上传已暂停，重新点击上传可续传。");
      const end = Math.min(received + chunkSize, file.size);
      const chunk = file.slice(received, end);
      let saved = false;
      for (let attempt = 0; attempt < 3 && !saved; attempt++) {
        try {
          uploadController = new AbortController();
          const result = await api(`/api/uploads/${id}/chunk?offset=${received}`, { method: "PUT", body: chunk, signal: uploadController.signal });
          received = result.received; saved = true;
        } catch (error) {
          if (uploadPauseRequested) throw new Error("上传已暂停，重新点击上传可续传。");
          if (attempt === 2) throw error;
          const current = await api(`/api/datasets/${id}`);
          received = current.received;
          if (received >= end) saved = true;
        }
      }
      const percent = Math.round(received / file.size * 100);
      $("progressBar").style.width = `${percent}%`;
      $("progressText").textContent = `${percent}% · ${formatBytes(received)} / ${formatBytes(file.size)}`;
    }
    if (uploadPauseRequested) throw new Error("上传已暂停，重新点击上传可续传。");
    await api(`/api/uploads/${id}/complete`, { method: "POST" });
    localStorage.removeItem(resumeKey);
    notice("上传完成，正在后台处理数据");
    await refresh();
  } catch (error) { notice(error.message, !uploadPauseRequested); }
  finally { busy = false; uploadController = null; $("uploadBtn").disabled = false; $("pauseUploadBtn").hidden = true; }
}

async function importSheets() {
  if (!selectedId) return;
  const sheets = [...$("sheetList").querySelectorAll("input:checked")].map((input) => input.value);
  if (!sheets.length) return notice("请至少选择一个工作表", true);
  try {
    await api(`/api/datasets/${selectedId}/import`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sheets }) });
    notice("已开始导入工作表"); await refresh();
  } catch (error) { notice(error.message, true); }
}

async function retryImport() {
  if (!selectedItem) return;
  try {
    await api(`/api/datasets/${selectedId}/import`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sheets: selectedItem.extension === ".xlsx" ? (selectedItem.selected_sheets || (selectedItem.sheets || []).slice(0, 1)) : [] }) });
    notice("已重新开始导入"); await refresh();
  } catch (error) { notice(error.message, true); }
}

function drawChart(rows, kind, targetBox = null) {
  const box = targetBox || $("chartBox"); box.replaceChildren(); box.hidden = true;
  if (kind === "none" || !rows.length) return;
  const keys = Object.keys(rows[0] || {});
  const valueKey = keys.find((key) => rows.every((row) => row[key] == null || (typeof row[key] === "number" && Number.isFinite(row[key]))));
  const labelKey = keys.find((key) => key !== valueKey);
  if (!valueKey || !labelKey) return;
  const values = rows.slice(0, 50).map((row) => Number(row[valueKey]) || 0);
  const labels = rows.slice(0, 50).map((row) => String(row[labelKey] ?? ""));
  if (kind === "pie") { drawPieChart(values, labels, box); return; }
  const width = 760, height = 310, left = 58, right = 18, top = 20, bottom = 67;
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = max - min || 1;
  const y = (value) => top + (max - value) / span * (height - top - bottom);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const line = (x1, y1, x2, y2, color = "#dce6ef") => { const el = document.createElementNS(svg.namespaceURI, "line"); for (const [k, v] of Object.entries({ x1, y1, x2, y2, stroke: color })) el.setAttribute(k, v); svg.append(el); };
  const text = (x, yy, value, anchor = "middle") => { const el = document.createElementNS(svg.namespaceURI, "text"); el.setAttribute("x", x); el.setAttribute("y", yy); el.setAttribute("font-size", "11"); el.setAttribute("fill", "#71859a"); el.setAttribute("text-anchor", anchor); el.textContent = value; svg.append(el); };
  for (let i = 0; i <= 4; i++) { const value = min + span * i / 4; const yy = y(value); line(left, yy, width - right, yy); text(left - 8, yy + 4, Number(value.toFixed(2)).toLocaleString(), "end"); }
  const plotWidth = width - left - right, step = plotWidth / values.length;
  if (kind === "line" || kind === "area") {
    if (kind === "area") {
      const area = document.createElementNS(svg.namespaceURI, "polygon");
      area.setAttribute("points", `${left + step / 2},${y(0)} ` + values.map((value, i) => `${left + step * (i + .5)},${y(value)}`).join(" ") + ` ${left + step * (values.length - .5)},${y(0)}`);
      area.setAttribute("fill", "#b4d8fb"); area.setAttribute("opacity", ".65"); svg.append(area);
    }
    const poly = document.createElementNS(svg.namespaceURI, "polyline");
    poly.setAttribute("points", values.map((value, i) => `${left + step * (i + .5)},${y(value)}`).join(" "));
    poly.setAttribute("fill", "none"); poly.setAttribute("stroke", "#2878cf"); poly.setAttribute("stroke-width", "3"); svg.append(poly);
    values.forEach((value, i) => { const circle = document.createElementNS(svg.namespaceURI, "circle"); circle.setAttribute("cx", left + step * (i + .5)); circle.setAttribute("cy", y(value)); circle.setAttribute("r", "4"); circle.setAttribute("fill", "#2878cf"); svg.append(circle); });
  } else values.forEach((value, i) => { const rect = document.createElementNS(svg.namespaceURI, "rect"); const yy = Math.min(y(value), y(0)); rect.setAttribute("x", left + step * (i + .17)); rect.setAttribute("y", yy); rect.setAttribute("width", Math.max(2, step * .66)); rect.setAttribute("height", Math.max(1, Math.abs(y(0) - y(value)))); rect.setAttribute("rx", "3"); rect.setAttribute("fill", "#3187d8"); svg.append(rect); });
  labels.forEach((label, i) => { if (i % Math.max(1, Math.ceil(labels.length / 10))) return; text(left + step * (i + .5), height - 42, label.length > 10 ? label.slice(0, 9) + "…" : label); });
  text(width / 2, height - 10, valueKey);
  box.append(svg); box.hidden = false;
}

function formatMetric(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "—");
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function renderDashboard(data) {
  currentDashboard = data;
  $("dashboardTitle").textContent = data.title;
  $("dashboardDescription").textContent = data.description || "";
  const kpiGrid = $("kpiGrid"); kpiGrid.replaceChildren();
  for (const kpi of data.kpis || []) {
    const card = document.createElement("div"); card.className = "kpi-card";
    const label = document.createElement("span"); label.textContent = kpi.title;
    const value = document.createElement("strong"); value.textContent = formatMetric(kpi.value);
    const detail = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "计算 SQL";
    const code = document.createElement("pre"); code.textContent = kpi.sql + (kpi.parameters?.length ? `\n参数：${JSON.stringify(kpi.parameters)}` : "");
    detail.append(summary, code); card.append(label, value, detail); kpiGrid.append(card);
  }
  const insight = $("dashboardInsight"); insight.textContent = data.insight || ""; insight.hidden = !data.insight;
  const chartGrid = $("chartGrid"); chartGrid.replaceChildren();
  for (const chart of data.charts || []) {
    const card = document.createElement("div"); card.className = "chart-card";
    const title = document.createElement("h3"); title.textContent = chart.title;
    const plot = document.createElement("div"); plot.className = "chart-plot";
    drawChart(chart.rows || [], chart.kind, plot);
    const note = document.createElement("p"); note.className = "hint"; note.textContent = chart.truncated ? "仅展示前 50 个数据点" : `${(chart.rows || []).length} 个数据点`;
    if (chart.kind === "pie" && chart.rows.length > 10) note.textContent += "；第 10 项之后合并为其他";
    const detail = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "查看计算 SQL";
    const code = document.createElement("pre"); code.textContent = chart.sql + (chart.parameters?.length ? `\n参数：${JSON.stringify(chart.parameters)}` : "");
    detail.append(summary, code); card.append(title, plot, note, detail); chartGrid.append(card);
  }
  const warnings = $("dashboardWarnings");
  const messages = [...(data.warnings || [])];
  if ((data.dictionary_version || 0) !== (selectedItem?.dictionary_version || 0)) messages.unshift("字段说明或指标口径已更新，这份看板使用的是旧版口径，请重新生成。");
  warnings.textContent = messages.join("\n"); warnings.hidden = !messages.length;
  $("dashboardSection").hidden = false;
}

async function loadDashboard(itemId) {
  try {
    const result = await api(`/api/datasets/${itemId}/dashboard`);
    if (selectedId === itemId) renderDashboard(result);
  } catch (error) {
    if (error.message !== "尚未生成数据看板") notice(error.message, true);
  }
}

async function generateDashboard() {
  if (!selectedId || busy) return;
  if (boardEditSource) return notice("请先保存或取消看板编辑。", true);
  if (dictionaryDrafts.has(selectedId)) return notice("请先保存字段说明与指标口径，再生成看板。", true);
  const itemId = selectedId;
  busy = true; $("dashboardBtn").disabled = true; $("dashboardBtn").textContent = "生成中…"; notice("正在生成看板，可能需要几十秒");
  try {
    const result = await api(`/api/datasets/${itemId}/dashboard`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ focus: $("dashboardFocus").value.trim() }),
    });
    if (selectedId === itemId) {
      renderDashboard(result);
      notice("看板已生成并保存到本机");
      $("dashboardSection").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) { notice(error.message, true); }
  finally { busy = false; $("dashboardBtn").disabled = false; $("dashboardBtn").textContent = "重新生成看板"; }
}

async function ask() {
  const question = $("questionInput").value.trim();
  if (!question) return notice("请输入问题", true);
  if (busy) return;
  if (dictionaryDrafts.has(selectedId)) return notice("请先保存字段说明与指标口径，再开始分析。", true);
  const itemId = selectedId;
  busy = true; $("askBtn").disabled = true; $("askBtn").textContent = "分析中…"; notice("");
  try {
    const result = await api(`/api/datasets/${itemId}/ask`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question, parent_id: currentParentId }) });
    if (!authenticated || selectedId !== itemId) return;
    setFollowup(result); showAnalysisResult(result); loadHistory(itemId);
  } catch (error) { notice(error.message, true); }
  finally { busy = false; $("askBtn").disabled = false; $("askBtn").textContent = "开始分析"; }
}

function downloadResult() {
  if (!latestResult || !latestResult.rows.length) return notice("没有可下载的结果", true);
  const keys = Object.keys(latestResult.rows[0]);
  const quote = (value) => `"${String(value == null ? "" : value).replaceAll('"', '""')}"`;
  const content = "\ufeff" + [keys.map(quote).join(","), ...latestResult.rows.map((row) => keys.map((key) => quote(row[key])).join(","))].join("\r\n");
  const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" })); link.download = "分析结果.csv"; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

$("fileInput").onchange = () => { const file = $("fileInput").files[0]; $("fileName").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "尚未选择文件"; };
$("uploadBtn").onclick = upload;
$("pauseUploadBtn").onclick = () => { uploadPauseRequested = true; uploadController?.abort(); };
$("cancelImportBtn").onclick = cancelImport;
$("deleteDatasetBtn").onclick = () => deleteDataset(selectedItem);
$("confirmDeleteBtn").onclick = confirmDatasetDeletion;
$("cancelDeleteBtn").onclick = () => closeDeleteDialog();
$("deleteDialog").addEventListener("cancel", (event) => { event.preventDefault(); closeDeleteDialog(); });
$("refreshHistoryBtn").onclick = () => loadHistory(selectedId);
$("moreHistoryBtn").onclick = () => loadHistory(selectedId, true);
$("newQuestionBtn").onclick = () => { currentParentId = null; $("followupContext").hidden = true; $("questionInput").value = ""; $("questionInput").focus(); };
$("editDashboardBtn").onclick = openDashboardEditor;
$("dashboardEditor").onsubmit = saveBoard;
$("dashboardEditor").oninput = () => { boardEditorDirty = true; };
$("dashboardEditor").onchange = () => { boardEditorDirty = true; };
$("cancelBoardEditBtn").onclick = () => { if (!boardEditorDirty || window.confirm("放弃尚未保存的看板修改？")) closeDashboardEditor(); };
$("addBoardKPIBtn").onclick = () => { addBoardRow({}, null, false); boardEditorDirty = true; };
$("addBoardChartBtn").onclick = () => { addBoardRow({}, null, true); boardEditorDirty = true; };
$("refreshBtn").onclick = refresh;
$("importBtn").onclick = importSheets;
$("retryBtn").onclick = retryImport;
$("reprocessExcelBtn").onclick = async () => {
  if (!selectedItem || busy) return;
  busy = true; $("reprocessExcelBtn").disabled = true;
  try {
    const result = await api(`/api/datasets/${selectedId}/reprocess`, { method: "POST" });
    selectedId = result.id; selectedItem = null; lastViewKey = "";
    resetWorkspaceState(); await refresh();
    notice("已创建新数据集并开始重新读取，原数据集和历史记录保留。");
  } catch (error) { notice(error.message, true); }
  finally { busy = false; $("reprocessExcelBtn").disabled = false; }
};
$("askBtn").onclick = ask;
$("dashboardBtn").onclick = generateDashboard;
$("dashboardFocus").onkeydown = (event) => { if (event.key === "Enter") generateDashboard(); };
$("printDashboardBtn").onclick = () => window.print();
$("questionInput").onkeydown = (event) => { if (event.key === "Enter") ask(); };
$("downloadBtn").onclick = downloadResult;
$("profileBtn").onclick = generateProfile;
$("dictionaryForm").onsubmit = saveDictionary;
$("dictionaryForm").oninput = markDictionaryDirty;
$("addMetricBtn").onclick = () => {
  if ($("metricDefinitions").children.length >= 20) return notice("最多保存 20 个业务指标。", true);
  addMetricRow(); markDictionaryDirty();
};
window.addEventListener("beforeunload", (event) => { if (dictionaryDrafts.size || boardEditorDirty) { event.preventDefault(); event.returnValue = ""; } });
$("authForm").onsubmit = submitAuth;
$("authSwitch").onclick = () => { authMode = authMode === "login" ? "register" : "login"; showAuth(); };
$("legacySwitch").onclick = () => { authMode = "legacy"; showAuth(); };
$("logoutBtn").onclick = async () => {
  try { await api("/api/auth/logout", { method: "POST" }); }
  catch (error) { notice(error.message, true); return; }
  selectedId = null; selectedItem = null; lastViewKey = ""; latestResult = null;
  resetQualityViews();
  resetWorkspaceState();
  authMode = "login";
  showAuth();
};

boot(); setInterval(refresh, 2000);
