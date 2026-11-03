let uploadPauseRequested = false;
let uploadController = null;
let currentParentId = null;
let historyOffset = 0;
let historyLoadedFor = "";
let currentDashboard = null;
let boardEditSource = null;
let boardEditDataset = null;
let boardEditorDirty = false;
let pendingDeletion = null;
const deletingDatasets = new Set();

function drawPieChart(values, labels, box) {
  if (values.some((value) => value < 0) || values.reduce((a, b) => a + b, 0) <= 0) { box.textContent = "饼图需要非负数值且总和大于零。"; box.hidden = false; return; }
  const points = values.slice(0, 10).map((value, index) => ({ value, label: labels[index] }));
  if (values.length > 10) points.push({ value: values.slice(10).reduce((a, b) => a + b, 0), label: "其他" });
  const total = points.reduce((sum, point) => sum + point.value, 0);
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 760 330");
  const colors = ["#327bd0", "#49adbc", "#67ae83", "#e7b354", "#ac80cb", "#de887d", "#7c9ec6", "#86bcba", "#c4ae80", "#9b91b7", "#bdc8d6"];
  let angle = -Math.PI / 2;
  points.forEach((point, index) => {
    const sweep = point.value / total * Math.PI * 2;
    if (point.value > 0) {
      const slice = document.createElementNS(ns, sweep > Math.PI * 2 - .00001 ? "circle" : "path");
      if (slice.tagName === "circle") { slice.setAttribute("cx", "185"); slice.setAttribute("cy", "160"); slice.setAttribute("r", "120"); }
      else slice.setAttribute("d", `M185,160 L${185 + Math.cos(angle) * 120},${160 + Math.sin(angle) * 120} A120,120 0 ${sweep > Math.PI ? 1 : 0},1 ${185 + Math.cos(angle + sweep) * 120},${160 + Math.sin(angle + sweep) * 120} Z`);
      slice.setAttribute("fill", colors[index]); slice.setAttribute("stroke", "white"); slice.setAttribute("stroke-width", "2");
      const title = document.createElementNS(ns, "title"); title.textContent = `${point.label}: ${point.value.toLocaleString()} (${(point.value / total * 100).toFixed(1)}%)`; slice.append(title); svg.append(slice);
    }
    const marker = document.createElementNS(ns, "rect"); marker.setAttribute("x", "365"); marker.setAttribute("y", 25 + index * 25); marker.setAttribute("width", "10"); marker.setAttribute("height", "10"); marker.setAttribute("rx", "3"); marker.setAttribute("fill", colors[index]);
    const text = document.createElementNS(ns, "text"); text.setAttribute("x", "385"); text.setAttribute("y", 35 + index * 25); text.setAttribute("fill", "#526a83"); text.setAttribute("font-size", "12"); text.textContent = `${point.label.slice(0, 18)} · ${(point.value / total * 100).toFixed(1)}%`;
    svg.append(marker, text); angle += sweep;
  });
  box.append(svg); box.hidden = false;
}

function resetWorkspaceState() {
  currentParentId = null; historyOffset = 0; historyLoadedFor = "";
  currentDashboard = null; boardEditSource = null; boardEditDataset = null; boardEditorDirty = false;
  $("followupContext").hidden = true; $("dashboardEditor").hidden = true;
  $("historyList").replaceChildren(); $("moreHistoryBtn").hidden = true;
  $("answerSection").hidden = true; $("dashboardSection").hidden = true;
}

function updateDatasetTools(item) {
  const cancellable = ["queued", "scanning", "importing", "awaiting_sheets", "cancelling"].includes(item.status);
  $("cancelImportBtn").hidden = !cancellable;
  $("cancelImportBtn").disabled = item.status === "cancelling";
  $("cancelImportBtn").textContent = item.status === "cancelling" ? "取消中…" : "取消导入";
  $("deleteDatasetBtn").disabled = busy || deletingDatasets.has(item.id);
  if (historyLoadedFor !== item.id && item.status === "ready") {
    resetWorkspaceState(); historyLoadedFor = item.id;
    loadHistory(item.id);
  }
}

