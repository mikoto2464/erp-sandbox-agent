<script setup lang="ts">
import { useRouter } from 'vue-router';
import { useWorkspaceContext } from '../composables/context';
import type { Project } from '../lib/types';
const router = useRouter();
const { projects, navigate } = useWorkspaceContext();
defineEmits<{ create: []; delete: [project: Project] }>();
function openProject(project: Project) {
  navigate(() => {
    void router.push({ name: 'workflow', params: { projectId: project.id, step: 1 } });
  });
}
</script>
<template>
  <section>
    <div class="page-heading">
      <div>
        <p class="eyebrow">项目</p>
        <h1>我的项目</h1>
      </div>
      <button
        class="primary"
        @click="$emit('create')"
      >
        ＋ 新建项目
      </button>
    </div>
    <div class="project-grid">
      <article
        v-for="value in [...projects].reverse()"
        :key="value.id"
        class="project-card"
      >
        <div class="project-symbol">▤</div>
        <h2>{{ value.name }}</h2>
        <p class="muted">创建于 {{ new Date(value.created_at).toLocaleDateString('zh-CN') }}</p>
        <div class="card-actions">
          <button
            class="review-action"
            @click="openProject(value)"
          >
            打开项目 →</button
          ><button
            class="quiet danger-text"
            @click="$emit('delete', value)"
          >
            删除
          </button>
        </div>
      </article>
    </div>
    <div
      v-if="!projects.length"
      class="empty"
    >
      暂无项目<button
        class="review-action"
        @click="$emit('create')"
      >
        新建项目
      </button>
    </div>
  </section>
</template>
