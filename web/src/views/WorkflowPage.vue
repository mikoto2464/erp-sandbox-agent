<script setup lang="ts">
import { useWorkspaceContext } from '../composables/context';
import type { Project } from '../lib/types';
import UploadStep from './UploadStep.vue';
import ReviewStep from './ReviewStep.vue';
import SolverStep from './SolverStep.vue';
import FutureStep from './FutureStep.vue';
const { project, loading, currentStep } = useWorkspaceContext();
defineEmits<{ delete: [project: Project]; settings: [] }>();
</script>
<template>
  <section v-if="project">
    <div class="flow-heading">
      <div>
        <h1>{{ project.name }}</h1>
      </div>
      <button
        class="quiet danger-text"
        @click="$emit('delete', project)"
      >
        删除项目
      </button>
    </div>
    <div
      v-if="loading"
      class="card empty"
    >
      <span class="spinner" />正在加载项目…
    </div>
    <div
      v-else
      class="step-panel"
    >
      <UploadStep
        v-show="currentStep === 1"
        @settings="$emit('settings')"
      /><ReviewStep v-show="currentStep === 2" /><SolverStep
        v-show="currentStep === 3"
      /><FutureStep v-show="currentStep >= 4" />
    </div>
  </section>
</template>
