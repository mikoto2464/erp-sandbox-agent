<script setup lang="ts">
import { useWorkspaceContext } from '../composables/context';
const { currentStep, solutions, setStep } = useWorkspaceContext();
</script>
<template>
  <section>
    <div class="section-heading">
      <div>
        <h2>{{ currentStep === 4 ? '方案与表格' : '方案答疑' }}</h2>
        <p class="muted">{{ solutions.at(-1)?.plan.title || '尚未生成经营计划' }}</p>
      </div>
      <span class="badge planned">尚未开放</span>
    </div>
    <template v-if="currentStep === 4"
      ><div class="module-grid">
        <article class="card module-card">
          <span class="module-symbol">▤</span>
          <h3>经营方案文档</h3>
          <p class="muted">整理战略、逐期操作、资金安排与风险。</p>
          <div class="module-preview">
            <span>方案概述</span><span>逐期操作计划</span><span>资金与风险说明</span>
          </div>
          <button
            class="secondary full"
            disabled
          >
            导出 DOCX · 待开发
          </button>
        </article>
        <article class="card module-card">
          <span class="module-symbol">▦</span>
          <h3>预算表填写</h3>
          <p class="muted">上传 Excel 空表，核对项目后填入经营数据。</p>
          <div class="template-placeholder">
            <strong>XLSX 空白预算表</strong
            ><button
              class="secondary"
              disabled
            >
              上传模板 · 待开发
            </button>
          </div>
          <button
            class="secondary full"
            disabled
          >
            导出 XLSX · 待开发
          </button>
        </article>
      </div>
      <div class="card validation-placeholder">
        <h3>文件核对</h3>
        <div class="placeholder-checks">
          <span>经营数据</span><span>表格对应关系</span><span>公式重算与对账</span>
        </div>
      </div></template
    >
    <div
      v-else
      class="card conversation-card"
    >
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
        ← 查看经营方案</button
      ><button
        v-if="currentStep === 4"
        class="review-action"
        @click="setStep(5)"
      >
        方案答疑 →
      </button>
    </footer>
  </section>
</template>
