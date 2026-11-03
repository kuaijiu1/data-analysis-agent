let qualityViewKey = "";
let dictionaryViewKey = "";
const dictionaryDrafts = new Map();

function resetQualityViews() {
  qualityViewKey = ""; dictionaryViewKey = ""; dictionaryDrafts.clear();
}

function renderQualityAndDictionary(item) {
  const key = `${item.id}:${item.profile_status}:${item.profile_updated_at}:${item.profile_error}`;
  if (key !== qualityViewKey) { qualityViewKey = key; renderQuality(item); }
  renderDictionary(item);
}

function renderQuality(item) {
  const working = ["queued", "running"].includes(item.profile_status);
  const complete = item.tables.every((table) => table.profile);
  $("profileBtn").disabled = working;
  $("profileBtn").textContent = working ? "统计中…" : complete ? "重新统计" : "生成概览";
  const status = $("profileStatus");
  status.className = `muted${item.profile_status === "error" ? " quality-error" : ""}`;
  status.textContent = working ? "正在本机统计数据质量，完成后会自动显示。" : item.profile_error ? `统计未完成：${item.profile_error}。可以重新统计；已有概览仍可查看。` : complete ? "缺失值包含 NULL 和空白文本；空值比例及范围统计全表，常见取值与不同值数量统计前 10,000 行（不足时使用全部行）。" : "这份旧数据尚无质量概览，点击“生成概览”即可补充。";
  const summary = $("qualitySummary"); summary.replaceChildren();
  const columns = item.tables.flatMap((table) => table.profile?.columns || []);
  if (columns.length) {
    for (const [label, value] of [["已统计字段", columns.length], ["存在缺失的字段", columns.filter((column) => column.missing_count > 0).length], ["全部缺失的字段", item.tables.flatMap((table) => (table.profile?.columns || []).filter((column) => table.rows > 0 && column.missing_count === table.rows)).length]]) {
      const card = document.createElement("div"); card.className = "quality-stat";
      const text = document.createElement("span"); text.textContent = label;
      const number = document.createElement("strong"); number.textContent = value;
      card.append(text, number); summary.append(card);
    }
  }
  const tables = $("qualityTables"); tables.replaceChildren();
  for (const table of item.tables) {
    if (!table.profile) continue;
    const box = document.createElement("div"); box.className = "quality-table";
    const title = document.createElement("h3"); title.textContent = table.label;
    const sample = document.createElement("p"); sample.className = "hint";
    sample.textContent = `全表 ${table.rows.toLocaleString()} 行 · 常见值样本为前 ${table.profile.sample_rows.toLocaleString()} 行 · 括号内为样本出现次数`;
    const rows = table.profile.columns.map((column) => ({
      "字段": column.name, "类型": column.type,
      "缺失比例": `${column.missing_percent}%`,
      "NULL / 空白": `${column.null_count} / ${column.blank_count}`,
      "最小值": column.min ?? "—", "最大值": column.max ?? "—",
      "样本不同值数": column.sample_distinct,
      "常见取值（样本）": column.top_values.map((value) => `${value.value}${value.truncated ? "…" : ""}（${value.count}）`).join("；") || "—",
    }));
    box.append(title, sample, makeTable(rows)); tables.append(box);
  }
}

function dictionaryFromForm() {
  return {
    description: $("dictionaryDescription").value,
    fields: [...$("fieldDefinitions").querySelectorAll("textarea")].filter((input) => input.value.trim()).map((input) => ({ table: input.dataset.table, column: input.dataset.column, description: input.value })),
    metrics: [...$("metricDefinitions").children].map((row) => ({ name: row.querySelector("input").value, definition: row.querySelector("textarea").value })),
  };
}

function markDictionaryDirty() {
  if (!selectedId) return;
  dictionaryDrafts.set(selectedId, dictionaryFromForm());
  $("dictionaryStatus").textContent = "有未保存的修改";
  $("dictionaryStatus").className = "definition-status dirty";
  $("saveDictionaryBtn").disabled = false;
}

