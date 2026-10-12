<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
const props = defineProps<{ open: boolean; title: string; wide?: boolean; drawer?: boolean }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>();
const leaving = ref(false);
const headingId = useId();
let previous: HTMLElement | null = null;
let closeTimer: ReturnType<typeof setTimeout> | undefined;
function finishClose() {
  dialog.value?.close();
  leaving.value = false;
  if (previous?.isConnected) previous.focus();
}
watch(
  () => props.open,
  async () => {
    clearTimeout(closeTimer);
    await nextTick();
    if (!dialog.value) return;
    if (props.open) {
      leaving.value = false;
      if (!dialog.value.open) {
        previous = document.activeElement as HTMLElement;
        dialog.value.showModal();
      }
    } else if (dialog.value.open && !leaving.value) {
      if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) finishClose();
      else {
        leaving.value = true;
        closeTimer = setTimeout(finishClose, 130);
      }
    }
  },
  { immediate: true },
);
function outside(event: MouseEvent) {
  if (event.target !== dialog.value) return;
  const rect = dialog.value.getBoundingClientRect();
  if (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  )
    emit('close');
}
const pointerOutside = ref(false);
onBeforeUnmount(() => {
  clearTimeout(closeTimer);
  dialog.value?.close();
});
</script>
<template>
  <dialog
    ref="dialog"
    class="app-modal"
    :class="{ 'wide-modal': wide, 'drawer-modal': drawer, 'modal-leaving': leaving }"
    :aria-labelledby="headingId"
    @cancel.prevent="emit('close')"
    @pointerdown="pointerOutside = $event.target === dialog"
    @click="pointerOutside && outside($event)"
  >
    <header class="modal-heading">
      <h2 :id="headingId">{{ title }}</h2>
      <button
        type="button"
        class="icon-button"
        aria-label="关闭窗口"
        autofocus
        @click="emit('close')"
      >
        ×
      </button>
    </header>
    <div class="modal-content"><slot /></div>
  </dialog>
</template>
