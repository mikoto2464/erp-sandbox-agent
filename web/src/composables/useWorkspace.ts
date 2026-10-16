import { computed, ref } from 'vue';
import type { Router } from 'vue-router';
import { ApiError, post, request } from '../lib/api';
import { cloneJson } from '../lib/json';
import { distinctIssues } from '../lib/review';
import type {
  EditableReading,
  BudgetTemplate,
  Artifact,
  WorkbookMapping,
  WorkbookEntry,
  GenerationConfig,
  Job,
  ModelCatalog,
  ParsedDocument,
  PlanTone,
  Project,
  ReadingInputs,
  Snapshot,
  Solution,
  Status,
} from '../lib/types';
import { useToasts } from './useToasts';
import { useApplicationJobs } from './useApplicationJobs';
const emptyInputs = (): ReadingInputs => ({
  revision: 0,
  problem_text: '',
  supplement_text: '',
  annotations: [],
});
export function useWorkspace(router: Router) {
  const authenticated = ref(false),
    initializing = ref(true),
    loading = ref(false);
  const projects = ref<Project[]>([]),
    project = ref<Project | null>(null),
    status = ref<Status | null>(null),
    catalog = ref<ModelCatalog | null>(null);
  const documents = ref<ParsedDocument[]>([]),
    solutions = ref<Solution[]>([]);
  const mappings = ref<WorkbookMapping[]>([]),
    artifacts = ref<Artifact[]>([]);
  const snapshot = ref<Snapshot | null>(null),
    editable = ref<EditableReading | null>(null),
    inputs = ref(emptyInputs());
  const inputsDirty = ref(false),
    readingDirty = ref(false),
    taskOpen = ref(false),
    resolutionJobId = ref<string | null>(null),
    pendingNavigation = ref<null | (() => void)>(null);
  const planTone = ref<PlanTone>('balanced'),
    customPrompt = ref(''),
    templateIds = ref<string[]>([]),
    templates = ref<BudgetTemplate[]>([]),
    selectedSolutionId = ref<string | null>(null);
  const currentSolution = computed(
    () =>
      solutions.value.find((solution) => solution.id === selectedSolutionId.value) ||
      solutions.value.at(-1) ||
      null,
  );
  const generationReady = computed(
    () =>
      canSolve.value &&
      templateIds.value.every((id) =>
        templates.value.some(
          (template) =>
            template.source.id === id &&
            snapshot.value?.template_readings?.some(
              (reading) =>
                reading.template_id === id &&
                reading.reviewed &&
                reading.sha256 === template.source.sha256,
            ),
        ),
      ) &&
      (planTone.value !== 'custom' || !!customPrompt.value.trim()),
  );
  const selected = ref<string[]>([]),
    operations = ref(new Set<string>()),
    notifications = useToasts();
  const applicationTasks = useApplicationJobs(notifyJob),
    allJobs = applicationTasks.jobs,
    jobs = computed(() => allJobs.value.filter((job) => job.project_id === project.value?.id));
  let generation = 0,
    uploadRevision = 0,
    inputSequence = 0,
    readingSequence = 0,
    polling: ReturnType<typeof setInterval> | undefined,
    pollInFlight = false,
    knownDocuments = new Set<string>(),
    readVersion = 0;
  const base = () => `/api/projects/${project.value?.id}`;
  const currentStep = computed(() => Number(router.currentRoute.value.params.step) || 1);
  const reviewTab = computed(() =>
    ['facts', 'rules', 'issues'].includes(String(router.currentRoute.value.query.review))
      ? String(router.currentRoute.value.query.review)
      : 'issues',
  );
  const issues = computed(() =>
    distinctIssues(
      [
        ...(snapshot.value?.reading.issues.filter((issue) =>
          issue.code.startsWith('validation.'),
        ) || []),
        ...(editable.value?.issues || []),
      ].filter(
        (issue) =>
          issue.code !== 'validation.rule_unimplemented' &&
          !issue.code.startsWith('review.resolved.'),
      ),
    ),
  );
  const errors = computed(
    () => snapshot.value?.reading.issues.filter((issue) => issue.severity === 'error').length || 0,
  );
  const horizon = computed(
    () =>
      !!(snapshot.value?.reading.problem.years && snapshot.value.reading.problem.periods_per_year),
  );
  const activeRead = computed(() =>
    jobs.value.some((job) => job.kind === 'read' && ['queued', 'running'].includes(job.status)),
  );
  const activeSolve = computed(() =>
    jobs.value.some(
      (job) =>
        job.kind === 'solve' && (['queued', 'running'].includes(job.status) || !!job.intervention),
    ),
  );
  const inputChanged = computed(
    () =>
      inputsDirty.value ||
      (snapshot.value && inputs.value.revision !== (snapshot.value.inputs?.revision || 0)),
  );
  const canRead = computed(
    () =>
      !!project.value &&
      !!status.value?.llm_configured &&
      !!(selected.value.length || inputs.value.problem_text.trim()) &&
      !activeRead.value &&
      !loading.value,
  );
  const canSolve = computed(
    () =>
      !!snapshot.value &&
      !readingDirty.value &&
      !inputChanged.value &&
      !errors.value &&
      horizon.value &&
      !activeSolve.value &&
      !!status.value?.llm_configured,
  );
  const unsaved = computed(() => inputsDirty.value || readingDirty.value);
  function notify(text: string, kind = 'info') {
    notifications.notify(text, kind);
  }
  async function run(key: string, operation: () => Promise<unknown>) {
    if (operations.value.has(key)) return;
    operations.value.add(key);
    try {
      await operation();
    } catch (error) {
      notify(error instanceof Error ? error.message : '操作失败。', 'error');
    } finally {
      operations.value.delete(key);
    }
  }
  async function loadStatus() {
    const [value, presets] = await Promise.all([
      request<Status>('/api/status'),
      request<ModelCatalog>('/api/model-presets'),
    ]);
    status.value = value;
    catalog.value = presets;
  }
  function navigate(operation: () => void) {
    if (unsaved.value && project.value) pendingNavigation.value = operation;
    else operation();
  }
  function discardAndNavigate() {
    const action = pendingNavigation.value;
    pendingNavigation.value = null;
    inputsDirty.value = readingDirty.value = false;
    action?.();
  }
  function goProjects() {
    navigate(() => {
      void router.push('/projects');
    });
  }
  function goTemplateLibrary() {
    navigate(() => {
      void router.push('/templates');
    });
  }
  function setStep(step: number) {
    if (project.value)
      void router.replace({
        name: 'workflow',
        params: { projectId: project.value.id, step },
        query: { review: reviewTab.value },
      });
  }
  function setReviewTab(tab: string) {
    void router.replace({ query: { ...router.currentRoute.value.query, review: tab } });
  }
  function acceptReading(value: Snapshot) {
    snapshot.value = value;
    editable.value = {
      title: value.reading.problem.title,
      facts: cloneJson(value.reading.problem.facts),
      rules: cloneJson(value.reading.rules.rules),
      issues: cloneJson(
        value.reading.issues.filter((issue) => !issue.code.startsWith('validation.')),
      ),
    };
    readingDirty.value = false;
  }
  function markInputsDirty() {
    inputsDirty.value = true;
    inputSequence++;
  }
  function markReadingDirty() {
    readingDirty.value = true;
    readingSequence++;
  }
  function annotation(id: string) {
    return inputs.value.annotations.find((item) => item.document_id === id)?.text || '';
  }
  function setAnnotation(id: string, text: string) {
    inputs.value.annotations = inputs.value.annotations.filter((item) => item.document_id !== id);
    if (text.trim()) inputs.value.annotations.push({ document_id: id, text });
    markInputsDirty();
  }
  async function saveInputs() {
    if (!inputsDirty.value) return;
    const captured = generation,
      sequence = inputSequence,
      saved = await post<ReadingInputs>(base() + '/inputs', cloneJson(inputs.value));
    if (captured !== generation) return;
    inputs.value.revision = saved.revision;
    if (sequence === inputSequence) {
      inputs.value = saved;
      inputsDirty.value = false;
    } else throw new Error('保存期间输入有变化，请再次保存。');
  }
  async function saveReading() {
    if (!snapshot.value || !editable.value) return;
    const captured = generation,
      sequence = readingSequence;
    const saved = await post<Snapshot>(base() + '/revisions', {
      expected_version: snapshot.value.version,
      note: '默认',
      reading: cloneJson(editable.value),
    });
    if (captured !== generation) return;
    if (sequence !== readingSequence) {
      snapshot.value = saved;
      throw new Error('保存期间数据有变化，请再次保存。');
    }
    acceptReading(saved);
    notify(
      errors.value ? '已保存，仍有错误需要处理。' : '已保存，可继续生成草稿。',
      errors.value ? 'info' : 'success',
    );
  }
  async function continueToSolve() {
    if (readingDirty.value) await saveReading();
    if (errors.value || !horizon.value) {
      setReviewTab('issues');
      notify('请先修正题目中的错误或周期冲突。');
    } else setStep(3);
  }
  async function startRead(append = !!snapshot.value, documentIds = selected.value) {
    if (readingDirty.value) throw new Error('请先保存读题修改。');
    const captured = generation;
    await saveInputs();
    if (captured !== generation) return;
    await post(base() + '/read', {
      document_ids: documentIds,
      append,
      input_revision: inputs.value.revision,
    });
    if (captured === generation) {
      setStep(2);
      await refresh();
    }
  }
  async function upload(files: File[]) {
    if (!project.value) return;
    const captured = generation,
      url = base();
    for (const file of files) {
      if (!/\.(docx|doc|pdf|xls|xlsx)$/i.test(file.name))
        throw new Error('请选择 DOCX、DOC、PDF、XLS 或 XLSX 文件。');
      if (file.size > (status.value?.max_upload_mb || 20) * 1024 * 1024)
        throw new Error(`${file.name} 超过大小限制。`);
      await request(url + '/documents', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Filename': encodeURIComponent(file.name),
        },
        body: file,
      });
    }
    if (captured === generation) {
      notify('文件已提交解析。');
      await refresh();
    }
  }
  async function importLibraryTemplate(templateId: string) {
    if (!project.value) throw new Error('请先打开项目。');
    if (readingDirty.value || inputChanged.value)
      throw new Error('请先保存并读取题目修改，再导入空表。');
    const captured = generation;
    const result = await post<{ template_id: string; version: number | null }>(
      base() + '/templates/import',
      { template_id: templateId },
    );
    if (captured !== generation) return;
    await refresh();
    if (result.version) {
      const value = await request<{ snapshot: Snapshot }>(base() + '/problem');
      if (captured !== generation) return;
      acceptReading(value.snapshot);
    }
    if (!templateIds.value.includes(result.template_id) && templateIds.value.length < 3)
      templateIds.value.push(result.template_id);
    notify('已导入空表及确认解读，无需重新读取。', 'success');
  }
  async function deleteUpload(kind: 'document' | 'template', id: string) {
    const captured = generation,
      sequence = inputSequence;
    const result = await request<{ inputs: ReadingInputs | null }>(
      `${base()}/${kind === 'document' ? 'documents' : 'templates'}/${id}`,
      { method: 'DELETE' },
    );
    if (captured !== generation) return;
    uploadRevision++;
    if (kind === 'document') {
      documents.value = documents.value.filter((doc) => doc.source.id !== id);
      selected.value = selected.value.filter((value) => value !== id);
      knownDocuments.delete(id);
      inputs.value.annotations = inputs.value.annotations.filter((item) => item.document_id !== id);
      if (result.inputs) inputs.value.revision = result.inputs.revision;
      if (sequence !== inputSequence) inputsDirty.value = true;
      inputSequence++;
    } else {
      templates.value = templates.value.filter((template) => template.source.id !== id);
      templateIds.value = templateIds.value.filter((value) => value !== id);
    }
    await refresh();
    notify('文件已删除。', 'success');
  }
  function selectSolution(id: string) {
    selectedSolutionId.value = id;
    void router.replace({ query: { ...router.currentRoute.value.query, solution: id } });
    const config = solutions.value.find((solution) => solution.id === id)?.generation;
    planTone.value = config?.tone || 'balanced';
    customPrompt.value = config?.custom_prompt || '';
    templateIds.value = (config?.template_ids || []).filter((id) =>
      templates.value.some((template) => template.source.id === id),
    );
  }
  async function solve(previousSolutionId: string | null = null, revisionPrompt = '') {
    if (!generationReady.value) throw new Error('请检查题目、预算表解析状态和方案基调。');
    if (previousSolutionId && !revisionPrompt.trim())
      throw new Error('请填写对已有方案的修改要求。');
    const captured = generation,
      url = base();
    const config: GenerationConfig = {
      tone: planTone.value,
      custom_prompt: planTone.value === 'custom' ? customPrompt.value.trim() : '',
      template_ids: templateIds.value,
      previous_solution_id: previousSolutionId,
      revision_prompt: revisionPrompt.trim(),
    };
    await post(url + '/solve', {
      version: snapshot.value?.version,
      generation: config,
    });
    if (captured !== generation) return;
    await refresh();
  }
  async function retryBudget() {
    const solution = currentSolution.value;
    if (!solution) throw new Error('请先生成行动方案。');
    await post(base() + '/budgets', { solution_id: solution.id });
    await refresh();
  }
  async function cancelJob(job: Job) {
    await post(`/api/jobs/${job.id}/cancel`, {});
    await refresh();
  }
  function openResolution(job: Job) {
    resolutionJobId.value = job.id;
  }
  async function answerJob(job: Job, questionId: string, option: string, text: string) {
    await post(`/api/jobs/${job.id}/answer`, { question_id: questionId, option, text });
    await refresh();
  }
  async function analyzeTemplate(templateId: string) {
    if (!currentSolution.value) throw new Error('请先生成方案。');
    await post(base() + '/mappings', {
      solution_id: currentSolution.value.id,
      template_id: templateId,
    });
    await refresh();
  }
  async function confirmMapping(mapping: WorkbookMapping, entries: WorkbookEntry[]) {
    const captured = generation;
    const saved = await post<WorkbookMapping>(`${base()}/mappings/${mapping.id}`, {
      expected_revision: mapping.revision,
      entries: cloneJson(entries),
    });
    if (captured !== generation) return;
    mappings.value = mappings.value.map((item) => (item.id === saved.id ? saved : item));
    notify('映射已确认，可填写预算表。', 'success');
  }
  async function exportArtifact(kind: 'docx' | 'xlsx', mappingId: string | null = null) {
    if (!currentSolution.value) throw new Error('请先生成方案。');
    await post(base() + '/artifacts', {
      solution_id: currentSolution.value.id,
      kind,
      mapping_id: mappingId,
    });
    await refresh();
  }
  async function retryJob(job: Job) {
    if (job.project_id === project.value?.id && job.kind === 'read') await saveInputs();
    await post(`/api/jobs/${job.id}/retry`, {});
    await refresh();
  }
  function notifyJob(job: Job) {
    if (job.intervention) {
      notifications.notify(
        `${job.scope_name || '项目'} · 方案生成等待你的处理。`,
        'info',
        () => openResolution(job),
        '处理问题',
      );
      return;
    }
    const library = job.project_id === 'template-library';
    const targetId = job.project_id;
    const targetStep =
      job.kind === 'parse'
        ? 1
        : job.kind === 'read'
          ? 2
          : ['mapping', 'artifact'].includes(job.kind)
            ? 4
            : 3;
    const name = library
      ? job.kind === 'template'
        ? '空表解析'
        : '空表解读'
      : job.kind === 'parse'
        ? '文件解析'
        : job.kind === 'mapping'
          ? '预算表映射'
          : job.kind === 'budget'
            ? '预算处理'
            : job.kind === 'artifact'
              ? '文件生成'
              : job.kind === 'read'
                ? '读题'
                : '方案生成';
    const text =
      job.status === 'failed'
        ? `${name}失败：${(job.error?.message || '请查看任务详情。').slice(0, 160)}`
        : job.status === 'needs_input'
          ? `${name}已完成，有内容需要核对。`
          : `${name}${job.status === 'cancelled' ? '已取消' : '已完成'}。`;
    const showResult = ['succeeded', 'needs_input'].includes(job.status);
    notifications.notify(
      `${job.scope_name ? job.scope_name + ' · ' : ''}${text}`,
      job.status === 'failed' ? 'error' : job.status === 'succeeded' ? 'success' : 'info',
      () => {
        if (!showResult) {
          taskOpen.value = true;
          return;
        }
        if (library) goTemplateLibrary();
        else if (targetId)
          navigate(() => {
            void router.push({
              name: 'workflow',
              params: { projectId: targetId, step: targetStep },
            });
          });
      },
      showResult ? '查看结果' : '查看任务',
    );
  }
  async function refreshTasks() {
    if (authenticated.value) await applicationTasks.refresh();
  }
  async function refresh(initial = false) {
    if (!authenticated.value) return;
    const captured = generation,
      capturedUploads = uploadRevision;
    await refreshTasks();
    if (!authenticated.value || !project.value || captured !== generation) return;
    const url = base(),
      latestJobs = jobs.value;
    const [docs, plans, budgetTemplates, latestMappings, latestArtifacts] = await Promise.all([
      request<ParsedDocument[]>(url + '/documents'),
      request<Solution[]>(url + '/solutions'),
      request<BudgetTemplate[]>(url + '/templates'),
      request<WorkbookMapping[]>(url + '/mappings'),
      request<Artifact[]>(url + '/artifacts'),
    ]);
    if (captured !== generation || capturedUploads !== uploadRevision) return;
    documents.value = docs;
    const newPlan = plans.at(-1);
    const planChanged = newPlan && !solutions.value.some((solution) => solution.id === newPlan.id);
    solutions.value = plans;
    templates.value = budgetTemplates;
    mappings.value = latestMappings;
    artifacts.value = latestArtifacts;
    if (initial && newPlan) {
      const remembered = String(router.currentRoute.value.query.solution || '');
      selectSolution(plans.some((plan) => plan.id === remembered) ? remembered : newPlan.id);
    } else if (planChanged && !selectedSolutionId.value) selectSolution(newPlan.id);
    if (initial && !newPlan)
      templateIds.value = budgetTemplates.slice(-3).map((template) => template.source.id);
    for (const doc of docs)
      if (!knownDocuments.has(doc.source.id)) {
        knownDocuments.add(doc.source.id);
        selected.value.push(doc.source.id);
      }
    const completed = Math.max(
      0,
      ...latestJobs
        .filter((job) => job.kind === 'read' && ['succeeded', 'needs_input'].includes(job.status))
        .map((job) => job.result?.version || 0),
    );
    if (initial || completed > readVersion) {
      readVersion = completed;
      if (readingDirty.value) {
        notify('新读题结果已保存；请先处理未保存修改。');
        return;
      }
      try {
        const result = await request<{ snapshot: Snapshot }>(url + '/problem');
        if (captured === generation) acceptReading(result.snapshot);
      } catch (error) {
        if (!(error instanceof ApiError) || error.code !== 'snapshot_not_found') throw error;
      }
    }
  }
  function resetProject() {
    generation++;
    loading.value = false;
    inputsDirty.value = readingDirty.value = false;
    snapshot.value = editable.value = null;
    inputs.value = emptyInputs();
    documents.value = [];
    solutions.value = [];
    templates.value = [];
    mappings.value = [];
    artifacts.value = [];
    templateIds.value = [];
    selectedSolutionId.value = null;
    planTone.value = 'balanced';
    customPrompt.value = '';
    selected.value = [];
    knownDocuments = new Set();
    readVersion = 0;
    project.value = null;
  }
  async function syncRoute() {
    if (!authenticated.value) return;
    const route = router.currentRoute.value,
      id = String(route.params.projectId || '');
    if (id === project.value?.id) return;
    resetProject();
    const captured = generation;
    project.value = projects.value.find((item) => item.id === id) || null;
    if (!id) {
      const values = await request<Project[]>('/api/projects');
      if (captured === generation) projects.value = values;
      return;
    }
    if (!project.value) {
      notify('项目不存在或已删除。', 'error');
      await router.replace('/projects');
      return;
    }
    loading.value = true;
    try {
      const values = await request<ReadingInputs>(base() + '/inputs');
      if (captured !== generation) return;
      inputs.value = values;
      await refresh(true);
    } finally {
      if (captured === generation) loading.value = false;
    }
  }
  async function enterApp() {
    authenticated.value = true;
    await loadStatus();
    projects.value = await request<Project[]>('/api/projects');
    await refreshTasks();
    await syncRoute();
  }
  async function initialize() {
    try {
      const session = await request<{ authenticated: boolean; configured: boolean }>(
        '/api/auth/session',
      );
      if (session.authenticated) await enterApp();
      else if (!session.configured) notify('请在服务器 .env 中配置 ERP_ACCESS_KEY。', 'error');
    } finally {
      initializing.value = false;
    }
  }
  async function login(key: string, remember: boolean) {
    await post('/api/auth/login', { key, remember });
    await enterApp();
  }
  function expire() {
    authenticated.value = false;
    resetProject();
    applicationTasks.clear();
    taskOpen.value = false;
    projects.value = [];
    status.value = catalog.value = null;
    pendingNavigation.value = null;
  }
  async function logout() {
    await post('/api/auth/logout', {});
    expire();
    inputsDirty.value = readingDirty.value = false;
  }
  async function createProject(name: string) {
    const value = await post<Project>('/api/projects', { name });
    projects.value.push(value);
    await router.push({ name: 'workflow', params: { projectId: value.id, step: 1 } });
  }
  async function deleteProject(value: Project) {
    await request(`/api/projects/${value.id}`, { method: 'DELETE' });
    projects.value = projects.value.filter((item) => item.id !== value.id);
    if (project.value?.id === value.id) {
      inputsDirty.value = readingDirty.value = false;
      await router.push('/projects');
    }
    notify('项目已删除。', 'success');
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (unsaved.value) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
  const removeGuard = router.beforeEach((to) => {
    if (project.value && unsaved.value && String(to.params.projectId || '') !== project.value.id) {
      pendingNavigation.value = () => {
        void router.push(to);
      };
      return false;
    }
  });
  function start() {
    window.addEventListener('erp-session-expired', expire);
    window.addEventListener('beforeunload', beforeUnload);
    polling = setInterval(() => {
      if (pollInFlight || !authenticated.value) return;
      pollInFlight = true;
      void refresh()
        .catch((error) => notify(error.message, 'error'))
        .finally(() => {
          pollInFlight = false;
        });
    }, 2000);
  }
  function dispose() {
    clearInterval(polling);
    generation++;
    applicationTasks.clear();
    notifications.dispose();
    removeGuard();
    window.removeEventListener('erp-session-expired', expire);
    window.removeEventListener('beforeunload', beforeUnload);
  }
  return {
    authenticated,
    initializing,
    loading,
    projects,
    project,
    status,
    catalog,
    documents,
    jobs,
    allJobs,
    refreshTasks,
    solutions,
    snapshot,
    editable,
    inputs,
    inputsDirty,
    readingDirty,
    taskOpen,
    resolutionJobId,
    openResolution,
    answerJob,
    pendingNavigation,
    planTone,
    customPrompt,
    templates,
    mappings,
    artifacts,
    analyzeTemplate,
    retryBudget,
    confirmMapping,
    exportArtifact,
    templateIds,
    generationReady,
    currentSolution,
    selectedSolutionId,
    selected,
    operations,
    notifications,
    currentStep,
    reviewTab,
    issues,
    errors,
    horizon,
    activeRead,
    activeSolve,
    inputChanged,
    canRead,
    canSolve,
    unsaved,
    run,
    notify,
    loadStatus,
    navigate,
    discardAndNavigate,
    goProjects,
    goTemplateLibrary,
    setStep,
    setReviewTab,
    annotation,
    setAnnotation,
    markInputsDirty,
    markReadingDirty,
    saveInputs,
    saveReading,
    continueToSolve,
    startRead,
    upload,
    solve,
    importLibraryTemplate,
    deleteUpload,
    selectSolution,
    cancelJob,
    retryJob,
    refresh,
    syncRoute,
    initialize,
    login,
    logout,
    createProject,
    deleteProject,
    start,
    dispose,
  };
}
