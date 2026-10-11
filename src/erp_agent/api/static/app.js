"use strict";
const $ = id => document.getElementById(id);
const labels = {
  initial:"初始条件", certification:"资格认证", market:"市场", product:"产品", bom:"产品用料",
  material:"原材料", production_line:"生产线", worker:"人员", training:"培训", incentive:"激励",
  shift:"班次", finance:"融资", discount:"贴现", demand_forecast:"市场调研", order_contract:"订单",
  carbon:"碳排放", digital:"数智化", budget:"预算", other:"其他"
};
const stateNames = {known:"已知", assumed:"假设", conflicted:"冲突", missing:"缺失"};
const fieldNames = {initial_cash:"初始现金",years:"经营年数",periods_per_year:"每年期数",
  management_fee:"管理费",monthly_salary:"月薪",efficiency:"效率",piece_rate:"计件工资",
  capacity:"基础产能",production_cycle:"生产周期",delivery_period:"送货期",
  payment_period:"账期",interest_rate:"利率",startup_cost:"单件开产费"};
const statusNames = {queued:"排队",running:"执行中",succeeded:"完成",failed:"失败",cancelled:"已取消",needs_input:"需要复核"};
const stageNames = {queued:"排队",starting:"开始",parsing:"解析文件",saving_document:"保存文件",reading:"整理题目",repairing_reading:"修复输出格式",validating:"校验来源",saving_reading:"保存读题",proposing:"生成策略",validating_plan:"校验计划",searching:"搜索候选",validating_best:"复验方案",saving:"保存结果",done:"已完成",cancelling:"等待取消"};
let projects = [], projectId = "", snapshot = null, editable = null, status = null;
let documents = [], jobs = [], selectedFiles = new Set(), knownFiles = new Set();
let authenticated = false, dirty = false, polling = false, generation = 0, currentStep = 1;
let pendingDelete = null, latestReadSeen = 0, documentsSignature = "", jobsSignature = "", solutionsSignature = "";
let toastTimer;
let pendingNavigation = null;
let modelProviders = [];
const providerHelp = {
  openai_chat: "适用于 OpenAI、DeepSeek、通义等兼容服务。填写基础地址（如 /v1）或完整 Chat Completions 地址。",
  openai_responses: "使用 OpenAI Responses 格式，可填写 /v1 基础地址或完整 /responses 地址。",
  anthropic: "使用 Claude 原生 Messages 格式，填写 /v1 基础地址或完整 /messages 地址。",
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
  finally { button.disabled = false; updateControls(); }
}
function markDirty() {
  dirty = true;
  $("dirty-note").textContent = "有未保存修改";
  $("reading-json").value = JSON.stringify(editable, null, 2);
  updateControls();
}
function navigate(operation) {
  if (dirty && projectId) {
    pendingNavigation = operation;
    $("discard-dialog").showModal();
  } else safe(operation);
}
function setStep(number) {
  currentStep = number;
  for (const button of document.querySelectorAll("[data-step]")) {
    const step = Number(button.dataset.step);
    button.classList.toggle("active", step === number);
    button.classList.toggle("complete", (step === 1 && documents.length > 0) || (step === 2 && !!snapshot));
    if (step === number) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  for (let step = 1; step <= 3; step++) $("step-" + step).hidden = step !== number;
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
  const canRead = authenticated && !!projectId && selectedFiles.size > 0 && status?.llm_configured && !activeRead();
  $("read").disabled = !canRead;
  $("reread").disabled = !canRead || dirty;
  $("read").textContent = activeRead() ? "正在读题…" : "读取题目 →";
  $("model-required").hidden = !!status?.llm_configured;
  $("selected-count").textContent = `已选择 ${selectedFiles.size} 个文件`;
  $("search-config").hidden = !mc;
  const readingErrors = snapshot?.reading.issues.some(issue => issue.severity === "error");
  $("solve").disabled = !snapshot || dirty || readingErrors || !status?.llm_configured || (mc && !status?.simulation_ready);
  $("save-revision").disabled = !snapshot || !dirty;
  $("next-solve").disabled = !snapshot || dirty || readingErrors;
  $("solver-note").textContent = readingErrors ? "请返回读题复核，修正错误后保存新版本。" : mc
    ? (status?.simulation_ready ? "在仿真器中搜索并比较候选方案。" : "经营仿真内核尚未就绪，暂不能使用蒙特卡罗。")
    : "当前输出为方案草稿，产能、现金与履约尚未经过经营仿真验证。";
  $("solve-version").textContent = snapshot ? `题目版本 ${snapshot.version}` : "请先完成读题";
  $("step1-caption").textContent = documents.length ? `${documents.length} 个文件` : "添加赛题与补充文件";
  $("step2-caption").textContent = snapshot ? `版本 ${snapshot.version} · ${snapshot.reading.problem.facts.length} 个字段` : "核对数据与规则";
}
async function loadStatus() {
  status = await api("/api/status");
  $("model-dot").classList.toggle("ready", status.llm_configured);
  $("upload-limit").textContent = `单文件最大 ${status.max_upload_mb} MB`;
  updateControls();
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
  $("project-title").textContent = project.name;
  $("project-subtitle").textContent = `创建于 ${new Date(project.created_at).toLocaleDateString("zh-CN")}`;
  $("project-view").hidden = true; $("flow-view").hidden = false;
  $("reading-empty").hidden = false; $("review").hidden = true;
  $("documents").replaceChildren(); $("source-content").replaceChildren(); $("solutions").replaceChildren();
  $("jobs").replaceChildren(); $("versions").replaceChildren(); $("active-job").hidden = true;
  $("fact-search").value = ""; $("state-filter").value = "";
  $("evidence-drawer").hidden = true;
  setStep(1); await refresh(true);
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
    const row = node("label", undefined, "file-row");
    const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selectedFiles.has(id);
    checkbox.onchange = () => { if (checkbox.checked) selectedFiles.add(id); else selectedFiles.delete(id); updateControls(); };
    const info = node("span", document.source.filename, "file-info");
    info.append(node("small", `${Math.ceil(document.source.size / 1024)} KB · ${document.evidence.length} 处证据`));
    row.append(checkbox, info, node("span", "已解析", "badge known"));
    $("documents").append(row);
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
function showEvidence(ids, title = "原文证据") {
  $("evidence-content").replaceChildren(node("h3", title));
  const index = new Map(documents.flatMap(doc => doc.evidence.map(e => [e.id, {...e, filename:doc.source.filename}])));
  if (!ids.length) $("evidence-content").append(node("p", "未关联原文证据。", "muted"));
  for (const id of ids) {
    const evidence = index.get(id), item = node("div", undefined, "evidence-item");
    item.append(node("small", evidence ? `${evidence.filename} · ${evidence.location}` : "证据不存在"));
    item.append(node("p", evidence?.text || "（空白或合并单元格）"), node("code", id));
    $("evidence-content").append(item);
  }
  $("evidence-drawer").hidden = false;
}
function renderReading(data) {
  snapshot = data.snapshot;
  editable = {title:snapshot.reading.problem.title, facts:structuredClone(snapshot.reading.problem.facts),
    rules:structuredClone(snapshot.reading.rules.rules), issues:snapshot.reading.issues.filter(issue => !issue.code.startsWith("validation."))};
  dirty = false;
  $("versions").replaceChildren();
  for (const version of data.versions) { const option = node("option", `版本 ${version}`); option.value = version; $("versions").append(option); }
  $("versions").value = snapshot.version;
  $("reading-empty").hidden = true; $("review").hidden = false;
  $("reading-model").textContent = `${snapshot.reading.model} · ${new Date(snapshot.created_at).toLocaleString("zh-CN")}`;
  $("metric-facts").textContent = editable.facts.length;
  $("metric-errors").textContent = snapshot.reading.issues.filter(i => i.severity === "error").length;
  $("metric-pending").textContent = editable.facts.filter(f => f.state !== "known").length;
  $("metric-rules").textContent = editable.rules.length;
  $("issue-count").textContent = snapshot.reading.issues.filter(i => i.severity !== "info").length;
  $("category-filter").replaceChildren(node("option", "全部分类"));
  $("category-filter").firstChild.value = "";
  for (const category of new Set(editable.facts.map(f => f.category))) { const option=node("option",labels[category] || category); option.value=category; $("category-filter").append(option); }
  $("reading-json").value = JSON.stringify(editable, null, 2);
  $("dirty-note").textContent = "修改后请保存新版本";
  renderFacts(); renderRules(); renderIssues(); updateControls(); setStep(currentStep);
}
function renderFacts() {
  $("facts").replaceChildren();
  if (!editable) return;
  const search = $("fact-search").value.trim().toLowerCase();
  const category = $("category-filter").value, state = $("state-filter").value;
  const visible = editable.facts.map((fact,index) => ({fact,index})).filter(({fact}) =>
    (!category || fact.category === category) && (!state || fact.state === state) &&
    (!search || `${fact.entity_name} ${fact.key} ${fact.value ?? ""}`.toLowerCase().includes(search)));
  const groups = new Map();
  for (const item of visible) { if (!groups.has(item.fact.category)) groups.set(item.fact.category, []); groups.get(item.fact.category).push(item); }
  for (const [category, items] of groups) {
    const group = node("tr", undefined, "group-row"), cell = node("td", `${labels[category] || category} · ${items.length}`); cell.colSpan=5; group.append(cell); $("facts").append(group);
    for (const {fact} of items) {
      const tr = node("tr"), name = node("td"); name.append(node("strong",fact.entity_name),node("small",fieldNames[fact.key] || fact.key));
      if (fact.explanation) name.append(node("small", fact.explanation));
      const valueCell=node("td"), value=node("input"); value.value=fact.value ?? ""; value.placeholder="未提供"; value.setAttribute("aria-label",`${fact.entity_name} ${fact.key}的值`);
      value.onchange=()=>{ fact.value=value.value === "" ? null : value.value; if(fact.value===null) {fact.state="missing";stateSelect.value="missing";} tr.classList.add("changed"); markDirty(); };
      valueCell.append(value);
      const stateCell=node("td"), stateSelect=node("select"); stateSelect.setAttribute("aria-label",`${fact.key}的状态`);
      for(const [key,label] of Object.entries(stateNames)){const option=node("option",label);option.value=key;stateSelect.append(option);} stateSelect.value=fact.state;
      stateSelect.onchange=()=>{fact.state=stateSelect.value;tr.classList.add("changed");markDirty();};stateCell.append(stateSelect);
      const sourceCell=node("td"), sourceButton=node("button",fact.evidence_ids.length ? `${fact.evidence_ids.length} 处` : "无来源","source-button");
      sourceButton.onclick=()=>showEvidence(fact.evidence_ids,`${fact.entity_name} · ${fact.key}`);sourceCell.append(sourceButton);
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
  if (!editable.rules.length) $("rules").append(node("p","未提取到独立规则。","empty"));
  for (const rule of editable.rules) {
    const item=node("div",undefined,"rule-item"), title=node("div",undefined,"rule-title");
    const state = node("select"); state.setAttribute("aria-label", `${rule.name}的状态`);
    for (const [value,label] of Object.entries(stateNames)) { const option = node("option",label); option.value=value; state.append(option); }
    state.value=rule.state; state.onchange=()=>{rule.state=state.value;markDirty();};
    title.append(node("strong",rule.name),state);
    const source=node("button","查看来源","source-button");source.onclick=()=>showEvidence(rule.evidence_ids,rule.name);title.append(source);
    const expression=node("textarea"); expression.value=rule.expression; expression.rows=2;
    expression.setAttribute("aria-label", `${rule.name}的公式或限制`);
    expression.onchange=()=>{rule.expression=expression.value;rule.state="assumed";state.value="assumed";markDirty();};
    const explanation=node("textarea"); explanation.value=rule.explanation; explanation.rows=2;
    explanation.placeholder="规则口径与适用范围"; explanation.setAttribute("aria-label",`${rule.name}的说明`);
    explanation.onchange=()=>{rule.explanation=explanation.value;markDirty();};
    item.append(title,expression,explanation);$("rules").append(item);
  }
}
function renderIssues() {
  $("issues").replaceChildren();
  for (const issue of snapshot.reading.issues) {
    if(issue.code === "validation.rule_unimplemented") continue;
    const item=node("div",issue.message,`issue-item ${issue.severity}`);item.append(node("span",`关联：${issue.target}`,"muted"));
    if(issue.evidence_ids.length){const button=node("button","查看原文","source-button");button.onclick=()=>showEvidence(issue.evidence_ids);item.append(button);}
    $("issues").append(item);
  }
  if (!$("issues").children.length) $("issues").append(node("p","没有待核对事项。","empty"));
}
function renderSolutions(solutions) {
  $("solutions").replaceChildren();
  for (const solution of [...solutions].reverse()) {
    const card=node("article",undefined,"card");
    card.append(node("h2",solution.plan.title),node("p",`题目版本 ${solution.problem_version} · ${solution.mode === "llm_only" ? "单 LLM" : "LLM + 蒙特卡罗"}`,"muted"));
    card.append(node("p",solution.evaluation.verified ? `评分 ${solution.evaluation.score}` : "草稿 · 尚未验证经营可行性","notice"),node("p",solution.plan.summary));
    for(const action of solution.plan.actions){const row=node("div",undefined,"solution-action");row.append(node("span",`第 ${action.period} 期`,"period"),node("span",`${action.description}${action.quantity!==null?` · 数量 ${action.quantity}`:""}${action.amount!==null?` · 金额 ${action.amount}`:""}`));card.append(row);}
    const details=node("details");details.append(node("summary","假设、风险与校验"),node("pre",[...solution.plan.assumptions,...solution.plan.risks,...solution.evaluation.issues.map(i=>i.message)].join("\n")));card.append(details);$("solutions").append(card);
  }
}
function renderJobs() {
  $("jobs").replaceChildren(); $("active-job").replaceChildren(); $("active-job").hidden=true;
  $("task-count").textContent=jobs.length ? `· ${jobs.length}` : "";
  for(const job of [...jobs].reverse()){
    const row=node("div",undefined,"task-row"), text=node("div",`${job.kind==="parse"?"文件解析":job.kind==="read"?"读题":"方案生成"} · ${statusNames[job.status]} · ${stageNames[job.stage]||job.stage}`);
    if(job.error)text.append(node("p",job.error.message,"error"));row.append(text);
    if(["queued","running"].includes(job.status)){
      const button=node("button","取消","quiet");button.onclick=()=>safe(async()=>{await post(`${base()}/jobs/${job.id}/cancel`,{});await refresh();});row.append(button);
      if($("active-job").hidden){$("active-job").hidden=false;$("active-job").append(node("span",undefined,"spinner"),node("span",text.textContent));}
    }
    if(["failed","cancelled"].includes(job.status)){
      const retry=node("button","重试","secondary");retry.onclick=()=>busy(retry,async()=>{
        if(job.kind==="parse")await post(`${base()}/documents/${job.metadata.document_id}/retry`,{});
        else if(job.kind==="read")await post(`${base()}/read`,{document_ids:job.metadata.document_ids});
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
    if(!/\.(docx|doc)$/i.test(file.name))throw new Error("请选择 DOCX 或 DOC 文件。");
    if(file.size>status.max_upload_mb*1024*1024)throw new Error(`${file.name} 超过大小限制。`);
    await api(url+"/documents",{method:"POST",headers:{"Content-Type":"application/octet-stream","X-Filename":encodeURIComponent(file.name)},body:file});
  }
  $("files").value="";
  if(captured===generation){toast("文件已提交解析。");await refresh();}
}
async function startReading(button) {
  if(dirty){toast("请先保存读题修改。");return;}
  await busy(button,async()=>{await post(base()+"/read",{document_ids:[...selectedFiles]});setStep(2);await refresh();});
}
async function openSettings() {
  const settings=await api("/api/model-settings");
  modelProviders=settings.providers;
  $("model-provider").replaceChildren();
  for (const provider of modelProviders) {
    const option=node("option",provider.name); option.value=provider.id; $("model-provider").append(option);
  }
  $("model-provider").value=settings.provider;
  $("model-url").value=settings.base_url;$("model-name").value=settings.model;$("model-key").value="";
  $("model-key").placeholder=settings.api_key_set?"已保存，留空保留原密钥":"输入 API 密钥";
  $("clear-model-key").checked=false;$("model-format").value=settings.format;$("model-timeout").value=settings.timeout;
  $("model-input-limit").value=settings.max_input_chars;$("model-output-limit").value=settings.max_output_tokens;
  $("model-token-parameter").value=settings.token_parameter;
  $("settings-status").textContent=settings.configured?"已配置 · 修改后用于新请求":"尚未配置模型";
  updateProviderFields();
  $("settings-dialog").showModal();
}
function updateProviderFields() {
  const provider=$("model-provider").value;
  $("provider-help").textContent=providerHelp[provider] || "";
  $("token-parameter-field").hidden=provider!=="openai_chat";
}
$("model-provider").onchange=()=>{
  const provider=modelProviders.find(item=>item.id===$("model-provider").value);
  $("model-url").value=provider.base_url;
  $("model-name").value="";
  $("model-key").value="";
  $("model-key").placeholder=provider.api_key_required?"输入此接口的 API 密钥":"本机服务可留空";
  $("clear-model-key").checked=false;
  $("settings-status").textContent="接口已切换，请填写模型及其密钥后保存";
  updateProviderFields();
};
$("login-form").onsubmit=async event=>{
  event.preventDefault();$("login-button").disabled=true;$("login-error").hidden=true;
  try{await post("/api/auth/login",{key:$("access-key").value,remember:$("remember").checked});await enterApp();}
  catch(error){$("login-error").textContent=error.message;$("login-error").hidden=false;}
  finally{$("login-button").disabled=false;}
};
$("logout").onclick=()=>navigate(async()=>{await post("/api/auth/logout",{});dirty=false;showLogin();});
$("home").onclick=$("back-projects").onclick=()=>navigate(showProjects);
$("discard-confirm").onclick=()=>{const operation=pendingNavigation;pendingNavigation=null;dirty=false;$("discard-dialog").close();if(operation)safe(operation);};
window.addEventListener("beforeunload", event=>{if(dirty){event.preventDefault();event.returnValue="";}});
$("new-project").onclick=$("empty-create").onclick=()=>{$("project-dialog").showModal();$("project-name").focus();};
$("create-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{const project=await post("/api/projects",{name:$("project-name").value});$("project-dialog").close();$("project-name").value="";projects.push(project);await openProject(project);});};
$("delete-current").onclick=()=>askDelete(projects.find(p=>p.id===projectId));
$("delete-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{await api(base(pendingDelete.id),{method:"DELETE"});$("delete-dialog").close();await showProjects();toast("项目已删除。");});};
for(const button of document.querySelectorAll("[data-close]"))button.onclick=()=>$(button.dataset.close).close();
for(const button of document.querySelectorAll("[data-step]"))button.onclick=()=>setStep(Number(button.dataset.step));
for(const button of document.querySelectorAll("[data-review]"))button.onclick=()=>reviewTab(button.dataset.review);
for(const radio of document.querySelectorAll('input[name="solver-mode"]'))radio.onchange=updateControls;
$("files").onchange=()=>safe(()=>uploadFiles([...$("files").files]));
for(const type of ["dragenter","dragover"])$("drop-zone").addEventListener(type,event=>{event.preventDefault();$("drop-zone").classList.add("dragover");});
for(const type of ["dragleave","drop"])$("drop-zone").addEventListener(type,event=>{event.preventDefault();$("drop-zone").classList.remove("dragover");if(type==="drop")safe(()=>uploadFiles([...event.dataTransfer.files]));});
$("read").onclick=()=>startReading($("read"));$("reread").onclick=()=>startReading($("reread"));
$("next-solve").onclick=()=>setStep(3);$("back-review").onclick=()=>setStep(2);
$("versions").onchange=()=>safe(async()=>{if(dirty){$("versions").value=snapshot.version;toast("请先保存修改。");return;}const data=await api(base()+`/problem?version=${$("versions").value}`);renderReading(data);});
$("fact-search").oninput=renderFacts;$("category-filter").onchange=renderFacts;$("state-filter").onchange=renderFacts;
$("save-revision").onclick=()=>{$("revision-dialog").showModal();$("revision-note").focus();};
$("revision-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{
  const result=await post(base()+"/revisions",{expected_version:snapshot.version,note:$("revision-note").value,reading:editable});
  const versions=[result.version,...Array.from($("versions").options,o=>Number(o.value))];renderReading({snapshot:result,versions});
  $("revision-dialog").close();$("revision-note").value="";toast("新版本已保存并校验。");
});};
$("apply-json").onclick=()=>safe(async()=>{const data=JSON.parse($("reading-json").value);if(!Array.isArray(data.facts)||!Array.isArray(data.rules)||!Array.isArray(data.issues)||typeof data.title!=="string")throw new Error("结构化数据必须包含 title、facts、rules、issues。");editable=data;markDirty();renderFacts();renderRules();toast("已应用，请保存新版本。");});
$("solve").onclick=()=>busy($("solve"),async()=>{await post(base()+"/solve",{version:snapshot.version,mode:mode(),seed:Number($("seed").value),candidates:Number($("candidates").value)});await refresh();});
$("open-settings").onclick=$("configure-model").onclick=()=>safe(openSettings);
$("settings-form").onsubmit=event=>{event.preventDefault();busy(event.submitter,async()=>{
  await post("/api/model-settings",{provider:$("model-provider").value,base_url:$("model-url").value,model:$("model-name").value,api_key:$("model-key").value,clear_api_key:$("clear-model-key").checked,format:$("model-format").value,token_parameter:$("model-token-parameter").value,timeout:Number($("model-timeout").value),max_input_chars:Number($("model-input-limit").value),max_output_tokens:Number($("model-output-limit").value)});
  $("model-key").value="";$("settings-dialog").close();await loadStatus();toast("模型设置已保存。");
});};
$("close-evidence").onclick=()=>{$("evidence-drawer").hidden=true;};
safe(async()=>{const session=await api("/api/auth/session");if(session.authenticated)await enterApp();else if(!session.configured){$("login-error").textContent="请先在服务器 .env 中设置 ERP_ACCESS_KEY。";$("login-error").hidden=false;}});
setInterval(()=>{if(polling||!authenticated||!projectId)return;polling=true;refresh().catch(error=>toast(error.message)).finally(()=>{polling=false;});},2000);
