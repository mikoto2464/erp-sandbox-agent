<script setup lang="ts">
defineProps<{ filename: string; size?: number; metadata?: string }>();
</script>
<template>
  <article
    class="file-entry"
    :aria-label="filename"
  >
    <div class="file-row">
      <slot name="selection" />
      <span class="file-info">
        {{ filename }}
        <small v-if="size != null || metadata">
          {{ size != null ? `${Math.ceil(size / 1024)} KB` : '' }}
          {{ size != null && metadata ? ' · ' : '' }}{{ metadata }}
        </small>
      </span>
      <div class="file-actions"><slot name="actions" /></div>
    </div>
    <slot />
  </article>
</template>
