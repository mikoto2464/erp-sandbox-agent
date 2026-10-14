from dataclasses import replace

from erp_agent.application.contracts import dumps, loads, obj, to_json
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl


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
        material = obj(loads(request.user))
        material["shared_alignment"] = self.memory.alignment()
        material["shared_memory"] = to_json(self.memory.recall(self.query, 1800))
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
        if task and len(dumps(task)) <= 2000:
            material["task_memory"] = task
        system = (
            request.system
            + "\n共享记忆仅辅助名称和口径一致，当前版本规范题目/方案与本次要求优先。"
            + "记忆不能替代数值或原文来源。"
        )
        return replace(request, system=system, user=dumps(material))
