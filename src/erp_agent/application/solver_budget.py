import hashlib
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from importlib.resources import files

from erp_agent.application.budget_context import budget_task_material
from erp_agent.application.budget_execution import (
    CheckedInput,
    addresses,
    expand_mappings,
    merge_gaps,
)
from erp_agent.application.contracts import (
    NULLABLE_TEXT,
    TEXT,
    TEXTS,
    array,
    dumps,
    loads,
    obj,
    object_schema,
    to_json,
)
from erp_agent.application.llm_recovery import TRANSIENT_ERRORS, recovery_call
from erp_agent.application.memory import RoleMemory
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, RunControl, TaskContext
from erp_agent.application.task_budget import task_budget
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.budget import BudgetResult
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import Plan, PlanningContext, ProblemSnapshot
from erp_agent.domain.workbook import output_sources

GAP_SCHEMA = object_schema(
    {"id": TEXT, "code": TEXT, "message": TEXT, "source_ids": TEXTS, "rule_id": TEXT}
)
GROUP_MAPPING_SCHEMA = object_schema(
    {
        "group_id": TEXT,
        "target_range": TEXT,
        "source_ids": TEXTS,
        "period_axis": {"type": "string", "enum": ["single", "rows", "columns"]},
        "periods": {"type": "array", "items": {"type": ["integer", "null"]}},
        "conversion": {
            "type": "string",
            "enum": ["identity", "sum", "yuan_to_wan", "wan_to_yuan", "ratio_to_percent"],
        },
        "calculation": TEXT,
        "gap_id": NULLABLE_TEXT,
    }
)
BUDGET_SCHEMA = object_schema(
    {
        "status": {"type": "string", "enum": ["applicable", "not_applicable", "missing_input"]},
        "mappings": {"type": "array", "items": GROUP_MAPPING_SCHEMA},
        "gaps": {"type": "array", "items": GAP_SCHEMA},
    }
)
PATCH_SCHEMA = object_schema(
    {
        "patches": {
            "type": "array",
            "items": object_schema({"index": {"type": "integer"}, "mapping": GROUP_MAPPING_SCHEMA}),
        },
        "gaps": {"type": "array", "items": GAP_SCHEMA},
    }
)