async function cancelImport() {
  try {
    await api(`/api/datasets/${selectedId}/cancel`, { method: "POST" });
    notice("已请求取消导入，正在收尾并清理临时文件。");
    await refresh();
  } catch (error) { notice(error.message, true); }
}

function deleteDataset(item = selectedItem) {
  if (!item || deletingDatasets.has(item.id) || (busy && selectedId === item.id)) return;
  pendingDeletion = { ...item, email: currentEmail };
  $("deleteFileName").textContent = item.name;
  $("deleteDialogStatus").textContent = ["queued", "scanning", "importing", "cancelling", "awaiting_sheets"].includes(item.status)
    ? "确认后会先取消导入，再删除该文件。" : "";
  $("deleteDialogError").hidden = true;
  $("confirmDeleteBtn").disabled = false;
  $("confirmDeleteBtn").textContent = "确认删除";
  $("cancelDeleteBtn").disabled = false;
  if (!$("deleteDialog").open) $("deleteDialog").showModal();
  $("cancelDeleteBtn").focus();
}

function closeDeleteDialog(force = false) {
  if (!force && pendingDeletion && deletingDatasets.has(pendingDeletion.id)) return;
  if ($("deleteDialog").open) $("deleteDialog").close();
  pendingDeletion = null;
}

async function confirmDatasetDeletion() {
  const item = pendingDeletion;
  if (!item || deletingDatasets.has(item.id) || item.email !== currentEmail || !authenticated) return;
  const sameAccount = () => authenticated && currentEmail === item.email;
  deletingDatasets.add(item.id);
  $("confirmDeleteBtn").disabled = true;
  $("confirmDeleteBtn").textContent = "删除中…";
  $("cancelDeleteBtn").disabled = true;
  $("deleteDialogError").hidden = true;
  $("deleteDialogStatus").textContent = "正在检查文件状态…";
  try {
    let current = await api(`/api/datasets/${item.id}`);
    if (!sameAccount()) return;
    if (["queued", "scanning", "importing", "cancelling", "awaiting_sheets"].includes(current.status)) {
      $("deleteDialogStatus").textContent = "正在取消导入，完成后删除…";
      await api(`/api/datasets/${item.id}/cancel`, { method: "POST" });
      const deadline = Date.now() + 30000;
      do {
        await new Promise((resolve) => setTimeout(resolve, 300));
        if (!sameAccount()) return;
        current = await api(`/api/datasets/${item.id}`);
      } while (["queued", "scanning", "importing", "cancelling"].includes(current.status) && Date.now() < deadline);
      if (["queued", "scanning", "importing", "cancelling"].includes(current.status)) throw new Error("已请求取消导入，任务尚未结束。请稍后重试删除。");
    }
    if (["queued", "running"].includes(current.profile_status)) throw new Error("文件仍在统计数据质量，请等待统计完成后重试删除。");
    if (!sameAccount()) return;
    $("deleteDialogStatus").textContent = "正在清理文件、看板和历史记录…";
    const result = await api(`/api/datasets/${item.id}`, { method: "DELETE" });
    if (!sameAccount()) return;
    localStorage.removeItem(`analysis-upload:${item.email}:${item.name}:${item.size}`);
    dictionaryDrafts.delete(item.id);
    if (selectedId === item.id) {
      qualityViewKey = ""; dictionaryViewKey = "";
      selectedId = null; selectedItem = null; lastViewKey = ""; latestResult = null;
      resetWorkspaceState();
    }
    closeDeleteDialog(true);
    await refresh();
    if (sameAccount()) notice(`已删除“${item.name}”，释放约 ${formatBytes(result.released_bytes)} 空间。`);
  } catch (error) {
    if (sameAccount() && pendingDeletion?.id === item.id) {
      $("deleteDialogError").textContent = error.message;
      $("deleteDialogError").hidden = false;
      $("deleteDialogStatus").textContent = "删除未完成，文件仍会保留在列表中，可重试。";
    }
  } finally {
    deletingDatasets.delete(item.id);
    if (sameAccount()) {
      $("confirmDeleteBtn").disabled = false;
      $("confirmDeleteBtn").textContent = "确认删除";
      $("cancelDeleteBtn").disabled = false;
      if (selectedItem) updateDatasetTools(selectedItem);
    }
  }
}

