from dataclasses import replace

from erp_agent.application.contracts import JSONValue, obj, to_json
from erp_agent.application.ports import LLMClient, LLMRequest, LLMResponse, RunControl, TaskContext
from erp_agent.application.task_budget import model_call, task_budget


def report_task(control: RunControl, task: TaskContext, status: str, **metadata: JSONValue) -> None:
    report = getattr(control, "report_task", None)
    if report:
        report(task.id, {**obj(to_json(task)), "status": status, **metadata})


class TaskLLMClient:
    def __init__(self, client: LLMClient, control: RunControl, task: TaskContext) -> None:
        self.client, self.control, self.task = client, control, task

    @property
    def configured(self) -> bool:
        return self.client.configured

    @property
    def max_input_chars(self) -> int:
        return getattr(self.client, "max_input_chars", 100_000)

    @property
    def request_overhead_chars(self) -> int:
        return getattr(self.client, "request_overhead_chars", 0)

    def generate(self, request: LLMRequest) -> LLMResponse:
        if request.task is None:
            request = replace(request, task=self.task)
        controlled = getattr(self.client, "generate_controlled", None)
        if controlled:
            return controlled(request, self.control)
        report_task(self.control, self.task, "running")
        with model_call(task_budget(self.control, request.task or self.task)):
            response = self.client.generate(request)
        report_task(self.control, self.task, "running", model=response.model)
        return response

    def plan_solver(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        planner = getattr(self.client, "plan_solver", None)
        if planner:
            planner(requests, control, concurrency)

    def plan_output(
        self, requests: tuple[LLMRequest, ...], control: RunControl, concurrency: int
    ) -> None:
        planner = getattr(self.client, "plan_output", None) or getattr(
            self.client, "plan_solver", None
        )
        if planner:
            planner(requests, control, concurrency)