class PlanBudgetPlanner:
    def __init__(self, llm: LLMClient, control: RunControl, memory: RoleMemory | None) -> None:
        self.llm, self.control, self.memory = llm, control, memory

    def plan(self, snapshot: ProblemSnapshot, context: PlanningContext, plan: Plan) -> Plan:
        return replace(plan, budget_items=self.generate(snapshot, context, plan).items)

    def generate(
        self,
        snapshot: ProblemSnapshot,
        context: PlanningContext,
        plan: Plan,
        *,
        solution_id: str = "",
        on_sheet=None,
    ) -> BudgetResult:
        from erp_agent.application.solution_stages import plan_hash
        from erp_agent.domain.models import Evaluation, SolutionBundle, SolverMode

        prompt = files("erp_agent").joinpath("prompts/solver_budget.txt").read_text()
        schema, digest = dumps(BUDGET_SCHEMA), plan_hash(plan)
        horizon = (snapshot.reading.problem.years or 0) * (
            snapshot.reading.problem.periods_per_year or 0
        )
        hashes = tuple((t.source.id, t.source.sha256) for t in context.templates)
        source_solution = SolutionBundle(
            solution_id,
            snapshot.project_id,
            snapshot.version,
            SolverMode.LLM_ONLY,
            plan,
            Evaluation(False, False, None, (), "草稿"),
            None,
            0,
            0,
            None,
            "",
            "",
        )
        sources = output_sources(source_solution, snapshot)
        units = []
        for template in context.templates:
            reading = next(
                (r for r in snapshot.template_readings if r.template_id == template.source.id), None
            )
            if not reading or not reading.reviewed or reading.sha256 != template.source.sha256:
                raise ERPError("template_not_reviewed", "预算只能使用已核对且哈希一致的空表。", 409)
            for sheet in template.sheets:
                interpreted = next((r for r in reading.sheets if r.name == sheet.name), None)
                groups = tuple(
                    CheckedInput(
                        f"group:{i}", g.label, g.range, g.unit, g.description, g.depends_on_sheets
                    )
                    for i, g in enumerate(interpreted.input_groups if interpreted else ())
                )
                if not groups:
                    continue
                key = (template.source.id, sheet.name)
                units.append((key, template, sheet, groups))
        unit_keys = {u[0] for u in units}
        dependencies = {}
        for key, template, sheet, _ in units:
            reading = next(
                r for r in snapshot.template_readings if r.template_id == template.source.id
            )
            checked = next(r for r in reading.sheets if r.name == sheet.name)
            declared = {
                name
                for group in checked.input_groups
                for name in getattr(group, "depends_on_sheets", ())
            }
            unknown = {(template.source.id, name) for name in declared} - unit_keys
            if unknown:
                raise ERPError("budget.sheet_dependency", "已核对输入引用未知工作表依赖。")
            dependencies[key] = {(template.source.id, name) for name in declared}
        outcomes, failed = {}, {}

        def result(status):
            ordered = [outcomes[u[0]] for u in units if u[0] in outcomes]
            gaps = merge_gaps(g for _, gs in ordered for g in gs)
            canonical = {target: g.id for g in gaps for target in g.targets}
            from erp_agent.domain.budget import BudgetTarget

            items = tuple(
                replace(
                    item,
                    gap_id=canonical.get(
                        BudgetTarget(item.template_id, item.sheet, item.cell), item.gap_id
                    ),
                )
                for values, _ in ordered
                for item in values
            )
            return BudgetResult(
                items,
                gaps,
                status,
                solution_id,
                snapshot.version,
                hashes,
                digest,
                tuple(u[0] for u in units if u[0] in outcomes),
                tuple((*u[0], failed[u[0]]) for u in units if u[0] in failed),
            )

        def extract(template, sheet, groups, task, depth=0):
            self.control.checkpoint("planning_budget_values")
            task_budget(self.control, task).check()
            payload = budget_task_material(snapshot, plan, template, sheet, groups)
            related_ids = {f["id"] for f in array(payload["facts"])}
            action_ids = {a["action_id"] for a in array(payload["actions"])}
            scoped_sources = tuple(
                s
                for s in sources
                if any(s.id.startswith(f"action:{aid}:") for aid in action_ids)
                or s.id.removeprefix("fact:") in related_ids
            )
            payload["sources"] = [
                {
                    "id": source.id,
                    "numeric": source.numeric,
                    "period": source.period,
                    "unit": source.unit,
                }
                for source in scoped_sources
            ]
            payload["solution_id"], payload["plan_hash"] = solution_id, digest
            payload["rule_version"] = snapshot.reading.rules.version
            catalog = getattr(self.llm, "models", ())
            fingerprint = [m.metadata() for m in catalog]
            key = (
                "solver-budget:"
                + hashlib.sha256(
                    dumps(
                        {
                            "payload": payload,
                            "prompt": prompt,
                            "schema": schema,
                            "models": fingerprint,
                        }
                    ).encode()
                ).hexdigest()
            )
            saved = self.memory.get(key) if self.memory else None
            if saved and "mapping" in saved:
                expanded = expand_mappings(
                    obj(saved["mapping"]), template, sheet, groups, scoped_sources, plan, horizon
                )
                report_task(self.control, task, "cached", input_group_count=len(groups))
                return expanded
            group_chars = len(dumps(to_json(groups)))
            estimated_output = len(groups) * 240
            can_split = depth < 3 and (len(groups) > 1 or len(addresses(groups[0].range)) >= 16)

            def split():
                if self.memory:
                    self.memory.put(key, {"split": True})
                report_task(self.control, task, "split", child_count=2)
                if len(groups) > 1:
                    midpoint = len(groups) // 2
                    parts = (groups[:midpoint], groups[midpoint:])
                else:
                    g = groups[0]
                    first, _, last = g.range.partition(":")
                    lo, hi = cell_position(first), cell_position(last or first)
                    axis = 0 if hi[0] > lo[0] else 1
                    middle = (lo[axis] + hi[axis]) // 2
                    if axis == 0:
                        left = first + ":" + (last or first).rstrip("0123456789") + str(middle)
                        right = first.rstrip("0123456789") + str(middle + 1) + ":" + (last or first)
                    else:
                        from erp_agent.application.template_reader import cell_address

                        left = first + ":" + cell_address(hi[0], middle)
                        right = cell_address(lo[0], middle + 1) + ":" + (last or first)
                    parts = ((replace(g, range=left),), (replace(g, range=right),))
                combined, gaps = [], []
                for i, part in enumerate(parts):
                    child = TaskContext(
                        f"{task.id}:{i}",
                        "Solver",
                        task.role,
                        task.object + f" · 区域 {i + 1}",
                        task.id,
                    )
                    items, issues = extract(template, sheet, part, child, depth + 1)
                    combined.extend(items)
                    gaps.extend(issues)
                return tuple(combined), merge_gaps(gaps)

            if can_split and (
                group_chars > 28000 or estimated_output > 16000 or saved and saved.get("split")
            ):
                return split()
            client, response, data = TaskLLMClient(self.llm, self.control, task), None, None
            repair_scope = set()
            request = LLMRequest(
                prompt,
                dumps(payload),
                "erp_solver_budget",
                schema,
                max_output_tokens=6000,
                reasoning_reserve_tokens=8192,
                stream=True,
                retries=0,
                task=task,
                task_complexity="moderate" if any(g.depends_on_sheets for g in groups) else "auto",
            )
            if len(request.system) + len(request.user) + len(schema) > getattr(
                self.llm, "max_input_chars", 100000
            ):
                if can_split:
                    return split()
                raise ERPError("llm_input_limit", "预算关联输入超过模型容量。", 413)
            for attempt in range(2):
                try:
                    response = recovery_call(client, request, self.control)
                    raw = obj(loads(normalize_model_json(response.content)))
                    if attempt and data is not None:
                        patch = obj(raw, {"patches", "gaps"})
                        mappings = list(array(data["mappings"]))
                        seen = set()
                        for value in array(patch["patches"]):
                            item = obj(value, {"index", "mapping"})
                            index = item["index"]
                            if (
                                not isinstance(index, int)
                                or index in seen
                                or index not in repair_scope
                                or not 0 <= index < len(mappings)
                            ):
                                raise ERPError(
                                    "budget.patch_scope", "补丁只能修改当前任务已有映射对象。"
                                )
                            seen.add(index)
                            original = obj(mappings[index])
                            fixed = obj(item["mapping"])
                            if (
                                fixed["group_id"] != original["group_id"]
                                or fixed["target_range"] != original["target_range"]
                            ):
                                raise ERPError(
                                    "budget.patch_scope", "补丁不能修改输入组或扩大目标范围。"
                                )
                            mappings[index] = fixed
                        raw = {
                            "status": data["status"],
                            "mappings": mappings,
                            "gaps": patch["gaps"],
                        }
                    data = raw
                    expanded = expand_mappings(
                        data, template, sheet, groups, scoped_sources, plan, horizon
                    )
                    self.control.checkpoint("saving_planning_progress")
                    if self.memory:
                        self.memory.put(key, {"mapping": data})
                    report_task(
                        self.control,
                        task,
                        "succeeded",
                        model=response.model,
                        input_group_count=len(groups),
                    )
                    return expanded
                except Cancelled:
                    raise
                except ERPError as error:
                    if can_split and error.code in TRANSIENT_ERRORS | {
                        "llm_input_limit",
                        "llm_incomplete",
                        "llm_truncated",
                    }:
                        return split()
                    if attempt == 0 and response is not None:
                        match = re.search(r"mappings\.(\d+)", error.message)
                        if data and "mappings" in data:
                            mappings = array(data["mappings"])
                            repair_scope = (
                                {int(match.group(1))}
                                if match
                                else {
                                    i
                                    for i, mapping in enumerate(mappings)
                                    if any(
                                        cell in error.message
                                        for cell in addresses(obj(mapping)["target_range"])
                                    )
                                }
                            )
                            if not repair_scope:
                                # A malformed/empty whole response is the only whole-range retry.
                                repair_scope = set(range(len(mappings)))
                        repair = {
                            "error": {
                                "code": error.code,
                                "path": error.message,
                                "scope": task.id,
                                "mapping_indices": sorted(repair_scope),
                                "impact": [template.source.id, sheet.name],
                            },
                            "mapping_objects": data,
                            "allowed_groups": to_json(groups),
                            "sources": to_json(scoped_sources),
                            "instruction": "局部修正标出的映射对象，不改行动、事实、目标范围。",
                        }
                        request = replace(
                            request,
                            user=dumps(repair),
                            schema_json=dumps(PATCH_SCHEMA) if data else schema,
                            schema_name="erp_budget_patch" if data else "erp_solver_budget",
                        )
                        continue
                    report_task(self.control, task, "failed", error=error.message)
                    raise
            raise ERPError("budget.invalid", "预算来源映射未完成。")

        def execute(unit):
            _, template, sheet, groups = unit
            task = TaskContext(
                f"solver:values:{template.source.id}:{sheet.name}",
                "Solver",
                "预算来源映射",
                f"{template.source.filename} · {sheet.name}",
                "solver-budget",
            )
            report_task(self.control, task, "queued")
            return extract(template, sheet, groups, task)

        remaining, running = {u[0]: u for u in units}, {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="erp-budget") as pool:
            try:
                while remaining or running:
                    self.control.checkpoint("planning_budget_values")
                    for key, unit in tuple(remaining.items()):
                        if len(running) >= 2:
                            break
                        if dependencies[key] & failed.keys():
                            failed[key] = "依赖工作表未完成。"
                            del remaining[key]
                        elif dependencies[key] <= outcomes.keys():
                            running[pool.submit(execute, unit)] = key
                            del remaining[key]
                    if not running:
                        if remaining:
                            for key in remaining:
                                failed[key] = "工作表依赖存在循环，不能确定执行顺序。"
                            remaining.clear()
                        break
                    completed, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        key = running.pop(future)
                        try:
                            outcomes[key] = future.result()
                            if on_sheet:
                                on_sheet(result("partial"))
                        except Cancelled:
                            raise
                        except ERPError as error:
                            failed[key] = error.message
            except Cancelled:
                for future in running:
                    future.cancel()
                # Wait for transport completion; only complete successes can be retained.
                wait(tuple(running))
                for future, key in tuple(running.items()):
                    if future.cancelled():
                        continue
                    try:
                        outcomes[key] = future.result()
                    except ERPError:
                        continue
                if on_sheet and outcomes:
                    on_sheet(result("partial"))
                raise
        final = result("failed" if failed else "succeeded")
        if failed:
            if on_sheet:
                on_sheet(final)
            raise ERPError(
                "budget.stage_failed",
                "；".join(f"{sheet}：{error}" for _, sheet, error in final.failed_sheets),
            )
        return final
