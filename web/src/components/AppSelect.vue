<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId } from 'vue';
import type { SelectOption } from '../lib/types';
const props = defineProps<{
  modelValue: string;
  options: SelectOption[];
  label: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const opened = ref(false),
  teleportTarget = ref<HTMLElement | string>('body'),
  trigger = ref<HTMLButtonElement>(),
  menu = ref<HTMLElement>(),
  position = ref<Record<string, string>>({}),
  id = useId();
const selected = computed(
  () => props.options.find((item) => item.value === props.modelValue)?.label || '请选择',
);
const enabled = computed(() => props.options.filter((item) => !item.disabled));
async function show() {
  if (props.disabled) return;
  teleportTarget.value = trigger.value?.closest('dialog') || document.body;
  opened.value = !opened.value;
  if (!opened.value) return;
  await nextTick();
  if (!opened.value || !menu.value || !trigger.value) return;
  menu.value?.showPopover();
  const rect = trigger.value!.getBoundingClientRect(),
    height = Math.min(menu.value!.scrollHeight, 260),
    width = Math.max(rect.width, 180);
  position.value = {
    left: Math.max(8, Math.min(rect.left, innerWidth - width - 8)) + 'px',
    top:
      (rect.bottom + height + 8 > innerHeight
        ? Math.max(8, rect.top - height - 6)
        : rect.bottom + 6) + 'px',
    width: width + 'px',
  };
  await nextTick();
  (
    (menu.value?.querySelector('[aria-selected="true"]:not(:disabled)') as HTMLElement) ||
    (menu.value?.querySelector('button:not(:disabled)') as HTMLElement)
  )?.focus();
}
function close() {
  opened.value = false;
}
function choose(value: string) {
  emit('update:modelValue', value);
  close();
  trigger.value?.focus();
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation();
    close();
    trigger.value?.focus();
    return;
  }
  if (event.key === 'Tab') {
    close();
    return;
  }
  const elements = [
      ...(menu.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []),
    ],
    current = elements.indexOf(document.activeElement as HTMLButtonElement);
  let next: number | undefined;
  if (event.key === 'ArrowDown') next = (current + 1) % enabled.value.length;
  if (event.key === 'ArrowUp') next = (current - 1 + enabled.value.length) % enabled.value.length;
  if (event.key === 'Home') next = 0;
  if (event.key === 'End') next = elements.length - 1;
  if (next !== undefined) {
    event.preventDefault();
    elements[next]?.focus();
  }
}
onBeforeUnmount(() => {
  if (menu.value?.matches(':popover-open')) menu.value.hidePopover();
});
</script>
<template>
  <div class="app-select">
    <button
      ref="trigger"
      type="button"
      class="select-trigger"
      :disabled="disabled"
      :aria-label="label"
      aria-haspopup="listbox"
      :aria-expanded="opened"
      :aria-controls="id"
      @click="show"
      @keydown.down.prevent="show"
    >
      {{ selected }}<span aria-hidden="true">⌄</span>
    </button>
    <Teleport :to="teleportTarget"
      ><div
        v-if="opened"
        :id="id"
        ref="menu"
        popover="auto"
        role="listbox"
        :aria-label="label"
        class="select-menu"
        :style="position"
        @toggle="opened = $event.newState === 'open'"
        @keydown="key"
      >
        <button
          v-for="option in options"
          :key="option.value"
          type="button"
          role="option"
          :aria-selected="modelValue === option.value"
          :disabled="option.disabled"
          @click="choose(option.value)"
        >
          {{ option.label
          }}<span
            v-if="modelValue === option.value"
            aria-hidden="true"
            >✓</span
          >
        </button>
      </div></Teleport
    >
  </div>
</template>