function setFollowup(record) {
  currentParentId = record.history_id;
  $("followupText").textContent = `继续追问：${record.question.slice(0, 80)}`;
  $("followupContext").hidden = false;
}

function showAnalysisResult(result) {
  latestResult = result;
  $("answerText").textContent = result.answer + (result.truncated ? "\n\n表格仅显示前 200 行。" : "");
  if ((result.dictionary_version || 0) !== (selectedItem.dictionary_version || 0)) $("answerText").textContent += "\n\n此结果使用旧版指标口径，请重新分析。";
  $("sqlText").textContent = result.sql;
  $("resultTable").replaceChildren(makeTable(result.rows));
  drawChart(result.rows, result.chart);
  $("answerSection").hidden = false;
  $("answerSection").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadHistory(itemId, append = false) {
  if (!itemId) return;
  const offset = append ? historyOffset : 0;
  $("moreHistoryBtn").disabled = true;
  try {
    const result = await api(`/api/datasets/${itemId}/history?offset=${offset}&limit=30`);
    if (!authenticated || selectedId !== itemId) return;
    const list = $("historyList");
    if (!append) list.replaceChildren();
    if (!result.total) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "分析成功后会自动保存，可以随时回来继续追问。"; list.append(empty); }
    for (const record of result.items) {
      const button = document.createElement("button"); button.type = "button"; button.className = "history-entry";
      const text = document.createElement("div");
      const question = document.createElement("strong"); question.textContent = record.question;
      const date = document.createElement("small"); date.textContent = `${new Date(record.created_at).toLocaleString("zh-CN")} · ${record.row_count} 行结果`;
      const tag = document.createElement("span"); tag.className = "history-tag"; tag.textContent = record.parent_id ? "追问" : "新问题";
      text.append(question, date); button.append(text, tag);
      button.onclick = async () => {
        try {
          const full = await api(`/api/datasets/${itemId}/history/${record.id}`);
          if (selectedId !== itemId || !authenticated) return;
          setFollowup(full); $("questionInput").value = ""; showAnalysisResult(full);
        } catch (error) { notice(error.message, true); }
      };
      list.append(button);
    }
    historyOffset = offset + result.items.length;
    $("moreHistoryBtn").hidden = !result.has_more;
  } catch (error) { if (selectedId === itemId) notice(error.message, true); }
  finally { $("moreHistoryBtn").disabled = false; }
}

function editorSelect(role, options, value) {
  const select = document.createElement("select"); select.dataset.role = role;
  for (const [key, text] of options) { const option = document.createElement("option"); option.value = key; option.textContent = text; select.append(option); }
  if ([...select.options].some((option) => option.value === String(value))) select.value = value;
  return select;
}

function editorField(label, control) {
  const box = document.createElement("div");
  const text = document.createElement("label"); text.textContent = label; control.setAttribute("aria-label", label);
  box.append(text, control); return box;
}

