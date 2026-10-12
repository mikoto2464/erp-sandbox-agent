<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue';
import { RouterView, useRouter } from 'vue-router';
import { useWorkspace } from './composables/useWorkspace';
import { workspaceKey } from './composables/context';
import AppModal from './components/AppModal.vue';
import AppSelect from './components/AppSelect.vue';
import ToastHost from './components/ToastHost.vue';
import TaskDrawer from './components/TaskDrawer.vue';
import ModelSettings from './components/ModelSettings.vue';
import type { Project } from './lib/types';
const router = useRouter(),
  workspace = useWorkspace(router);
provide(workspaceKey, workspace);
const {
  authenticated,
  initializing,
  project,
  currentStep,
  catalog,
  jobs,
  errors,
  horizon,
  snapshot,
  readingDirty,
  run,
  operations,
  taskOpen,
  pendingNavigation,
  goProjects,
  setStep,
  discardAndNavigate,
} = workspace;
const key = ref(''),
  remember = ref(true),
  loginError = ref(''),
  creating = ref(false),
  projectName = ref(''),
  deleting = ref<Project | null>(null),
  settings = ref<InstanceType<typeof ModelSettings>>();
const modelOptions = computed(
  () =>
    catalog.value?.presets.map((preset) => ({
      value: preset.id,
      label: preset.name,
      disabled: !preset.configured && preset.id !== catalog.value!.active_id,
    })) || [],
);
const running = computed(
  () => jobs.value.filter((job) => ['queued', 'running'].includes(job.status)).length,
);
const steps = ['上传题目', '确认题目', '经营方案', '方案与表格', '方案答疑'];
watch(
  () => router.currentRoute.value.params.projectId,
  () => {
    void workspace.syncRoute().catch((error: Error) => workspace.notify(error.message, 'error'));
    creating.value = false;
    deleting.value = null;
  },
);
async function login() {
  loginError.value = '';
  await run('login', async () => {
    try {
      await workspace.login(key.value, remember.value);
      key.value = '';
    } catch (error) {
      loginError.value = error instanceof Error ? error.message : '登录失败';
      throw error;
    }
  });
}
onMounted(async () => {
  await router.isReady();
  workspace.start();
  await run('initialize', workspace.initialize);
});
watch(authenticated, (value) => {
  if (!value) {
    creating.value = false;
    deleting.value = null;
    pendingNavigation.value = null;
  }
});
onBeforeUnmount(workspace.dispose);
</script>
<template>
  <ToastHost />
  <div
    v-if="initializing"
    class="login-view"
  >
    <span class="spinner" />
  </div>
  <section
    v-else-if="!authenticated"
    class="login-view"
  >
    <form
      class="login-card"
      @submit.prevent="login"
    >
      <h1>登录</h1>
      <label for="access-key">访问密钥</label
      ><input
        id="access-key"
        v-model="key"
        type="password"
        autocomplete="current-password"
        maxlength="2048"
        required
        autofocus
      /><label class="check"
        ><input
          v-model="remember"
          type="checkbox"
        />记住此浏览器 · 30 天</label
      >
      <p
        v-if="loginError"
        class="error"
        role="alert"
      >
        {{ loginError }}
      </p>
      <button
        class="primary full"
        :disabled="operations.has('login')"
      >
        进入系统
      </button>
    </form>
  </section>
  <div v-else>
    <header class="topbar">
      <div class="topbar-main">
        <button
          class="quiet nav-home"
          @click="goProjects"
        >
          项目列表
        </button>
        <div class="topbar-actions">
          <AppSelect
            :model-value="catalog?.active_id || ''"
            label="当前模型预设"
            :options="modelOptions"
            :disabled="operations.has('preset')"
            @update:model-value="settings?.activate($event)"
          /><button
            class="quiet"
            @click="settings?.show()"
          >
            模型预设</button
          ><button
            v-if="project"
            class="quiet task-toggle"
            :class="{ 'has-running': running }"
            @click="taskOpen = true"
          >
            任务<span
              v-if="running"
              class="task-badge"
              >{{ running }}</span
            ></button
          ><button
            class="quiet"
            @click="
              workspace.navigate(() => {
                void run('logout', workspace.logout);
              })
            "
          >
            退出
          </button>
        </div>
      </div>
      <nav
        v-if="project"
        class="steps"
        aria-label="操作步骤"
      >
        <button
          v-for="(title, index) in steps"
          :key="title"
          class="step"
          :class="{
            active: currentStep === index + 1,
            complete:
              (index === 0 && workspace.documents.value.length > 0) ||
              (index === 1 && snapshot && !errors && horizon && !readingDirty),
          }"
          :aria-current="currentStep === index + 1 ? 'step' : undefined"
          @click="setStep(index + 1)"
        >
          <span class="step-number">{{ index + 1 }}</span
          >{{ title }}
        </button>
      </nav>
    </header>
    <main>
      <RouterView v-slot="{ Component }">
        <component
          :is="Component"
          @create="creating = true"
          @delete="deleting = $event"
          @settings="settings?.show()"
        />
      </RouterView>
    </main>
    <ModelSettings ref="settings" /><TaskDrawer />
  </div>
  <AppModal
    :open="creating"
    title="新建项目"
    @close="creating = false"
    ><form
      @submit.prevent="
        run('create', async () => {
          await workspace.createProject(projectName);
          projectName = '';
          creating = false;
        })
      "
    >
      <label
        >项目名称<input
          v-model="projectName"
          maxlength="100"
          required
          autofocus
      /></label>
      <footer class="dialog-footer">
        <button
          type="button"
          class="secondary"
          @click="creating = false"
        >
          取消</button
        ><button
          class="primary"
          :disabled="operations.has('create')"
        >
          创建
        </button>
      </footer>
    </form></AppModal
  ><AppModal
    :open="!!deleting"
    title="删除项目"
    @close="deleting = null"
    ><p>删除“{{ deleting?.name }}”及其文件、题目与方案？此操作无法撤销。</p>
    <footer class="dialog-footer">
      <button
        class="secondary"
        @click="deleting = null"
      >
        取消</button
      ><button
        class="danger"
        :disabled="operations.has('delete')"
        @click="
          run('delete', async () => {
            await workspace.deleteProject(deleting!);
            deleting = null;
          })
        "
      >
        删除
      </button>
    </footer></AppModal
  ><AppModal
    :open="!!pendingNavigation"
    title="有未保存的修改"
    @close="pendingNavigation = null"
    ><p>离开后将丢弃当前修改。</p>
    <footer class="dialog-footer">
      <button
        class="secondary"
        @click="pendingNavigation = null"
      >
        返回修改</button
      ><button
        class="danger"
        @click="discardAndNavigate"
      >
        丢弃并离开
      </button>
    </footer></AppModal
  >
</template>
