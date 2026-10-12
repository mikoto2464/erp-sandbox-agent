<script setup lang="ts">
import { useWorkspaceContext } from '../composables/context';
const { currentSolution, setStep } = useWorkspaceContext();
</script>
<template>
  <section>
    <div class="section-heading">
      <div>
        <h2>方案答疑</h2>
        <p class="muted">{{ currentSolution?.plan.title || '尚未生成经营计划' }}</p>
      </div>
      <span class="badge planned">尚未开放</span>
    </div>
    <div class="card conversation-card">
      <div class="conversation-empty">
        <span class="module-symbol">◇</span>
        <h3>方案答疑</h3>
        <p class="muted">功能接入后，回答将引用当前题目与经营计划。</p>
        <div class="question-examples">
          <span>为什么选择这条产线？</span><span>这个季度需要多少现金？</span
          ><span>少借款有什么影响？</span>
        </div>
      </div>
      <div class="conversation-compose">
        <textarea
          rows="2"
          placeholder="输入关于方案的问题 · 待开发"
          aria-label="关于方案的问题"
          disabled
        /><button
          class="primary"
          disabled
        >
          发送
        </button>
      </div>
    </div>
    <footer class="panel-footer">
      <button
        class="review-action"
        @click="setStep(3)"
      >
        ← 查看经营方案
      </button>
    </footer>
  </section>
</template>
