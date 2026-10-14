import hashlib
from dataclasses import replace
from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.codec import workbook_entries
from erp_agent.application.contracts import JSONObject, array, dumps, loads, obj, to_json
from erp_agent.application.llm_recovery import TRANSIENT_ERRORS, recovery_call
from erp_agent.application.mapping_sources import validate_entry_sources
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    BudgetTemplate,
    Issue,
    ProblemSnapshot,
    SolutionBundle,
    WorkbookEntry,
)
from erp_agent.domain.workbook import validate_mapping


class WorkbookMappingTasks:
    def __init__(self, memory: RoleMemory | None = None) -> None:
        self.memory = memory

    def run(
        self,
        units: list[tuple[TaskContext, JSONObject]],
        solution: SolutionBundle,
        template: BudgetTemplate,
        fixed: tuple[WorkbookEntry, ...],
        llm: LLMClient,
        control: RunControl,
        snapshot: ProblemSnapshot | None,
        mapping_schema: JSONObject,
        prompt_version: str,
        batch_chars: int,
    ) -> tuple[tuple[WorkbookEntry, ...], tuple[str, ...]]:
        if not units:
            return (), ()
        prompt = files("erp_agent").joinpath("prompts/mapper.txt").read_text(encoding="utf-8")
        schema = dumps(mapping_schema)
        capacity = getattr(llm, "max_input_chars", 100000)

        def request_for(task: TaskContext, material: JSONObject) -> LLMRequest:
            return LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps(material),
                "erp_workbook_mapping",
                schema,
                max_output_tokens=4000,
                retries=0,
                task=task,
                reasoning_reserve_tokens=8192,
                stream=True,
            )

        def key_for(material: JSONObject) -> str:
            return (
                "mapper-unit:"
                + hashlib.sha256(
                    dumps(
                        {
                            "version": prompt_version,
                            "material": material,
                            "prompt": prompt,
                            "schema": schema,
                        }
                    ).encode()
                ).hexdigest()
            )

        def children(
            task: TaskContext, material: JSONObject
        ) -> tuple[tuple[TaskContext, JSONObject], ...]:
            items = array(material["planned_inputs"])
            rows = sorted(
                {
                    (cell_position(str(obj(obj(item)["item"])["cell"])) or (0, 0))[0]
                    for item in items
                }
            )
            if len(rows) < 2:
                return ()
            middle = rows[(len(rows) - 1) // 2]
            parts = [
                [
                    item
                    for item in items
                    if ((cell_position(str(obj(obj(item)["item"])["cell"])) or (0, 0))[0] <= middle)
                    == first
                ]
                for first in (True, False)
            ]
            result = []
            for index, part in enumerate(parts):
                used = {
                    source for item in part for source in array(obj(item)["candidate_source_ids"])
                }
                sliced = {
                    **material,
                    "planned_inputs": part,
                    "sources": [
                        source for source in array(material["sources"]) if obj(source)["id"] in used
                    ],
                }
                result.append(
                    (
                        TaskContext(
                            f"{task.id}:{index}",
                            "Answerer",
                            task.role,
                            task.object + f" · 输入区域 {index + 1}",
                            task.id,
                        ),
                        sliced,
                    )
                )
            return tuple(result)

        def decode(material: JSONObject, payload: JSONObject) -> tuple[WorkbookEntry, ...]:
            entries = workbook_entries(obj(payload, {"entries"})["entries"])
            planned = {
                (str(obj(obj(value)["item"])["sheet"]), str(obj(obj(value)["item"])["cell"])): obj(
                    value
                )
                for value in array(material["planned_inputs"])
            }
            seen = set()
            result = []
            for entry in entries:
                target = (entry.sheet, entry.cell)
                if target not in planned or target in seen:
                    raise ERPError(
                        "mapping_unplanned",
                        "只能核对当前未确定预算项，不能增加、重复或更改填写位置。",
                    )
                seen.add(target)
                item = planned[target]
                validate_entry_sources(entry, item, material)
                result.append(replace(entry, label=str(obj(item["item"])["label"])))
            replacements = {(entry.sheet, entry.cell): entry for entry in result}
            merged = tuple(replacements.get((entry.sheet, entry.cell), entry) for entry in fixed)
            self.raise_errors(validate_mapping(template, merged, solution, snapshot))
            return tuple(result)

        def run(
            unit: tuple[TaskContext, JSONObject], depth: int = 0
        ) -> tuple[tuple[WorkbookEntry, ...], str]:
            task, material = unit
            key = key_for(material)
            saved = self.memory.get(key) if self.memory else None
            if saved and "result" in saved:
                result = decode(material, obj(saved["result"]))
                report_task(control, task, "cached", entry_count=len(result))
                return result, str(saved.get("model", ""))
            parts = children(task, material) if depth < 6 else ()
            request = request_for(task, material)

            def split() -> tuple[tuple[WorkbookEntry, ...], str]:
                if self.memory:
                    self.memory.put(key, {"split": True})
                report_task(
                    control, task, "split", child_count=len(parts), stage="splitting_mapping"
                )
                results = []
                for part in parts:
                    report_task(control, part[0], "queued")
                    results.append(run(part, depth + 1))
                result = tuple(entry for entries, _ in results for entry in entries)
                model = "、".join(dict.fromkeys(model for _, model in results if model))
                if self.memory:
                    self.memory.put(key, {"result": {"entries": to_json(result)}, "model": model})
                report_task(control, task, "succeeded", entry_count=len(result), model=model)
                return result, model

            if parts and (
                (saved and saved.get("split"))
                or len(request.user) > batch_chars
                or len(request.system) + len(request.user) + len(schema) > capacity
            ):
                return split()
            client = TaskLLMClient(llm, control, task)
            payload = material
            for attempt in range(2):
                response = None
                try:
                    control.checkpoint("mapping_template")
                    request = request_for(task, payload)
                    if len(request.system) + len(request.user) + len(schema) > capacity:
                        raise ERPError("llm_input_limit", "填写来源核对任务超出模型输入上限。", 413)
                    response = recovery_call(client, request, control)
                    result = decode(material, obj(loads(normalize_model_json(response.content))))
                    control.checkpoint("saving_mapping_progress")
                    if self.memory:
                        self.memory.put(
                            key, {"result": {"entries": to_json(result)}, "model": response.model}
                        )
                    report_task(
                        control, task, "succeeded", entry_count=len(result), model=response.model
                    )
                    return result, response.model
                except Cancelled:
                    raise
                except ERPError as error:
                    if parts and error.code in TRANSIENT_ERRORS | {
                        "llm_truncated",
                        "llm_incomplete",
                        "llm_input_limit",
                    }:
                        return split()
                    if attempt == 0 and (
                        response is not None or error.code == "llm_structured_response"
                    ):
                        payload = {
                            **material,
                            "error": error.message,
                            "instruction": "仅修复JSON或引用；无法判断的预算项保持空格并具体说明。",
                        }
                        continue
                    report_task(control, task, "failed", error=error.message)
                    raise ERPError(
                        error.code,
                        f"{task.object} · {task.role}：{error.message}"
                        " 已保存完成项，重试可继续未完成的来源核对。",
                        error.status,
                    ) from error
            raise ERPError("mapping_output_invalid", "填写来源核对失败。", 502)

        for task, _ in units:
            report_task(control, task, "queued")
        planner = getattr(llm, "plan_output", None) or getattr(llm, "plan_solver", None)
        if planner:
            requests = tuple(
                request
                for task, material in units
                for request in (request_for(task, material),)
                if not (self.memory and (self.memory.get(key_for(material)) or {}).get("result"))
                and len(dumps(material)) <= batch_chars
                and len(request.system) + len(request.user) + len(schema) <= capacity
            )
            if requests:
                planner(requests, control, 2)
        results = RunnableLambda(run).batch(
            units, config={"max_concurrency": 2}, return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        completed = [result for result in results if not isinstance(result, Exception)]
        return tuple(entry for entries, _ in completed for entry in entries), tuple(
            model for _, model in completed if model
        )

    @staticmethod
    def raise_errors(issues: tuple[Issue, ...]) -> None:
        errors = [issue.message for issue in issues if issue.severity == "error"]
        if errors:
            raise ERPError("mapping_invalid", "；".join(errors))
