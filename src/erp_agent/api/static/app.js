"use strict";
const $ = id => document.getElementById(id);
const labels = {
  initial:"初始条件", certification:"资格认证", market:"市场", product:"产品", bom:"产品用料",
  material:"原材料", production_line:"生产线", worker:"人员", training:"培训", incentive:"激励",
  shift:"班次", finance:"融资", discount:"贴现", demand_forecast:"市场调研", order_contract:"订单",
  carbon:"碳排放", digital:"数智化", budget:"预算", other:"其他"
};
const stateNames = {known:"原文已确认", assumed:"暂定 / 默认", conflicted:"存在疑问", missing:"未提供"};
const fieldNames = {initial_cash:"初始现金",years:"经营年数",periods_per_year:"每年期数",
  management_fee:"管理费",monthly_salary:"月薪",efficiency:"效率",piece_rate:"计件工资",
  capacity:"基础产能",production_cycle:"生产周期",delivery_period:"送货期",
  payment_period:"账期",interest_rate:"利率",startup_cost:"单件开产费",
  repayment_method:"还款方式",discount_interest:"贴息比例",loan_period:"贷款期限",
  loan_duration:"贷款期限",collection_period:"收款期",discount_period:"贴现期"};
const statusNames = {queued:"排队",running:"执行中",succeeded:"完成",failed:"失败",cancelled:"已取消",needs_input:"需要复核"};
const stageNames = {queued:"排队",starting:"开始",parsing:"解析文件",saving_document:"保存文件",reading:"整理题目",repairing_reading:"修复读题格式",repairing_strategy:"修复方案格式",validating:"校验来源",saving_reading:"保存读题",proposing:"生成策略",validating_plan:"校验计划",searching:"搜索候选",validating_best:"复验方案",saving:"保存结果",done:"已完成",cancelling:"等待取消"};
let projects = [], projectId = "", snapshot = null, editable = null, status = null;
let inputDraft={revision:0,problem_text:"",supplement_text:"",annotations:[]},inputsDirty=false,inputEditSequence=0;
let documents = [], jobs = [], selectedFiles = new Set(), knownFiles = new Set();
let authenticated = false, dirty = false, polling = false, generation = 0, currentStep = 1;
let pendingDelete = null, latestReadSeen = 0, documentsSignature = "", jobsSignature = "", solutionsSignature = "";
let toastTimer;
let pendingNavigation = null;
let editingFact = null, editingEvidence = new Set();
let modelProviders = [];
let modelCatalog = null, selectedPresetId = "", presetDirty = false;
const providerHelp = {
  openai_chat: "适用于 OpenAI、DeepSeek、通义等兼容服务。填写基础地址（如 /v1）或完整 Chat Completions 地址。",
  openai_responses: "使用 OpenAI Responses 格式，可填写 /v1 基础地址或完整 /responses 地址。",
  anthropic: "使用 Anthropic Messages 格式。基础地址自动补全 /v1/messages；也可填写完整端点。兼容服务可选 JSON 对象或提示词约束 JSON。",
  gemini: "使用 Gemini 原生格式，填写 /v1beta 基础地址或完整 generateContent 地址。",
  ollama: "使用 Ollama 原生 /api/chat，本机服务通常为 http://localhost:11434。"
};
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = !text;
  if (text) toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6000);
}
function showLogin() {
  authenticated = false;
  generation++;
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  $("evidence-drawer").hidden = true;
}
async function api(path, options = {}) {
  const response = await fetch(path, {...options, credentials:"same-origin"});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && data.error?.code === "authentication_required") showLogin();
    const error = new Error(data.error?.message || `请求失败 ${response.status}`);
    error.code = data.error?.code;
    throw error;
  }
  return data;
}
const post = (path, data) => api(path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
const base = (id = projectId) => `/api/projects/${id}`;
async function safe(operation) { try { await operation(); } catch (error) { toast(error.message); } }
async function busy(button, operation) {
  button.disabled = true;
  try { await operation(); } catch (error) { toast(error.message); }
  finally { button.disabled = false; updateControls(); if(modelCatalog)updatePresetControls(); }
}
function markDirty() {
  dirty = true;
  $("dirty-note").textContent = "有未保存修改";
  $("reading-json").value = JSON.stringify(editable, null, 2);
  updateControls();
}
function navigate(operation) {
  if ((dirty || inputsDirty) && projectId) {
    pendingNavigation = operation;
    $("discard-dialog").showModal();
  } else safe(operation);
}
function setStep(number) {
  currentStep = number;
  for (const button of document.querySelectorAll("[data-step]")) {
    const step = Number(button.dataset.step);
    button.classList.toggle("active", step === number);
    const ready=reviewReadiness();
    button.classList.toggle("complete", (step === 1 && documents.length > 0) || (step === 2 && !!snapshot && !dirty && !ready.errors && ready.horizon));
    if (step === number) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".step-panel")) panel.hidden = panel.id !== "step-" + number;
  updateControls();
}
function reviewTab(name) {
  for (const button of document.querySelectorAll("[data-review]")) button.classList.toggle("active", button.dataset.review === name);
  for (const tab of ["facts", "rules", "issues"]) $(tab + "-panel").hidden = tab !== name;
}
function activeRead() { return jobs.some(job => job.kind === "read" && ["queued","running"].includes(job.status)); }
function mode() { return document.querySelector('input[name="solver-mode"]:checked').value; }
function updateControls() {
  const mc = mode() === "llm_monte_carlo";
  const canRead = authenticated && !!projectId && (selectedFiles.size > 0 || inputDraft.problem_text.trim()) && status?.llm_configured && !activeRead();
  $("read").disabled = !canRead;
  $("reread").disabled = !canRead || dirty;
  $("read").textContent = activeRead() ? "正在读题…" : "读取题目 →";
  $("model-required").hidden = !!status?.llm_configured;
  $("selected-count").textContent = `已选择 ${selectedFiles.size} 个文件${inputDraft.problem_text.trim()?" · 含手动题目":""}`;
  $("save-inputs").disabled=!inputsDirty;
  $("input-save-status").textContent=inputsDirty?"输入待保存，读取时会自动保存":"输入已保存";
  if(inputsDirty)$("selected-count").textContent+=" · 将先保存输入";
  $("search-config").hidden = !mc;
  const readiness = reviewReadiness();
  const readingErrors = readiness.errors > 0;
  const horizonMissing = !readiness.horizon;
  const solving = jobs.some(job=>job.kind==="solve" && ["queued","running"].includes(job.status));
  const inputsChanged=inputsDirty || (snapshot && inputDraft.revision!==(snapshot.inputs?.revision || 0));
  $("solve").disabled = !snapshot || dirty || inputsChanged || readingErrors || horizonMissing || solving || !status?.llm_configured || (mc && !status?.simulation_ready);
  $("save-revision").disabled = !snapshot || !dirty;
  $("next-solve").disabled = !snapshot || (!dirty && (readingErrors || horizonMissing));
  $("next-solve").textContent = dirty ? "保存并继续 →" : readingErrors ? "待修正错误" : horizonMissing ? "先确认经营周期" : "继续生成方案 →";
  $("solve").textContent = solving ? "正在生成…" : "生成方案草稿";
  $("solver-note").textContent = inputsChanged ? "题目输入或文件说明已修改，请返回上传步骤并重新读取。" : horizonMissing ? "请返回确认题目，填写经营年数和每年期数。" : readingErrors ? "题目还有错误，请修正并保存。" : mc
    ? (status?.simulation_ready ? "在仿真器中搜索并比较候选方案。" : "经营仿真内核尚未就绪，暂不能使用蒙特卡罗。")
    : "当前输出为方案草稿，产能、现金与履约尚未经过经营仿真验证。";
  $("solve-context").textContent = snapshot ? `${snapshot.reading.problem.years || "待确认"} 年 · 每年 ${snapshot.reading.problem.periods_per_year || "待确认"} 期` : "请先确认题目";
  $("step1-caption").textContent = documents.length ? `${documents.length} 个文件` : "添加赛题与补充文件";
  $("step2-caption").textContent = snapshot ? (dirty ? "修改待保存" : readiness.errors ? readiness.errors + " 项待修正" : horizonMissing ? "周期待填写" : "可生成草稿") : "处理疑问，核对数据";
  if(snapshot)renderReviewGuide(readiness);
}
async function loadStatus() {
  const [currentStatus, catalog] = await Promise.all([api("/api/status"), api("/api/model-presets")]);
  status = currentStatus;
  modelCatalog = catalog;
  renderQuickPresets();
  $("model-dot").classList.toggle("ready", status.llm_configured);
  $("upload-limit").textContent = `单文件最大 ${status.max_upload_mb} MB`;
  updateControls();
}
function renderQuickPresets() {
  $("active-model-preset").replaceChildren();
  for (const preset of modelCatalog.presets) {
    const option=node("option",preset.name);
    option.value=preset.id;
    option.disabled=!preset.configured && preset.id!==modelCatalog.active_id;
    $("active-model-preset").append(option);
  }
  $("active-model-preset").value=modelCatalog.active_id;
}
async function enterApp() {
  authenticated = true;
  $("access-key").value = "";
  $("login-error").hidden = true;
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  await loadStatus();
  document.querySelector(`input[name="solver-mode"][value="${status.default_solver}"]`).checked = true;
  await showProjects();
}
async function showProjects() {
  const captured = ++generation;
  projectId = "";
  dirty = false;
  $("project-view").hidden = false;
  $("flow-view").hidden = true;
  $("evidence-drawer").hidden = true;
  const received = await api("/api/projects");
  if (captured !== generation) return;
  projects = received;
  $("project-list").replaceChildren();
  $("no-projects").hidden = projects.length > 0;
  for (const project of [...projects].reverse()) {
    const card = node("article", undefined, "project-card");
    card.append(node("div", "▤", "project-symbol"), node("h2", project.name));
    card.append(node("p", `创建于 ${new Date(project.created_at).toLocaleDateString("zh-CN")}`, "muted"));
    const actions = node("div", undefined, "card-actions");
    const open = node("button", "打开项目 →", "text-button");
    open.onclick = () => safe(() => openProject(project));
    const remove = node("button", "删除", "danger-text");
    remove.onclick = () => askDelete(project);
    actions.append(open, remove); card.append(actions); $("project-list").append(card);
  }
}
async function openProject(project) {
  generation++;
  projectId = project.id; snapshot = null; editable = null; dirty = false;
  documents = []; jobs = []; selectedFiles = new Set(); knownFiles = new Set(); latestReadSeen = 0;
  documentsSignature = ""; jobsSignature = ""; solutionsSignature = "";
  inputDraft={revision:0,problem_text:"",supplement_text:"",annotations:[]};inputsDirty=false;inputEditSequence=0;
  $("problem-text").value="";$("supplement-text").value="";$("manual-entry").open=false;
  $("project-title").textContent = project.name;
  $("project-subtitle").textContent = `创建于 ${new Date(project.created_at).toLocaleDateString("zh-CN")}`;
  $("project-view").hidden = true; $("flow-view").hidden = false;
  $("reading-empty").hidden = false; $("review").hidden = true;
  $("documents").replaceChildren(); $("source-content").replaceChildren(); $("solutions").replaceChildren();
  $("jobs").replaceChildren(); $("active-job").hidden = true;
  $("solutions-empty").hidden=false;
  updateFutureModules([]);
  $("fact-search").value = ""; $("state-filter").value = ""; $("issue-filter").value="";
  $("evidence-drawer").hidden = true;
  setStep(1);
  const captured=generation;
  const inputs=await api(base()+"/inputs");if(captured!==generation)return;
  inputDraft=inputs;$("problem-text").value=inputs.problem_text;$("supplement-text").value=inputs.supplement_text;
  $("manual-entry").open=!!inputs.problem_text;
  await refresh(true);
  if (snapshot) setStep(2);
}
function askDelete(project) {
  pendingDelete = project;
  $("delete-name").textContent = project.name;
  $("delete-dialog").showModal();
}
function renderDocuments() {
  $("documents").replaceChildren(); $("source-content").replaceChildren();
  for (const document of documents) {
    const id = document.source.id;
    if (!knownFiles.has(id)) { knownFiles.add(id); selectedFiles.add(id); }
    const entry=node("div",undefined,"file-entry"),row = node("div", undefined, "file-row");
    const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selectedFiles.has(id);
    checkbox.onchange = () => { if (checkbox.checked) selectedFiles.add(id); else selectedFiles.delete(id); updateControls(); };
    const info = node("span", document.source.filename, "file-info");
    info.append(node("small", `${Math.ceil(document.source.size / 1024)} KB · ${document.evidence.length} 处证据`));
    row.append(checkbox, info, node("span", "已解析", "badge known"));
    const note=node("label",undefined,"file-note"),text=node("textarea");text.rows=2;text.maxLength=5000;
    text.value=inputDraft.annotations.find(a=>a.document_id===id)?.text || "";
    text.placeholder="文件说明（选填），例如：订单数量不得超过此详单。";
    text.setAttribute("aria-label",`${document.source.filename}的文件说明`);
    text.oninput=()=>{inputDraft.annotations=inputDraft.annotations.filter(a=>a.document_id!==id);if(text.value.trim())inputDraft.annotations.push({document_id:id,text:text.value});markInputsDirty();};
    note.append(node("span","说明"),text);entry.append(row,note);$("documents").append(entry);
    const details = node("details", undefined, "source-document");
    details.append(node("summary", document.source.filename));
    for (const warning of document.warnings) details.append(node("p", warning, "notice"));
    for (const block of document.blocks) {
      if (block.kind === "paragraph" && block.text) details.append(node("p", block.text));
      if (block.kind === "table") {
        const wrapper = node("div", undefined, "table-wrap"), table = node("table", undefined, "source-table");
        const rows = new Map();
        for (const cell of block.cells) {
          if (!rows.has(cell.row)) rows.set(cell.row, node("tr"));
          const td = node("td", cell.text || "—", cell.vertical_merge === "continue" ? "continuation" : "");
          td.colSpan = cell.column_span; td.title = cell.evidence_id; rows.get(cell.row).append(td);
        }
        for (const row of rows.values()) table.append(row);
        wrapper.append(table); details.append(wrapper);
      }
    }
    $("source-content").append(details);
  }
  updateControls();
}
function evidenceIndex() {
  return new Map([...documents,...(snapshot?.additional_sources || [])].flatMap(doc=>doc.evidence.map(e=>[e.id,{...e,filename:doc.source.filename}])));
}
function evidenceContextText(evidence, index) {
  const context=evidence.table_context;
  if(!context)return "";
  const text=ids=>ids.map(id=>index.get(id)?.text).filter(Boolean).join(" / ");
  return [text(context.heading_ids),text(context.column_header_ids),text(context.row_ids)].filter(Boolean).join(" · ");
}
function appendEvidenceTable(item, evidence, index) {
  const context=evidence.table_context;
  if(!context)return;
  const document=documents.find(doc=>doc.source.id===evidence.document_id);
  const block=document?.blocks.find(block=>block.index===context.block_index);
  if(!block)return;
  const headerRows=new Map();
  for(const id of context.row_ids){
    for(const headerId of index.get(id)?.table_context?.column_header_ids || []){
      const header=index.get(headerId),ctx=header?.table_context;
      if(ctx)headerRows.set(`${ctx.block_index}:${ctx.row}`,{block:ctx.block_index,row:ctx.row});
    }
  }
  const wrapper=node("div",undefined,"table-wrap evidence-table-wrap"),table=node("table",undefined,"source-table evidence-table");
  const rows=[...headerRows.values()].sort((a,b)=>a.block-b.block || a.row-b.row);
  if(!headerRows.has(`${context.block_index}:${context.row}`))rows.push({block:context.block_index,row:context.row});
  for(const row of rows){
    const tr=node("tr");
    const sourceBlock=document.blocks.find(b=>b.index===row.block);
    for(const cell of sourceBlock?.cells.filter(c=>c.row===row.row) || []){
      const td=node(headerRows.has(`${row.block}:${row.row}`)?"th":"td",cell.text || (cell.vertical_merge==="continue"?"（合并续行）":"—"));
      td.colSpan=cell.column_span;
      if(cell.evidence_id===evidence.id){td.classList.add("evidence-selected");td.title="当前引用单元格";}
      tr.append(td);
    }
    table.append(tr);
  }
  wrapper.append(table);item.append(wrapper,node("small",rows.some(row=>row.block!==context.block_index)?"续表沿用上表标题；保留原列序，横向滚动可查看完整行。":"表格保留原列序；横向滚动可查看完整行。","muted"));
}
function showEvidence(ids, title = "原文证据", fact = null) {
  $("evidence-content").replaceChildren(node("h3", title));
  if(fact){
    const conversion={percent_to_ratio:"百分数转比例：原值 ÷ 100",wan_to_yuan:"万元转元：原值 × 10000",identity:"按原值保存"};
    $("evidence-content").append(node("p",`当前字段：${fact.value ?? "未提供"} ${fact.unit} · ${conversion[fact.conversion] || ""}`,"notice"));
  }
  const index = evidenceIndex();
  if (!ids.length) $("evidence-content").append(node("p", "未关联原文证据。", "muted"));
  for (const id of ids) {
    const evidence = index.get(id), item = node("div", undefined, "evidence-item");
    item.append(node("small", evidence ? `${evidence.filename} · ${evidence.location}` : "证据不存在"));
    if(evidence?.table_context){
      const contextText=evidenceContextText(evidence,index);
      const heading=evidence.table_context.heading_ids.map(id=>index.get(id)?.text).filter(Boolean).join(" / ");
      if(heading)item.append(node("h4",heading));
      const headers=evidence.table_context.column_header_ids.map(id=>index.get(id)?.text).filter(Boolean).join(" / ");
      if(headers)item.append(node("p",`列标题：${headers}`,"evidence-column-title"));
      item.append(node("p",`引用原值：${evidence.text || "（空白或合并单元格）"}`));
      appendEvidenceTable(item,evidence,index);
      if(contextText)item.title=contextText;
    }else item.append(node("p", evidence?.text || "（空白或合并单元格）"));
    const details=node("details");details.append(node("summary","来源编号"),node("code",id));item.append(details);
    $("evidence-content").append(item);
  }
  $("evidence-drawer").hidden = false;
}
function renderReading(data) {
  snapshot = data.snapshot;
  editable = {title:snapshot.reading.problem.title, facts:structuredClone(snapshot.reading.problem.facts),
    rules:structuredClone(snapshot.reading.rules.rules), issues:snapshot.reading.issues.filter(issue => !issue.code.startsWith("validation."))};
  dirty = false;
  $("reading-empty").hidden = true; $("review").hidden = false;
  $("reading-model").textContent = `${snapshot.reading.problem.title} · ${documents.length} 个文件`;
  $("metric-facts").textContent = editable.facts.length;
  $("metric-errors").textContent = snapshot.reading.issues.filter(i => i.severity === "error").length;
  $("metric-pending").textContent = reviewIssues().filter(i => i.severity !== "info").length;
  $("metric-rules").textContent = editable.rules.length;
  $("issue-count").textContent = snapshot.reading.issues.filter(i => i.severity !== "info").length;
  $("category-filter").replaceChildren(node("option", "全部分类"));
  $("category-filter").firstChild.value = "";
  for (const category of new Set(editable.facts.map(f => f.category))) { const option=node("option",labels[category] || category); option.value=category; $("category-filter").append(option); }
  $("reading-json").value = JSON.stringify(editable, null, 2);
  $("dirty-note").textContent = "修改后请保存";
  $("issue-filter").value = "";
  renderFacts(); renderRules(); renderIssues(); updateControls(); setStep(currentStep);
  reviewTab(reviewIssues().some(i=>i.severity!=="info") || !reviewReadiness().horizon ? "issues" : "facts");
}
function renderFacts() {
  $("facts").replaceChildren();
  if (!editable) return;
  const search = $("fact-search").value.trim().toLowerCase();
  const category = $("category-filter").value, state = $("state-filter").value;
  const visible = editable.facts.map((fact,index) => ({fact,index})).filter(({fact}) =>
    (!category || fact.category === category) && (!state || fact.state === state) &&
    (!search || `${fact.id} ${fact.entity_name} ${fact.key} ${fieldNames[fact.key]||""} ${fact.value ?? ""}`.toLowerCase().includes(search)));
  const groups = new Map();
  for (const item of visible) { if (!groups.has(item.fact.category)) groups.set(item.fact.category, []); groups.get(item.fact.category).push(item); }
  for (const [category, items] of groups) {
    const group = node("tr", undefined, "group-row"), cell = node("td", `${labels[category] || category} · ${items.length}`); cell.colSpan=5; group.append(cell); $("facts").append(group);
    for (const {fact} of items) {
      const tr = node("tr"), name = node("td"); tr.id=`fact-row-${fact.id}`; name.append(node("strong",fact.entity_name),node("small",fieldNames[fact.key] || fact.key));
      if (fact.explanation) name.append(node("small", fact.explanation));
      const valueCell=node("td"), value=node("input"); value.value=fact.value ?? ""; value.placeholder="未提供"; value.setAttribute("aria-label",`${fact.entity_name} ${fact.key}的值`);
      value.onchange=()=>{ fact.value=value.value === "" ? null : value.value; if(fact.value===null) {fact.state="missing";stateSelect.value="missing";} tr.classList.add("changed"); markDirty(); };
      valueCell.append(value);
      const stateCell=node("td"), stateSelect=node("select"); stateSelect.setAttribute("aria-label",`${fact.key}的状态`);
      for(const [key,label] of Object.entries(stateNames)){const option=node("option",label);option.value=key;stateSelect.append(option);} stateSelect.value=fact.state;
      stateSelect.onchange=()=>{fact.state=stateSelect.value;tr.classList.add("changed");markDirty();};stateCell.append(stateSelect);
      const sourceCell=node("td"), sourceButton=node("button",fact.evidence_ids.length ? `${fact.evidence_ids.length} 处` : "无来源","source-button");
      sourceButton.onclick=()=>showEvidence(fact.evidence_ids,`${fact.entity_name} · ${fieldNames[fact.key] || fact.key}`,fact);const editButton=node("button","确认 / 修改","text-button");editButton.onclick=()=>openFactEditor(fact.id);sourceCell.append(sourceButton,editButton);
      const unitCell = node("td"), unit = node("input");
      unit.value = fact.unit; unit.setAttribute("aria-label", `${fact.key}的单位`);
      unit.onchange = () => { fact.unit = unit.value; tr.classList.add("changed"); markDirty(); };
      unitCell.append(unit);
      tr.append(name,valueCell,unitCell,stateCell,sourceCell);$("facts").append(tr);
    }
  }
  $("no-facts").hidden = visible.length > 0;
}
function renderRules() {
  $("rules").replaceChildren();
  if (!editable.rules.length) $("rules").append(node("p","没有提取到独立规则。请核对原文中的公式、付款时点、取整和约束；可补充规则或添加文件重新读题。","empty"));
  for (const rule of editable.rules) {
    const item=node("div",undefined,"rule-item"), title=node("div",undefined,"rule-title");item.id=`rule-row-${rule.id}`;
    const state = node("select"); state.setAttribute("aria-label", `${rule.name}的状态`);
    for (const [value,label] of Object.entries(stateNames)) { const option = node("option",label); option.value=value; state.append(option); }
    state.value=rule.state; state.onchange=()=>{rule.state=state.value;markDirty();};
    const name=node("input");name.value=rule.name;name.setAttribute("aria-label","规则名称");name.onchange=()=>{rule.name=name.value;markDirty();};title.append(name,state);
    const source=node("button","查看来源","source-button");source.onclick=()=>showEvidence(rule.evidence_ids,rule.name);title.append(source);
    const expression=node("textarea"); expression.value=rule.expression; expression.rows=2;
    expression.setAttribute("aria-label", `${rule.name}的公式或限制`);
    expression.onchange=()=>{rule.expression=expression.value;rule.state="assumed";state.value="assumed";markDirty();};
    const explanation=node("textarea"); explanation.value=rule.explanation; explanation.rows=2;
    explanation.placeholder="规则口径与适用范围"; explanation.setAttribute("aria-label",`${rule.name}的说明`);
    explanation.onchange=()=>{rule.explanation=explanation.value;markDirty();};
    const confirm=node("button","确认规则","secondary");
    confirm.onclick=()=>{
      if(!rule.expression.trim()){toast("请先填写规则内容。");return;}
      rule.explanation=rule.explanation.trim() || "默认";
      if(rule.state!=="known")rule.state="assumed";
      state.value=rule.state;
      markDirty();confirm.textContent="已确认";toast("规则已确认，保存后生效。");
    };
    item.append(title,expression,explanation,confirm);$("rules").append(item);
  }
}
function reviewIssues() {
  if(!snapshot)return [];
  return [...snapshot.reading.issues.filter(i=>i.code.startsWith("validation.")), ...editable.issues]
    .filter(i=>i.code!=="validation.rule_unimplemented" && !i.code.startsWith("review.resolved."));
}
function reviewReadiness() {
  const issues=snapshot?.reading.issues || [];
  const problem=snapshot?.reading.problem;
  return {errors:issues.filter(i=>i.severity==="error").length,
    warnings:reviewIssues().filter(i=>i.severity==="warning").length,
    horizon:!!(problem?.years && problem?.periods_per_year)};
}
function renderReviewGuide(ready) {
  const title=$("review-status-title"), badge=$("review-status-badge");
  title.textContent=dirty ? "修改待校验" : ready.errors ? `先处理 ${ready.errors} 项错误` : !ready.horizon ? "请确认经营周期" : "可以生成方案草稿";
  badge.textContent=dirty ? "待保存" : ready.errors || !ready.horizon ? "待处理" : "结构校验通过";
  badge.className=`badge ${dirty || ready.errors || !ready.horizon ? "conflicted" : "known"}`;
  $("review-status-detail").textContent=dirty ? "已应用的修改尚未保存。保存后更新校验结果和是否可以继续。" : ready.errors ? "错误会阻止生成方案；警告可以在明确假设后保留。" : !ready.horizon ? "缺少经营年数或每年期数，无法生成覆盖完整周期的计划。" : `${ready.warnings} 项提示仍需复核；方案为草稿，经营数值尚未验证。`;
  $("review-issues-summary").textContent=`${ready.errors} 项错误 · ${ready.warnings} 项提示`;
  $("review-rules-summary").textContent=`${editable.facts.length} 个字段 · ${editable.rules.length} 条规则`;
  $("metric-facts").textContent=editable.facts.length;
  $("metric-pending").textContent=reviewIssues().filter(i=>i.severity!=="info").length;
  $("metric-rules").textContent=editable.rules.length;
  $("dirty-note").textContent=dirty ? "有未保存修改，请保存并校验" : ready.errors ? "校验未通过：先修正错误，再保存校验" : !ready.horizon ? "请补充周期，再保存校验" : "可以生成草稿；暂定内容会随方案保留";
}
function issueAdvice(issue) {
  if(issue.code==="validation.unsupported_value")return issueFact(issue)?.value_type==="text"
    ? "核对文字内容与对应原文。确认后选择来源；暂定解释可改为假设，并在下方说明。"
    : "核对数据格与适用表头。单位不明可留空；百分比或倍率转换须有依据。暂定值可改为假设，并在下方说明。";
  if(issue.code==="validation.source_missing")return "关联的原文依据无效或缺失。请选择正确来源；无依据的暂定内容标记为假设。";
  if(issue.code==="validation.rule_source")return "查看规则原文依据。无依据时改为假设并说明口径；补充来源可在高级结构化数据中修改 evidence_ids。";
  if(issue.code==="validation.duplicate_field" || issue.code==="validation.duplicate_id")return "合并重复字段及其来源，不同对象应使用不同编号；可在高级结构化数据中处理。";
  if(issue.code==="validation.uncertain")return "对照原文确认含义。已核实可补来源标为已知；无法确定则保留假设或冲突并说明。";
  if(issue.code==="validation.value_missing")return "在字段编辑中补充已查到的值；未查到时保留缺失，不要用零替代。";
  if(issue.code==="validation.horizon_missing" || /PERIOD|horizon|simulation_period/.test(issue.code+issue.target))return "在待确认项填写经营年数和每年期数，说明可留空，保存后使用。";
  return "对照原文核对相关字段或规则。处理模型提示后记录依据；系统错误须重新校验。";
}
function renderIssues() {
  $("issues").replaceChildren();
  const filter=$("issue-filter").value;
  const issues=reviewIssues().filter(i=>i.code!=="validation.horizon_missing" && i.severity!=="info" && (!filter || i.severity===filter))
    .sort((a,b)=>(a.severity==="error"?0:1)-(b.severity==="error"?0:1));
  $("issue-count").textContent=reviewIssues().filter(i=>i.severity!=="info").length;
  $("issue-guide").textContent=dirty ? "修改待保存，保存后将重新检查" : "错误必须修正；提示需明确依据或假设";
  renderCycleIssues(filter);
  for(const issue of issues) {
    if(isCycleIssue(issue))continue;
    const fact=issueFact(issue),rule=editable.rules.find(r=>r.id===issue.target);
    const item=node("article",undefined,`issue-item ${issue.severity}`);
    const heading=node("div",undefined,"issue-heading");
    heading.append(node("span",issue.severity==="error"?"阻止生成":"需核对","badge "+(issue.severity==="error"?"conflicted":"assumed")),node("strong",fact?`${fact.entity_name} · ${fieldNames[fact.key]||fact.key}`:rule?.name || (issue.target==="horizon"?"经营周期":"题目提示")));
    item.append(heading);
    if(fact)item.append(node("p",`当前${fact.value_type==="text"?"文字":"数值"}：${fact.value ?? "未提供"}${fact.unit?" · 单位："+fact.unit:" · 无单位"}`,"issue-value"));
    item.append(node("p",issue.message),node("p",issueAdvice(issue),"muted"));
    const note=node("label",undefined,"issue-explanation"),explanation=node("textarea");
    note.append(node("span","处理说明（选填）"));explanation.rows=2;
    explanation.placeholder="填写你的理解、暂定口径或仍不清楚的地方；留空为默认";
    explanation.setAttribute("aria-label",`${fact?.entity_name || rule?.name || "题目提示"}处理说明`);
    explanation.value=fact?.explanation || editable.issues.find(i=>i.code==="review.note."+issue.code && i.target===issue.target)?.message || "";
    explanation.oninput=()=>{
      if(fact)fact.explanation=explanation.value;
      else {
        editable.issues=editable.issues.filter(i=>!(i.code==="review.note."+issue.code && i.target===issue.target));
        if(explanation.value.trim())editable.issues.push({code:"review.note."+issue.code,severity:"info",target:issue.target,message:explanation.value.trim(),evidence_ids:issue.evidence_ids});
      }
      markDirty();
    };
    note.append(explanation);item.append(note);
    const actions=node("div",undefined,"actions");
    if(fact){const edit=node("button","确认 / 修改","secondary");edit.onclick=()=>openFactEditor(fact.id);actions.append(edit);}
    else if(rule){const edit=node("button","修正规则","secondary");edit.onclick=()=>{reviewTab("rules");$("rule-row-"+rule.id)?.scrollIntoView({block:"center"});};actions.append(edit);}
    if(issue.evidence_ids.length){const source=node("button","查看原文","source-button");source.onclick=()=>showEvidence(issue.evidence_ids,fact?`${fact.entity_name} · ${fieldNames[fact.key] || fact.key}`:"原文证据",fact);actions.append(source);}
    if(!issue.code.startsWith("validation.")){const resolve=node("button","确认处理","secondary");resolve.onclick=()=>{resolveModelIssue(issue,explanation.value.trim() || "默认");markDirty();renderIssues();toast("处理说明已记录，保存后生效。");};actions.append(resolve);}
    const unitIssue=!issue.code.startsWith("validation.") && /unit|单位/i.test(issue.code+issue.message);
    if(!issue.code.startsWith("validation.") || fact){
      for(const choice of ["无","不清楚"]){
        const button=node("button",choice,"annotation-button");
        button.onclick=()=>annotateIssue(issue,choice,unitIssue,explanation.value.trim());actions.append(button);
      }
    }
    item.append(actions);$("issues").append(item);
  }
  if(!$("issues").children.length)$("issues").append(node("p",filter?"没有此类问题。":"没有待处理的问题。可继续核对经营周期、数据和规则。","empty"));
}
function openFactEditor(id) {
  editingFact=editable.facts.find(f=>f.id===id);
  if(!editingFact)return;
  const fact=editingFact;
  editingEvidence=new Set(fact.evidence_ids);
  $("fact-editor-title").textContent=`${fact.entity_name} · ${fieldNames[fact.key]||fact.key}`;
  $("edit-fact-value").value=fact.value ?? "";$("edit-fact-unit").value=fact.unit;
  $("edit-fact-type").value=fact.value_type;$("edit-fact-state").value=fact.state;
  $("edit-fact-conversion").value=fact.conversion;$("edit-fact-explanation").value=fact.explanation;
  const issues=reviewIssues().filter(i=>i.target===id);
  $("fact-editor-issue").textContent=issues.map(i=>i.message).join("；");$("fact-editor-issue").hidden=!issues.length;
  $("fact-editor-error").hidden=true;$("fact-evidence-search").value="";
  renderEvidencePicker();$("fact-dialog").showModal();
}
function renderEvidencePicker() {
  const search=$("fact-evidence-search").value.trim().toLowerCase();
  const index=evidenceIndex(), evidence=[...index.values()];
  const selected=evidence.filter(e=>editingEvidence.has(e.id));
  const others=evidence.filter(e=>!editingEvidence.has(e.id) && search && `${e.text} ${e.location} ${evidenceContextText(e,index)}`.toLowerCase().includes(search)).slice(0,40);
  $("fact-evidence-list").replaceChildren();
  for(const e of [...selected,...others]) {
    const label=node("label",undefined,"evidence-choice"),check=node("input");check.type="checkbox";check.checked=editingEvidence.has(e.id);
    check.onchange=()=>{if(check.checked)editingEvidence.add(e.id);else editingEvidence.delete(e.id);};
    const body=node("span");body.append(node("small",`${e.filename} · ${e.location}`));
    const context=evidenceContextText(e,index);if(context)body.append(node("small",context));
    body.append(node("span",`原值：${e.text || "（空白单元格）"}`));label.append(check,body);$("fact-evidence-list").append(label);
  }
  if(!selected.length && !others.length)$("fact-evidence-list").append(node("p",search?"没有匹配的原文。换一个关键词搜索。":"暂无已选来源。输入关键词查找原文。","muted"));
}
function isCycleIssue(issue) {
  if(issue.severity==="error")return false;
  const fact=issueFact(issue);
  if(fact?.category==="initial" && ["years","periods_per_year"].includes(fact.key))return true;
  return /missing_periods|horizon|simulation_period/i.test(issue.code+issue.target)
    || ["years","periods_per_year"].includes(issue.target.split(".").pop());
}
function issueFact(issue) {
  const key=issue.target.split(".").pop();
  return editable.facts.find(f=>f.id===issue.target) || editable.facts.find(f=>f.key===key);
}
function setCycleField(key,value) {
  let fact=editable.facts.find(f=>f.category==="initial" && f.key===key);
  if(!fact){
    let id=`review_${key}`;while(editable.facts.some(f=>f.id===id))id+="_";
    fact={id,category:"initial",entity_id:"company",entity_name:"公司",key,evidence_ids:[]};editable.facts.push(fact);
  }
  if(fact.value!==value || fact.value_type!=="integer" || !["known","assumed"].includes(fact.state)){
    Object.assign(fact,{value:value || null,value_type:"integer",unit:key==="years"?"年":"期/年",
      state:value?"assumed":"missing",conversion:"identity",explanation:"默认",evidence_ids:[]});
    markDirty();renderFacts();
  }
}
function fieldConfirmation(fact) {
  return `用户已确认：${fieldNames[fact.key] || fact.key}=${fact.value ?? "未提供"} ${fact.unit}；说明：${fact.explanation || "默认"}`;
}
function recordFieldConfirmation(fact) {
  editable.issues=editable.issues.filter(i=>!(i.code==="review.confirmed" && i.target===fact.id));
  editable.issues.push({code:"review.confirmed",severity:"info",target:fact.id,
    message:fieldConfirmation(fact),evidence_ids:fact.evidence_ids});
}
function renderCycleIssues(filter) {
  if(filter==="error")return;
  for(const key of ["years","periods_per_year"]){
    const field=editable.facts.find(f=>f.category==="initial" && f.key===key);
    if(field?.value && ["known","assumed"].includes(field.state)
      && Number.isInteger(Number(field.value)) && Number(field.value)>=1 && Number(field.value)<=100)continue;
    const item=node("article",undefined,"issue-item cycle-item");
    const heading=node("div",undefined,"issue-heading");
    const confirmed=field && editable.issues.some(i=>i.code==="review.confirmed" && i.target===field.id && i.message===fieldConfirmation(field));
    const badge=node("span",confirmed?"已确认":field?.value?"待确认":"待填写","badge "+(confirmed?"known":"assumed"));
    heading.append(badge,node("strong",fieldNames[key]));
    const row=node("div",undefined,"inline-confirmation"),input=node("input");
    input.type="number";input.min="1";input.max="100";input.step="1";input.value=field?.value || "";
    input.id=`confirm-${key}`;input.setAttribute("aria-label",fieldNames[key]);input.placeholder="填写整数";
    const resetConfirmation=()=>{badge.textContent="待确认";badge.className="badge assumed";confirm.textContent="确认";confirm.disabled=false;};
    input.oninput=()=>{setCycleField(key,input.value);resetConfirmation();};
    const explanation=node("input");explanation.placeholder="说明（选填，留空为默认）";
    explanation.setAttribute("aria-label",fieldNames[key]+"说明");
    explanation.value=field?.explanation==="默认"?"":field?.explanation || "";
    explanation.oninput=()=>{setCycleField(key,input.value);const fact=editable.facts.find(f=>f.category==="initial"&&f.key===key);fact.explanation=explanation.value.trim()||"默认";markDirty();resetConfirmation();};
    const confirm=node("button",confirmed?"已确认":"确认","secondary confirm-button");confirm.type="button";confirm.disabled=!!confirmed;
    confirm.setAttribute("aria-label",`确认${fieldNames[key]}`);
    confirm.onclick=()=>{
      const value=Number(input.value);
      if(!input.value || !Number.isInteger(value) || value<1 || value>100){toast("请填写 1 到 100 的整数。");input.focus();return;}
      setCycleField(key,input.value);
      const fact=editable.facts.find(f=>f.category==="initial"&&f.key===key);
      fact.explanation=explanation.value.trim() || "默认";recordFieldConfirmation(fact);
      markDirty();badge.textContent="已确认";badge.className="badge known";confirm.textContent="已确认";confirm.disabled=true;
      toast(`${fieldNames[key]}已确认，保存后生效。`);
    };
    row.append(input,node("span",key==="years"?"年":"期/年","muted"),explanation,confirm);
    item.append(heading,row);$("issues").append(item);
  }
}
function resolveModelIssue(issue,explanation) {
  const index=editable.issues.findIndex(i=>i.code===issue.code && i.target===issue.target && i.message===issue.message);
  if(index>=0)editable.issues[index]={...editable.issues[index],code:"review.resolved."+issue.code,severity:"info",message:issue.message+"；用户标注："+explanation};
}
function annotateIssue(issue,choice,unitIssue,explanation="") {
  const fact=issueFact(issue);
  const key=issue.target.split(".").pop();
  const matches=fact?[fact]:unitIssue?editable.facts.filter(f=>f.key===key):[];
  const note=`用户标注：${unitIssue?"单位"+choice+"，按无单位保存":choice}${explanation?"；"+explanation:""}`;
  if(unitIssue){
    for(const field of matches){field.unit="";field.explanation=note;}
  }else if(fact){
    Object.assign(fact,{value:null,state:"missing",evidence_ids:[],explanation:note});
  }
  if(!issue.code.startsWith("validation."))resolveModelIssue(issue,note);
  else if(fact && !unitIssue){
    editable.issues.push({code:"review.annotation",severity:"info",target:fact.id,
      message:`用户标注：${choice}`,evidence_ids:[]});
  }
  markDirty();renderFacts();renderIssues();toast(`已标注“${choice}”，保存后生效。`);
}
function updateFutureModules(solutions) {
  const latest=solutions.at(-1);
  const plan=latest ? `当前计划：${latest.plan.title}` : "尚未生成经营计划";
  $("artifacts-context").textContent=plan+" · 文档与表格导出待开发";
  $("conversation-context").textContent=plan+" · 方案答疑待开发";
}
function appendPlanNotes(card,title,items) {
  if(!items.length)return;
  const details=node("details",undefined,"plan-notes"),list=node("ul");
  details.append(node("summary",`${title} · ${items.length}`));
  for(const text of items)list.append(node("li",text));
  details.append(list);card.append(details);
}
function renderSolutions(solutions) {
  $("solutions").replaceChildren();$("solutions-empty").hidden=solutions.length>0;
  updateFutureModules(solutions);
  for (const [index,solution] of [...solutions].reverse().entries()) {
    const card=node("article",undefined,"card solution-card");
    const heading=node("div",undefined,"section-heading");
    heading.append(node("h2",solution.plan.title),node("span",index===0?"最近生成":"此前计划","badge "+(index===0?"known":"planned")));
    card.append(heading,node("p",solution.mode==="llm_only"?"直接生成 · AI 计划草稿":"搜索生成 · 候选方案比较","muted"));
    if(snapshot && solution.problem_version!==snapshot.version)card.append(node("p","题目已调整，这份计划基于调整前的数据。建议重新生成。","notice"));
    card.append(node("p",solution.evaluation.verified ? `已完成计算核对 · 得分 ${solution.evaluation.score}` : "方案草稿 · 产能、现金与履约尚未经过经营仿真核对","notice"),node("p",solution.plan.summary));
    const periods=new Map();
    for(const action of solution.plan.actions){if(!periods.has(action.period))periods.set(action.period,[]);periods.get(action.period).push(action);}
    const planDetails=node("details",undefined,"period-plan");planDetails.open=index===0;
    planDetails.append(node("summary",`逐期操作 · ${periods.size} 期 / ${solution.plan.actions.length} 项`));
    for(const [period,actions] of [...periods].sort((a,b)=>a[0]-b[0])){
      const group=node("section",undefined,"period-group");
      group.append(node("h3",`第 ${period} 期`));
      for(const action of actions){
        const row=node("div",undefined,"plan-action");row.append(node("p",action.description));
        const amounts=[];
        if(action.quantity!==null)amounts.push(`数量 ${action.quantity}`);
        if(action.amount!==null)amounts.push(`金额 ${action.amount}`);
        if(amounts.length)row.append(node("span",amounts.join(" · "),"action-values"));
        group.append(row);
      }
      planDetails.append(group);
    }
    card.append(planDetails);
    appendPlanNotes(card,"默认与暂定条件",solution.plan.assumptions);
    appendPlanNotes(card,"风险提醒",solution.plan.risks);
    appendPlanNotes(card,"检查结果",solution.evaluation.issues.map(i=>i.message));
    $("solutions").append(card);
  }
}
function renderJobs() {
  $("jobs").replaceChildren(); $("active-job").replaceChildren(); $("active-job").hidden=true;
  $("task-count").textContent=jobs.length ? `· ${jobs.length}` : "";
  for(const job of [...jobs].reverse()){
    const row=node("div",undefined,"task-row"), text=node("div",`${job.kind==="parse"?"文件解析":job.kind==="read"?"读题":"方案生成"} · ${statusNames[job.status]} · ${stageNames[job.stage]||job.stage}`);
    if(job.metadata?.model_preset)text.append(node("p",job.metadata.model_preset.preset_name,"muted"));
    if(job.error)text.append(node("p",job.error.message,"error"));row.append(text);
    if(["queued","running"].includes(job.status)){
      const button=node("button","取消","quiet");button.onclick=()=>safe(async()=>{await post(`${base()}/jobs/${job.id}/cancel`,{});await refresh();});row.append(button);
      if($("active-job").hidden){$("active-job").hidden=false;$("active-job").append(node("span",undefined,"spinner"),node("span",text.textContent));}
    }
    if(["failed","cancelled"].includes(job.status)){
      const retry=node("button","重试","secondary");retry.onclick=()=>busy(retry,async()=>{
        if(job.kind==="parse")await post(`${base()}/documents/${job.metadata.document_id}/retry`,{});
        else if(job.kind==="read"){await saveInputs();await post(`${base()}/read`,{document_ids:[...selectedFiles],input_revision:inputDraft.revision});}
        else await post(`${base()}/solve`,{version:job.metadata.version,mode:job.metadata.mode,...job.metadata.search});
        await refresh();
      });row.append(retry);
    }
    $("jobs").append(row);
  }
  updateControls();
}
async function refresh(initial = false) {
  if(!projectId || !authenticated)return;
  const captured=generation, url=base();
  const [docs, latestJobs, solutions]=await Promise.all([api(url+"/documents"),api(url+"/jobs"),api(url+"/solutions")]);
  if(captured!==generation)return;
  documents=docs; jobs=latestJobs;
  const docKey=JSON.stringify(docs.map(d=>[d.source.id,d.source.sha256]));
  if(docKey!==documentsSignature){documentsSignature=docKey;renderDocuments();}
  const jobKey=JSON.stringify(latestJobs);
  if(jobKey!==jobsSignature){jobsSignature=jobKey;renderJobs();}
  const solutionKey=JSON.stringify(solutions.map(s=>s.id));
  if(solutionKey!==solutionsSignature){solutionsSignature=solutionKey;renderSolutions(solutions);}
  const completed=Math.max(0,...jobs.filter(j=>j.kind==="read"&&["succeeded","needs_input"].includes(j.status)).map(j=>j.result?.version||0));
  if(initial || completed>latestReadSeen){
    latestReadSeen=completed;
    if(dirty){toast("新读题结果已保存；请先处理未保存的修改。");return;}
    try{const data=await api(url+"/problem");if(captured!==generation)return;renderReading(data);if(!initial)setStep(2);}
    catch(error){if(error.code!=="snapshot_not_found")throw error;}
  }
}
async function uploadFiles(files) {
  if(!projectId)return;
  const captured=generation, url=base();
  for(const file of files){
    if(!/\.(docx|doc|pdf|xls)$/i.test(file.name))throw new Error("请选择 DOCX、DOC、PDF 或 XLS 文件。");
    if(file.size>status.max_upload_mb*1024*1024)throw new Error(`${file.name} 超过大小限制。`);
    await api(url+"/documents",{method:"POST",headers:{"Content-Type":"application/octet-stream","X-Filename":encodeURIComponent(file.name)},body:file});
  }
  $("files").value="";
  if(captured===generation){toast("文件已提交解析。");await refresh();}
}
async function startReading(button) {
  if(dirty){toast("请先保存读题修改。");return;}
  const captured=generation;
  await busy(button,async()=>{await saveInputs();if(captured!==generation)return;await post(base()+"/read",{document_ids:[...selectedFiles],input_revision:inputDraft.revision});if(captured!==generation)return;setStep(2);await refresh();});
}
function markInputsDirty(){inputsDirty=true;inputEditSequence++;updateControls();}
async function saveInputs(){
  if(!inputsDirty)return;
  const captured=generation,sequence=inputEditSequence;
  const saved=await post(base()+"/inputs",structuredClone(inputDraft));
  if(captured!==generation)return;
  inputDraft.revision=saved.revision;
  if(sequence===inputEditSequence){inputDraft=saved;inputsDirty=false;}
  updateControls();
  if(inputsDirty)throw new Error("输入在保存期间有变化，请再次点击保存或读取。");
}
$("problem-text").oninput=()=>{inputDraft.problem_text=$("problem-text").value;markInputsDirty();};
$("supplement-text").oninput=()=>{inputDraft.supplement_text=$("supplement-text").value;markInputsDirty();};
$("save-inputs").onclick=()=>busy($("save-inputs"),async()=>{await saveInputs();toast("输入已保存。");});
async function openSettings() {
  modelCatalog=await api("/api/model-presets");
  modelProviders=modelCatalog.providers;
  $("model-provider").replaceChildren();
  for (const provider of modelProviders) {
    const option=node("option",provider.name); option.value=provider.id; $("model-provider").append(option);
  }
  renderPresetPicker(modelCatalog.active_id);
  fillPreset(modelCatalog.presets.find(p=>p.id===modelCatalog.active_id));
  $("settings-dialog").showModal();
}
function renderPresetPicker(id) {
  $("preset-picker").replaceChildren();
  for (const preset of modelCatalog.presets) {
    const option=node("option",`${preset.name}${preset.id===modelCatalog.active_id?" · 当前启用":""}`);
    option.value=preset.id;$("preset-picker").append(option);
  }
  $("preset-picker").value=id;
}
function fillPreset(settings) {
  selectedPresetId=settings.id || "";
  presetDirty=false;
  $("preset-test-result").hidden=true;
  $("preset-name").value=settings.name || "";
  $("model-provider").value=settings.provider;
  $("model-url").value=settings.base_url;$("model-name").value=settings.model;$("model-key").value="";
  $("model-key").placeholder=settings.api_key_set?"已保存，留空保留原密钥":"输入 API 密钥";
  $("clear-model-key").checked=false;$("model-format").value=settings.format;$("model-timeout").value=settings.timeout;
  $("model-input-limit").value=settings.max_input_chars;$("model-output-limit").value=settings.max_output_tokens;
  $("model-token-parameter").value=settings.token_parameter;
  $("model-thinking-mode").value=settings.thinking_mode || "default";
  $("model-thinking-budget").value=settings.thinking_budget || 8192;
  $("settings-status").textContent=!selectedPresetId?"新预设 · 保存后可启用":settings.id===modelCatalog.active_id
    ?"当前启用 · 新任务使用此预设":settings.configured?"已保存 · 可切换启用":"尚未配置完成";
  updateProviderFields();
  updatePresetControls();
}
function updatePresetControls() {
  const preset=modelCatalog?.presets.find(p=>p.id===selectedPresetId);
  $("activate-preset").disabled=!preset || presetDirty || !preset.configured || preset.id===modelCatalog.active_id;
  $("delete-preset").disabled=!preset || preset.id===modelCatalog.active_id;
  $("test-preset").disabled=!preset || presetDirty || !preset.configured;
  $("reset-preset").hidden=!presetDirty;
  $("save-preset").textContent=selectedPresetId?"保存修改":"保存新预设";
}
function markPresetDirty() {
  presetDirty=true;$("settings-status").textContent="有未保存修改";updatePresetControls();
}
function updateProviderFields() {
  const provider=$("model-provider").value;
  $("provider-help").textContent=providerHelp[provider] || "";
  $("token-parameter-field").hidden=provider!=="openai_chat";
  $("thinking-mode-field").hidden=!["openai_chat","anthropic","ollama"].includes(provider);
  $("thinking-budget-field").hidden=provider!=="anthropic" || $("model-thinking-mode").value!=="enabled";
}
$("model-thinking-mode").onchange=()=>{markPresetDirty();updateProviderFields();};
$("model-provider").onchange=()=>{
  const provider=modelProviders.find(item=>item.id===$("model-provider").value);
  $("model-url").value=provider.base_url;
  $("model-name").value="";
  $("model-key").value="";
  $("model-key").placeholder=provider.api_key_required?"输入此接口的 API 密钥":"本机服务可留空";
  $("clear-model-key").checked=false;
  $("model-thinking-mode").value="default";
  $("settings-status").textContent="接口已切换，请填写模型及其密钥后保存";
  markPresetDirty();
  updateProviderFields();
};
$("settings-form").addEventListener("input",markPresetDirty);
$("preset-picker").onchange=()=>{
  if(presetDirty){$("preset-picker").value=selectedPresetId;toast("请先保存或撤销预设修改。");return;}
  fillPreset(modelCatalog.presets.find(p=>p.id===$("preset-picker").value));
};
$("new-preset").onclick=()=>{
  if(presetDirty){toast("请先保存或撤销预设修改。");return;}
  $("preset-picker").value="";
  fillPreset({provider:"openai_chat",base_url:modelProviders.find(p=>p.id==="openai_chat").base_url,
    model:"",api_key_set:false,format:"json_schema",timeout:90,max_input_chars:100000,
    max_output_tokens:12000,token_parameter:"max_completion_tokens"});
  $("preset-name").focus();
};
$("reset-preset").onclick=()=>{
  const preset=modelCatalog.presets.find(p=>p.id===(selectedPresetId || modelCatalog.active_id));
  renderPresetPicker(preset.id);fillPreset(preset);
};
async function activatePreset(preset) {
  await post(`/api/model-presets/${preset.id}/activate`,{expected_revision:preset.revision});
  await loadStatus();
  if($("settings-dialog").open){renderPresetPicker(preset.id);fillPreset(modelCatalog.presets.find(p=>p.id===preset.id));}
  toast(`已启用 ${preset.name}，新任务使用此预设。`);
}
$("active-model-preset").onchange=()=>busy($("active-model-preset"),async()=>{
  if(presetDirty && $("settings-dialog").open){$("active-model-preset").value=modelCatalog.active_id;toast("请先保存或撤销预设修改。");return;}
  const preset=modelCatalog.presets.find(p=>p.id===$("active-model-preset").value);
  try{await activatePreset(preset);}finally{$("active-model-preset").value=modelCatalog.active_id;}
});
$("activate-preset").onclick=()=>busy($("activate-preset"),()=>activatePreset(modelCatalog.presets.find(p=>p.id===selectedPresetId)));
$("test-preset").onclick=()=>busy($("test-preset"),async()=>{
  const preset=modelCatalog.presets.find(p=>p.id===selectedPresetId);
  const result=$("preset-test-result"); result.hidden=false;result.textContent="正在测试接口与 JSON 输出…";
  try{
    const test=await post(`/api/model-presets/${preset.id}/test`,{expected_revision:preset.revision});
    result.textContent=`${preset.name} · 版本 ${test.preset_revision} 测试通过 · ${test.elapsed_ms} ms`;
  }catch(error){result.textContent=`${preset.name} · 测试失败：${error.message}`;throw error;}
});
$("delete-preset").onclick=()=>{
  $("delete-preset-name").textContent=modelCatalog.presets.find(p=>p.id===selectedPresetId).name;
  $("delete-preset-dialog").showModal();
};
$("confirm-delete-preset").onclick=()=>busy($("confirm-delete-preset"),async()=>{
  const preset=modelCatalog.presets.find(p=>p.id===selectedPresetId);
  await api(`/api/model-presets/${preset.id}?revision=${preset.revision}`,{method:"DELETE"});
  $("delete-preset-dialog").close();await openSettings();await loadStatus();toast("预设已删除。");
});
$("login-form").onsubmit=async event=>{
  event.preventDefault();$("login-button").disabled=true;$("login-error").hidden=true;
  try{await post("/api/auth/login",{key:$("access-key").value,remember:$("remember").checked});await enterApp();}
  catch(error){$("login-error").textContent=error.message;$("login-error").hidden=false;}
  finally{$("login-button").disabled=false;}
};
$("logout").onclick=()=>navigate(async()=>{await post("/api/auth/logout",{});dirty=false;showLogin();});
$("home").onclick=$("back-projects").onclick=()=>navigate(showProjects);
$("discard-confirm").onclick=()=>{const operation=pendingNavigation;pendingNavigation=null;dirty=false;inputsDirty=false;$("discard-dialog").close();if(operation)safe(operation);};
window.addEventListener("beforeunload", event=>{if(dirty || inputsDirty){event.preventDefault();event.returnValue="";}});
$("new-project").onclick=$("empty-create").onclick=()=>{$("project-dialog").showModal();$("project-name").focus();};
$("create-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{const project=await post("/api/projects",{name:$("project-name").value});$("project-dialog").close();$("project-name").value="";projects.push(project);await openProject(project);});};
$("delete-current").onclick=()=>askDelete(projects.find(p=>p.id===projectId));
$("delete-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{await api(base(pendingDelete.id),{method:"DELETE"});$("delete-dialog").close();await showProjects();toast("项目已删除。");});};
for(const button of document.querySelectorAll("[data-close]"))button.onclick=()=>$(button.dataset.close).close();
for(const button of document.querySelectorAll("[data-step]"))button.onclick=()=>setStep(Number(button.dataset.step));
for(const button of document.querySelectorAll("[data-go-step]"))button.onclick=()=>setStep(Number(button.dataset.goStep));
for(const button of document.querySelectorAll("[data-review]"))button.onclick=()=>reviewTab(button.dataset.review);
for(const radio of document.querySelectorAll('input[name="solver-mode"]'))radio.onchange=updateControls;
$("files").onchange=()=>safe(()=>uploadFiles([...$("files").files]));
for(const type of ["dragenter","dragover"])$("drop-zone").addEventListener(type,event=>{event.preventDefault();$("drop-zone").classList.add("dragover");});
for(const type of ["dragleave","drop"])$("drop-zone").addEventListener(type,event=>{event.preventDefault();$("drop-zone").classList.remove("dragover");if(type==="drop")safe(()=>uploadFiles([...event.dataTransfer.files]));});
$("read").onclick=()=>startReading($("read"));$("reread").onclick=()=>startReading($("reread"));
$("next-solve").onclick=()=>busy($("next-solve"),async()=>{
  if(dirty)await saveRevision();
  const ready=reviewReadiness();
  if(ready.errors || !ready.horizon){reviewTab("issues");toast(ready.errors?"仍有错误需要处理。":"请填写经营年数和每年期数。");return;}
  setStep(3);
});$("back-review").onclick=()=>setStep(2);
$("fact-search").oninput=renderFacts;$("category-filter").onchange=renderFacts;$("state-filter").onchange=renderFacts;
async function saveRevision() {
  const result=await post(base()+"/revisions",{expected_version:snapshot.version,note:"默认",reading:editable});
  renderReading({snapshot:result});
  toast(result.reading.issues.some(i=>i.severity==="error")?"已保存，仍有错误需要处理。":!result.reading.problem.years || !result.reading.problem.periods_per_year?"已保存，请补充经营周期。":"已保存，可继续生成草稿。");
}
$("save-revision").onclick=()=>busy($("save-revision"),saveRevision);
$("apply-json").onclick=()=>safe(async()=>{const data=JSON.parse($("reading-json").value);if(!Array.isArray(data.facts)||!Array.isArray(data.rules)||!Array.isArray(data.issues)||typeof data.title!=="string")throw new Error("结构化数据必须包含 title、facts、rules、issues。");editable=data;markDirty();renderFacts();renderRules();renderIssues();toast("修改已应用，请保存。");});
$("solve").onclick=()=>busy($("solve"),async()=>{await post(base()+"/solve",{version:snapshot.version,mode:mode(),seed:Number($("seed").value),candidates:Number($("candidates").value)});await refresh();});
$("open-settings").onclick=$("configure-model").onclick=()=>safe(openSettings);
$("settings-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{
  const data={name:$("preset-name").value,provider:$("model-provider").value,base_url:$("model-url").value,model:$("model-name").value,api_key:$("model-key").value,clear_api_key:$("clear-model-key").checked,format:$("model-format").value,token_parameter:$("model-token-parameter").value,thinking_mode:$("model-thinking-mode").value,thinking_budget:Number($("model-thinking-budget").value),timeout:Number($("model-timeout").value),max_input_chars:Number($("model-input-limit").value),max_output_tokens:Number($("model-output-limit").value)};
  if(selectedPresetId)data.expected_revision=modelCatalog.presets.find(p=>p.id===selectedPresetId).revision;
  const saved=await post(selectedPresetId?`/api/model-presets/${selectedPresetId}`:"/api/model-presets",data);
  $("model-key").value="";await loadStatus();renderPresetPicker(saved.id);fillPreset(saved);toast("模型预设已保存。");
});};
$("review-open-issues").onclick=()=>reviewTab("issues");
$("review-open-rules").onclick=()=>reviewTab("rules");
$("issue-filter").onchange=renderIssues;
$("reset-filters").onclick=()=>{$("fact-search").value="";$("category-filter").value="";$("state-filter").value="";renderFacts();};
$("fact-evidence-search").oninput=renderEvidencePicker;
$("fact-form").onsubmit=event=>{
  event.preventDefault();
  const value=$("edit-fact-value").value.trim(), state=$("edit-fact-state").value;
  const explanation=$("edit-fact-explanation").value.trim();
  let error="";
  if(state==="known" && (!value || !editingEvidence.size))error="已知字段必须填写值并选择支持该值的原文。";
  if(state==="missing" && value)error="缺失状态应留空；有暂定值时请选择假设。";
  $("fact-editor-error").textContent=error;$("fact-editor-error").hidden=!error;if(error)return;
  Object.assign(editingFact,{value:value || null,unit:$("edit-fact-unit").value.trim(),
    value_type:$("edit-fact-type").value,state:value?state:"missing",conversion:$("edit-fact-conversion").value,
    evidence_ids:[...editingEvidence],explanation:explanation || "默认"});
  recordFieldConfirmation(editingFact);
  markDirty();renderFacts();renderIssues();$("fact-dialog").close();toast("字段已确认，保存后生效。");
};
$("unit-none").onclick=()=>{$("edit-fact-unit").value="";$("edit-fact-explanation").value="用户标注：无单位";};
$("unit-unknown").onclick=()=>{$("edit-fact-unit").value="";$("edit-fact-explanation").value="用户标注：单位不清楚，按无单位保存";};
$("add-rule").onclick=()=>{
  let id="review_rule";while(editable.rules.some(r=>r.id===id))id+="_";
  editable.rules.push({id,name:"待补充规则",expression:"",state:"assumed",evidence_ids:[],explanation:""});
  markDirty();renderRules();$("rule-row-"+id)?.scrollIntoView({block:"center"});
};
$("close-evidence").onclick=()=>{$("evidence-drawer").hidden=true;};
safe(async()=>{const session=await api("/api/auth/session");if(session.authenticated)await enterApp();else if(!session.configured){$("login-error").textContent="请先在服务器 .env 中设置 ERP_ACCESS_KEY。";$("login-error").hidden=false;}});
setInterval(()=>{if(polling||!authenticated||!projectId)return;polling=true;refresh().catch(error=>toast(error.message)).finally(()=>{polling=false;});},2000);