function queryEditor(query = {}, chart = false) {
  const box = document.createElement("div"); box.className = "query-config";
  const grid = document.createElement("div"); grid.className = "query-grid";
  const table = editorSelect("table", selectedItem.tables.map((table) => [table.name, table.label]), query.table);
  const aggregate = editorSelect("aggregate", [["sum", "求和"], ["avg", "平均值"], ["count", "记录数"], ["count_distinct", "去重计数"], ["min", "最小值"], ["max", "最大值"]], query.aggregate || "sum");
  const measure = editorSelect("column", [], "");
  const dimension = editorSelect("dimension", [], "");
  grid.append(editorField("数据表", table));
  if (chart) grid.append(editorField("分组字段", dimension));
  grid.append(editorField("统计方式", aggregate), editorField("统计字段", measure));
  if (chart) {
    grid.append(editorField("日期分组", editorSelect("grain", [["", "原始值"], ["day", "按日"], ["month", "按月"], ["year", "按年"]], query.grain || "")));
    grid.append(editorField("排序", editorSelect("sort", [["value_desc", "统计值从高到低"], ["label_asc", "分组值从小到大"]], query.sort || "value_desc")));
    const limit = document.createElement("input"); limit.type = "number"; limit.min = 1; limit.max = 50; limit.value = query.limit || 20; limit.dataset.role = "limit";
    grid.append(editorField("显示数量（1～50）", limit));
  }
  const filters = document.createElement("div"); filters.className = "query-filters";
  const filterRows = document.createElement("div"); filterRows.dataset.role = "filters";
  const add = document.createElement("button"); add.type = "button"; add.className = "secondary compact"; add.textContent = "＋ 添加筛选条件";
  function addFilter(rule = {}) {
    if (filterRows.children.length >= 5) return notice("每个图表最多 5 个筛选条件。", true);
    const columns = selectedItem.tables.find((item) => item.name === table.value).columns;
    const row = document.createElement("div"); row.className = "query-filter";
    const column = editorSelect("filter-column", columns.map((column) => [column.name, column.name]), rule.column);
    const op = editorSelect("filter-op", [["eq", "等于"], ["ne", "不等于"], ["gte", "大于等于"], ["lte", "小于等于"], ["contains", "包含文字"]], rule.op || "eq");
    const value = document.createElement("input"); value.maxLength = 500; value.dataset.role = "filter-value"; value.value = rule.value || ""; value.placeholder = "筛选值";
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "移除"; remove.onclick = () => { row.remove(); boardEditorDirty = true; };
    row.append(column, op, value, remove); filterRows.append(row);
  }
  function fillColumns(initial) {
    const columns = selectedItem.tables.find((item) => item.name === table.value).columns;
    for (const [select, value] of [[measure, initial.column || columns.find((column) => column.semantic_type === "numeric_text" || /^(U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL)/.test(column.type))?.name], [dimension, initial.dimension]]) {
      select.replaceChildren();
      for (const column of columns) { const option = document.createElement("option"); option.value = column.name; option.textContent = `${column.name} · ${column.semantic_type === "numeric_text" ? "数值（保留原文）" : column.type}`; select.append(option); }
      if (value && columns.some((column) => column.name === value)) select.value = value;
    }
    filterRows.replaceChildren(); (initial.filters || []).forEach(addFilter);
    measure.disabled = aggregate.value === "count";
  }
  fillColumns(query);
  table.onchange = () => { fillColumns({}); boardEditorDirty = true; };
  aggregate.onchange = () => { measure.disabled = aggregate.value === "count"; };
  add.onclick = () => { addFilter(); boardEditorDirty = true; };
  filters.append(add, filterRows); box.append(grid, filters); return box;
}

function readQuery(row, chart) {
  const get = (role) => row.querySelector(`[data-role="${role}"]`).value;
  const query = { table: get("table"), column: get("column"), aggregate: get("aggregate"),
    filters: [...row.querySelector('[data-role="filters"]').children].map((filter) => ({ column: filter.querySelector('[data-role="filter-column"]').value, op: filter.querySelector('[data-role="filter-op"]').value, value: filter.querySelector('[data-role="filter-value"]').value })) };
  if (chart) Object.assign(query, { dimension: get("dimension"), grain: get("grain"), sort: get("sort"), limit: Number(get("limit")) });
  return query;
}

