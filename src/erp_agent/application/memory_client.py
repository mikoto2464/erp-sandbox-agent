from dataclasses import replace

from erp_agent.application.contracts import dumps, loads, obj, to_json
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl

ROLE_QUERIES = {
    "reader": "原文 表头 单位 实体命名 规则口径",
    "operations": "产品 原材料 生产线 工人 认证 提前期 交付",
    "finance": "initial_cash 固定费用 融资 利率 到期 税费 回款",
    "coordinator": "行动依赖 冲突 资格 交付 截止 收官",
    "budget": "输入范围 单位 期间 行动来源 未确定原因",
    "report": "关键依赖 决策依据 风险 已处理说明 收官",
}


def material_references(value: object) -> tuple[set[str], set[str], set[str]]:
    """提取已携带实体、事实与引用，避免再次注入权威数据。"""
    present: set[str] = set()
    requested: set[str] = set()
    entities: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"id", "action_id", "fact_id", "rule_id"} and isinstance(child, str):
                    present.add(child)
                elif key.endswith("_ids") or key in {"source_id", "ref", "entity_id"}:
                    values = child if isinstance(child, list) else [child]
                    for identifier in values:
                        if isinstance(identifier, str):
                            (entities if "entity" in key else requested).add(identifier)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)

    # 紧凑规范关联表的首列是正式字段 ID。
    def table_ids(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.endswith("rows") and isinstance(child, list):
                    for row in child:
                        if isinstance(row, list) and row and isinstance(row[0], str):
                            present.add(row[0])
                table_ids(child)
        elif isinstance(item, list):
            for child in item:
                table_ids(child)

    table_ids(value)
    return present, requested - present, entities


class MemoryLLMClient:
    def __init__(self, client: LLMClient, memory: RoleMemory, query: str) -> None:
        self.client, self.memory, self.query = client, memory, query

    @property
    def configured(self) -> bool:
        return self.client.configured

    @property
    def max_input_chars(self) -> int:
        return max(1000, getattr(self.client, "max_input_chars", 100_000) - 5000)

    def generate_controlled(self, request: LLMRequest, control: RunControl) -> LLMResponse:
        return self._generate(request, control)

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self._generate(request)

    def plan_solver(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        planner = getattr(self.client, "plan_solver", None)
        if planner:
            planner(tuple(self._prepare(request) for request in requests), control, concurrency)

    def plan_output(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        planner = getattr(self.client, "plan_output", None) or getattr(
            self.client, "plan_solver", None
        )
        if planner:
            planner(tuple(self._prepare(request) for request in requests), control, concurrency)

    def _generate(self, request: LLMRequest, control: RunControl | None = None) -> LLMResponse:
        prepared = self._prepare(request)
        controlled = getattr(self.client, "generate_controlled", None)
        return (
            controlled(prepared, control)
            if controlled and control
            else self.client.generate(prepared)
        )

    def _prepare(self, request: LLMRequest) -> LLMRequest:
        from erp_agent.application.model_dispatch import task_responsibility

        material = obj(loads(request.user))
        alignment = self.memory.alignment()
        material["shared_alignment"] = {
            key: value for key, value in alignment.items() if key not in material
        }
        present, requested, entities = material_references(material)
        role = task_responsibility(request)
        query = ROLE_QUERIES[role]
        if request.task:
            query += " " + request.task.object
        solution_id = material.get("solution_id")
        recalled = self.memory.recall(
            query,
            1800,
            ids=tuple(sorted(requested)),
            entity_ids=tuple(sorted(entities)),
            exclude_ids=tuple(sorted(present)),
            solution_id=str(solution_id) if solution_id else None,
            object_id=request.task.object if request.task else None,
        )
        material["shared_memory"] = to_json(recalled)
        task = self.memory.get("requirements") or self.memory.get("solution")
        if task and task.get("solution_id"):
            authoritative = self.memory.get("solution:" + str(task["solution_id"]))
            if authoritative:
                material["solution_alignment"] = {
                    "solution_id": authoritative["solution_id"],
                    "problem_version": authoritative["problem_version"],
                    "prompt_version": authoritative["prompt_version"],
                    "verified": obj(authoritative["evaluation"])["verified"],
                }
        if (
            task
            and "generation" not in material
            and "requirements" not in material
            and len(dumps(task)) <= 2000
        ):
            material["task_memory"] = task
        system = (
            request.system
            + "\n共享记忆仅辅助名称和口径一致，当前版本规范题目/方案与本次要求优先。"
            + "记忆不能替代数值或原文来源。"
            + "上下文按职责裁剪，不包含的资料不代表全题缺失；需要依据时使用正式来源 ID。"
        )
        return replace(request, system=system, user=dumps(material))
