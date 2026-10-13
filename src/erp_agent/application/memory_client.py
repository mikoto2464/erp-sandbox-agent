from dataclasses import replace

from erp_agent.application.contracts import dumps, loads, obj, to_json
from erp_agent.application.memory import RoleMemory
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse


class MemoryLLMClient:
    def __init__(self, client: LLMClient, memory: RoleMemory, query: str) -> None:
        self.client, self.memory, self.query = client, memory, query

    @property
    def configured(self) -> bool:
        return self.client.configured

    def generate(self, request: LLMRequest) -> LLMResponse:
        material = obj(loads(request.user))
        material["shared_memory"] = to_json(self.memory.recall(self.query, 1800))
        task = self.memory.get("requirements") or self.memory.get("solution")
        if task and len(dumps(task)) <= 2000:
            material["task_memory"] = task
        system = (
            request.system
            + "\n共享记忆仅辅助名称和口径一致，当前版本规范题目/方案与本次要求优先。"
            + "记忆不能替代数值或原文来源。"
        )
        response = self.client.generate(replace(request, system=system, user=dumps(material)))
        return response