function addBoardRow(source, index, chart) {
  const list = $(chart ? "boardChartEditors" : "boardKPIEditors");
  if (list.children.length >= 12) return notice("最多添加 12 项。", true);
  const row = document.createElement("div"); row.className = "board-edit-row"; row.dataset.sourceIndex = index == null ? "" : index;
  const top = document.createElement("div"); top.className = "board-edit-top";
  const title = document.createElement("input"); title.className = "editor-title"; title.value = source.title || (chart ? "新图表" : "新指标"); title.maxLength = 60; title.required = true; title.setAttribute("aria-label", "标题"); top.append(title);
  if (chart) { const kind = editorSelect("kind", [["bar", "柱状图"], ["line", "折线图"], ["area", "面积图"], ["pie", "饼图"]], source.kind || "bar"); kind.className = "editor-kind"; top.append(kind); }
  const actions = document.createElement("div"); actions.className = "board-row-actions";
  for (const [label, action] of [["上移", () => { if (row.previousElementSibling) list.insertBefore(row, row.previousElementSibling); }], ["下移", () => { if (row.nextElementSibling) list.insertBefore(row.nextElementSibling, row); }], ["删除", () => row.remove()]]) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.onclick = () => { action(); boardEditorDirty = true; }; actions.append(button);
  }
  top.append(actions);
  const label = document.createElement("label"); label.className = "rebuild-label";
  const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.className = "rebuild-query"; checkbox.checked = index == null; checkbox.disabled = index == null;
  label.append(checkbox, document.createTextNode("重新选择统计方式"));
  const query = queryEditor(source.query || {}, chart); query.hidden = !checkbox.checked;
  function toggleQuery() {
    query.hidden = !checkbox.checked;
    query.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !checkbox.checked; });
    if (checkbox.checked) query.querySelector('[data-role="column"]').disabled = query.querySelector('[data-role="aggregate"]').value === "count";
  }
  toggleQuery();
  checkbox.onchange = () => { toggleQuery(); boardEditorDirty = true; };
  row.append(top, label, query); list.append(row);
}

function openDashboardEditor() {
  if (!currentDashboard || busy) return;
  boardEditSource = JSON.parse(JSON.stringify(currentDashboard)); boardEditDataset = selectedId; boardEditorDirty = false;
  $("boardTitleInput").value = boardEditSource.title; $("boardDescriptionInput").value = boardEditSource.description || "";
  $("boardKPIEditors").replaceChildren(); $("boardChartEditors").replaceChildren();
  boardEditSource.kpis.forEach((kpi, index) => addBoardRow(kpi, index, false));
  boardEditSource.charts.forEach((chart, index) => addBoardRow(chart, index, true));
  $("dashboardEditor").hidden = false; $("dashboardEditor").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeDashboardEditor() { $("dashboardEditor").hidden = true; boardEditSource = null; boardEditorDirty = false; }

async function saveBoard(event) {
  event.preventDefault();
  if (!boardEditSource || busy || selectedId !== boardEditDataset) return;
  function edits(list, chart) {
    return [...$(list).children].map((row) => ({ source_index: row.dataset.sourceIndex === "" ? null : Number(row.dataset.sourceIndex), title: row.querySelector(".editor-title").value,
      ...(chart ? { kind: row.querySelector(".editor-kind").value } : {}), query: row.querySelector(".rebuild-query").checked ? readQuery(row, chart) : null }));
  }
  const payload = { revision: boardEditSource.revision, title: $("boardTitleInput").value, description: $("boardDescriptionInput").value, kpis: edits("boardKPIEditors", false), charts: edits("boardChartEditors", true) };
  if (!payload.charts.length) return notice("请至少保留一张图表。", true);
  const itemId = boardEditDataset; busy = true;
  const controls = [...$("dashboardEditor").querySelectorAll("input,select,textarea,button")]; controls.forEach((control) => { control.dataset.wasDisabled = control.disabled; control.disabled = true; });
  try {
    const saved = await api(`/api/datasets/${itemId}/dashboard`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
    if (selectedId !== itemId || !authenticated) return;
    closeDashboardEditor(); renderDashboard(saved); notice("看板已重新计算并保存。");
  } catch (error) { notice(error.message, true); if (selectedId === itemId) loadDashboard(itemId); }
  finally { busy = false; controls.forEach((control) => { control.disabled = control.dataset.wasDisabled === "true"; }); }
}