function addMetricRow(metric = { name: "", definition: "" }) {
  const row = document.createElement("div"); row.className = "metric-definition";
  const name = document.createElement("input"); name.value = metric.name; name.maxLength = 80; name.placeholder = "指标名称，如客单价"; name.setAttribute("aria-label", "指标名称");
  const definition = document.createElement("textarea"); definition.value = metric.definition; definition.maxLength = 1500; definition.rows = 2; definition.placeholder = "计算公式、统计范围、单位、过滤条件…"; definition.setAttribute("aria-label", "指标计算口径");
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "metric-remove"; remove.textContent = "删除";
  remove.onclick = () => { row.remove(); markDictionaryDirty(); };
  row.append(name, definition, remove); $("metricDefinitions").append(row);
}

function renderDictionary(item) {
  const key = `${item.id}:${item.dictionary_version || 0}:${item.tables.map((table) => table.columns.length).join(",")}`;
  if (key === dictionaryViewKey) return;
  dictionaryViewKey = key;
  const draft = dictionaryDrafts.get(item.id);
  const dictionary = draft || item.dictionary || { description: "", fields: [], metrics: [] };
  $("dictionaryDescription").value = dictionary.description || "";
  const fields = $("fieldDefinitions"); fields.replaceChildren();
  item.tables.forEach((table, index) => {
    const details = document.createElement("details"); details.className = "field-definitions"; details.open = index === 0;
    const summary = document.createElement("summary"); summary.textContent = `${table.label} · ${table.columns.length} 个字段`;
    details.append(summary);
    table.columns.forEach((column, columnIndex) => {
      const row = document.createElement("div"); row.className = "field-definition";
      const input = document.createElement("textarea"); input.id = `field-definition-${index}-${columnIndex}`; input.rows = 1; input.maxLength = 500; input.dataset.table = table.name; input.dataset.column = column.name;
      input.value = (dictionary.fields || []).find((field) => field.table === table.name && field.column === column.name)?.description || "";
      input.placeholder = "例如：退款后的实付金额，单位：元";
      const label = document.createElement("label"); label.htmlFor = input.id; label.textContent = column.name;
      const type = document.createElement("small"); type.textContent = column.type; label.append(type);
      row.append(label, input); details.append(row);
    });
    fields.append(details);
  });
  $("metricDefinitions").replaceChildren();
  (dictionary.metrics || []).forEach(addMetricRow);
  $("dictionaryStatus").textContent = draft ? "有未保存的修改" : item.dictionary_version ? `已保存 · 第 ${item.dictionary_version} 版` : "可选填写";
  $("dictionaryStatus").className = `definition-status${draft ? " dirty" : ""}`;
  $("saveDictionaryBtn").disabled = !draft;
}

async function saveDictionary(event) {
  event.preventDefault();
  const itemId = selectedId;
  const dictionary = dictionaryFromForm();
  if (dictionary.metrics.some((metric) => !metric.name.trim() || !metric.definition.trim())) return notice("请补全指标名称和计算口径，或删除未填写的指标。", true);
  const controls = [...$("dictionaryForm").querySelectorAll("input, textarea, button")];
  controls.forEach((control) => { control.disabled = true; });
  try {
    const saved = await api(`/api/datasets/${itemId}/dictionary`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(dictionary) });
    dictionaryDrafts.delete(itemId);
    if (selectedId === itemId) {
      Object.assign(selectedItem, saved); dictionaryViewKey = ""; renderDictionary(selectedItem);
      notice("字段说明与指标口径已保存，将用于下一次分析。已有看板请重新生成。");
      loadDashboard(itemId);
    }
  } catch (error) { notice(error.message, true); }
  finally {
    controls.forEach((control) => { control.disabled = false; });
    $("saveDictionaryBtn").disabled = !dictionaryDrafts.has(selectedId);
  }
}

async function generateProfile() {
  const itemId = selectedId;
  $("profileBtn").disabled = true;
  try {
    await api(`/api/datasets/${itemId}/profile`, { method: "POST" });
    if (selectedId === itemId) { selectedItem.profile_status = "queued"; qualityViewKey = ""; renderQualityAndDictionary(selectedItem); }
  } catch (error) { notice(error.message, true); $("profileBtn").disabled = false; }
}
