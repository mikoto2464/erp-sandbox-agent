<script setup lang="ts">
import { useWorkspaceContext } from '../composables/context';
const {
  notifications: { messages, remove, pause, resume },
} = useWorkspaceContext();
</script>
<template>
  <div
    class="toast-host"
    role="status"
    aria-live="polite"
  >
    <TransitionGroup name="toast"
      ><div
        v-for="item in messages"
        :key="item.id"
        class="snackbar"
        :class="item.kind"
        @mouseenter="pause(item.id)"
        @mouseleave="resume(item.id)"
        @focusin="pause(item.id)"
        @focusout="resume(item.id)"
      >
        <span
          class="snackbar-icon"
          aria-hidden="true"
          >{{ item.kind === 'error' ? '!' : item.kind === 'success' ? '✓' : 'i' }}</span
        ><span class="snackbar-message">{{ item.text }}</span
        ><button
          v-if="item.action"
          class="snackbar-action"
          @click="
            remove(item.id);
            item.action();
          "
        >
          {{ item.actionLabel }}
        </button>
      </div></TransitionGroup
    >
  </div>
</template>
