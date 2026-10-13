import hashlib
from dataclasses import replace
from importlib.resources import files

from langchain_core.runnables import RunnableLambda

from erp_agent.application.budget_context import budget_material
from erp_agent.application.contracts import (
    ISSUE_SCHEMA,
    TEXT,
    JSONObject,
    JSONValue,
    array,
    dumps,
    loads,
    obj,
    object_schema,
    string,
)
from erp_agent.application.model_output import normalize_model_json
from erp_agent.application.ports import LLMClient, LLMRequest, Repository, RunControl, TaskContext
from erp_agent.application.task_reporting import TaskLLMClient, report_task
from erp_agent.domain.budget_validation import cell_position
from erp_agent.domain.errors import Cancelled, ERPError
from erp_agent.domain.models import (
    BudgetSheet,
    BudgetTemplate,
    Issue,
    TemplateInputGroup,
    TemplateReading,
    TemplateSheetReading,
)

TEMPLATE_READER_VERSION = "template-reader-3"
GROUP_SCHEMA = object_schema({"label": TEXT, "range": TEXT, "unit": TEXT, "description": TEXT})
SHEET_SCHEMA = object_schema(
    {
        "purpose": TEXT,
        "periods": TEXT,
        "input_groups": {"type": "array", "items": GROUP_SCHEMA},
        "issues": {"type": "array", "items": ISSUE_SCHEMA},
    }
)
TRANSIENT_ERRORS = {"llm_gateway_timeout", "llm_unavailable", "llm_timeout"}
SPLIT_ERRORS = TRANSIENT_ERRORS | {"llm_input_limit", "llm_incomplete", "llm_truncated"}
LIBRARY_SCOPE = "template-library"


def template_material(template: BudgetTemplate, sheet: BudgetSheet) -> JSONObject:
    raw = obj(budget_material((replace(template, sheets=(sheet,)),))[0]["sheets"][0])
    # 同列连续空白输入合并为矩形，只保留语义标签及输入格式。
    ranges: list[list[JSONValue]] = []
    open_ranges: dict[tuple[int, int, str], int] = {}
    for value in array(raw["input_ranges"]):
        address, number_format = array(value)
        first, _, last = string(address).partition(":")
        start, end = cell_position(first), cell_position(last or first)
        assert start and end
        key = (start[1], end[1], string(number_format))
        index = open_ranges.get(key)
        if index is not None and cell_position(ranges[index][0].split(":")[-1])[0] == start[0] - 1:
            ranges[index][0] = ranges[index][0].split(":")[0] + ":" + (last or first)
        else:
            open_ranges[key] = len(ranges)
            ranges.append([address, number_format])
    raw["cells"] = [[c.coordinate, c.text] for c in sheet.cells if c.text and not c.formula]
    raw["input_ranges"] = ranges
    raw.pop("blank_ranges", None)
    raw.pop("formula_references", None)
    raw.pop("validations", None)
    return {
        "sheet": raw,
        "workbook_sheet_names": [s.name for s in template.sheets],
        "instructions": "简单标签归类：只解释空白input_ranges，不分析公式、不求解、不填数。",
    }


def row_material(material: JSONObject, lo: int, hi: int) -> JSONObject:
    raw = obj(material["sheet"])
    subset = dict(raw)
    subset["cells"] = [
        c
        for c in array(raw["cells"])
        if (p := cell_position(string(array(c)[0]))) and (max(1, lo - 2) <= p[0] <= hi or p[0] <= 7)
    ]
    inputs = []
    for value in array(raw["input_ranges"]):
        address, number_format = array(value)
        first, _, last = string(address).partition(":")
        start, end = cell_position(first), cell_position(last or first)
        if start and end and start[0] <= hi and end[0] >= lo:
            first = first.rstrip("0123456789") + str(max(lo, start[0]))
            last = (last or string(address)).rstrip("0123456789") + str(min(hi, end[0]))
            inputs.append([first if first == last else first + ":" + last, number_format])
    subset["input_ranges"] = inputs
    subset["merged_ranges"] = [
        r
        for r in array(raw["merged_ranges"])
        if (p := cell_position(string(r).split(":")[0]))
        and (p[0] <= 7 or max(1, lo - 2) <= p[0] <= hi)
    ]
    return {**material, "sheet": subset, "row_window": [lo, hi]}


def cell_address(row: int, column: int) -> str:
    label = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        label = chr(65 + remainder) + label
    return label + str(row)


class TemplateReader:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repository = repository

    def read(
        self,
        template: BudgetTemplate,
        client: LLMClient,
        control: RunControl,
    ) -> TemplateReading:
        prompt = files("erp_agent").joinpath("prompts/reader_template.txt").read_text()
        schema = dumps(SHEET_SCHEMA)
        requests: list[LLMRequest] = []
        results: dict[str, tuple[TemplateSheetReading, tuple[Issue, ...]]] = {}
        materials = {s.name: template_material(template, s) for s in template.sheets}

        def task_for(sheet: BudgetSheet, segment: str = "") -> TaskContext:
            return TaskContext(
                "template-read:" + template.source.id + ":" + sheet.name + segment,
                "Reader",
                "空表理解",
                template.source.filename + " · " + sheet.name + segment,
                "template-read:" + template.source.id + ":" + sheet.name if segment else "reader",
            )

        def cached(key: str) -> JSONObject | None:
            if not self.repository:
                return None
            try:
                return self.repository.get("template_chunk", LIBRARY_SCOPE, key)
            except ERPError as error:
                if error.code != "not_found":
                    raise
                return None

        def key_for(material: JSONObject) -> str:
            return hashlib.sha256(
                dumps(
                    {
                        "hash": template.source.sha256,
                        "version": TEMPLATE_READER_VERSION,
                        "material": material,
                    }
                ).encode()
            ).hexdigest()

        def request_for(sheet: BudgetSheet, material: JSONObject, suffix: str = "") -> LLMRequest:
            return LLMRequest(
                prompt + "\nJSON schema:\n" + schema,
                dumps(material),
                "erp_template_reading",
                schema,
                max_output_tokens=min(
                    6000, max(2400, len(array(obj(material["sheet"])["input_ranges"])) * 120)
                ),
                retries=0,
                task=task_for(sheet, suffix),
            )

        for sheet in template.sheets:
            task = task_for(sheet)
            report_task(control, task, "queued")
            saved = cached(key_for(materials[sheet.name]))
            if not array(obj(materials[sheet.name]["sheet"])["input_ranges"]):
                results[sheet.name] = (
                    TemplateSheetReading(sheet.name, "仅展示，无空白输入", "", ()),
                    (),
                )
                report_task(control, task, "succeeded", input_group_count=0)
            elif saved:
                results[sheet.name] = self._decode(saved, template, sheet)
                report_task(control, task, "cached")
            else:
                requests.append(request_for(sheet, materials[sheet.name]))
        planner = getattr(client, "plan_reading", None)
        if planner and requests:
            bounded = tuple(
                r
                for r in requests
                if len(r.system) + len(r.user) + len(r.schema_json)
                <= getattr(client, "max_input_chars", 100_000)
            )
            planner(bounded, control, 3)

        def extract(
            sheet: BudgetSheet, material: JSONObject, suffix: str = "", depth: int = 0
        ) -> tuple[TemplateSheetReading, tuple[Issue, ...]]:
            control.checkpoint("reading_template")
            task = task_for(sheet, suffix)
            saved = cached(key_for(material))
            if saved:
                report_task(control, task, "cached")
                return self._decode(saved, template, sheet, material)
            report_task(control, task, "queued")

            def split() -> tuple[TemplateSheetReading, tuple[Issue, ...]]:
                row_start, row_end = material.get("row_window", [1, sheet.rows])
                midpoint = (int(row_start) + int(row_end)) // 2
                report_task(control, task, "split")
                if self.repository:
                    self.repository.put(
                        "template_split", LIBRARY_SCOPE, key_for(material), {"split": True}
                    )
                pieces = []
                for lo, hi in ((row_start, midpoint), (midpoint + 1, row_end)):
                    subset = row_material(material, int(lo), int(hi))
                    if array(obj(subset["sheet"])["input_ranges"]):
                        pieces.append(extract(sheet, subset, suffix + f" / {lo}-{hi}行", depth + 1))
                groups = tuple(dict.fromkeys(g for s, _ in pieces for g in s.input_groups))
                merged = TemplateSheetReading(
                    sheet.name,
                    "；".join(dict.fromkeys(s.purpose for s, _ in pieces)),
                    "；".join(dict.fromkeys(s.periods for s, _ in pieces)),
                    groups,
                )
                issues = tuple(dict.fromkeys(i for _, findings in pieces for i in findings))
                if self.repository:
                    self.repository.put(
                        "template_chunk",
                        LIBRARY_SCOPE,
                        key_for(material),
                        {
                            "purpose": merged.purpose,
                            "periods": merged.periods,
                            "input_groups": [
                                {
                                    "label": g.label,
                                    "range": g.range,
                                    "unit": g.unit,
                                    "description": g.description,
                                }
                                for g in groups
                            ],
                            "issues": [
                                {
                                    "code": i.code.removeprefix("template."),
                                    "severity": i.severity,
                                    "target": sheet.name,
                                    "message": i.message.removeprefix(sheet.name + "："),
                                    "evidence_ids": [],
                                }
                                for i in issues
                            ],
                        },
                    )
                report_task(
                    control,
                    task,
                    "needs_input" if issues else "succeeded",
                    input_group_count=len(groups),
                )
                return merged, issues

            row_start, row_end = material.get("row_window", [1, sheet.rows])
            can_split = depth < 3 and int(row_end) - int(row_start) >= 12
            learned_split = False
            if self.repository:
                try:
                    learned_split = (
                        self.repository.get("template_split", LIBRARY_SCOPE, key_for(material)).get(
                            "split"
                        )
                        is True
                    )
                except ERPError as error:
                    if error.code != "not_found":
                        raise
            if can_split and (learned_split or len(dumps(material)) > 12000):
                return split()
            scoped = TaskLLMClient(client, control, task)
            payload = material
            for attempt in range(2):
                response = None
                try:
                    request = request_for(sheet, payload, suffix)
                    if len(request.system) + len(request.user) + len(schema) > getattr(
                        client, "max_input_chars", 100_000
                    ):
                        raise ERPError("llm_input_limit", "空表结构超过模型输入上限。")
                    response = scoped.generate(request)
                    data = obj(loads(normalize_model_json(response.content)))
                    result = self._decode(data, template, sheet, material)
                    control.checkpoint("saving_template_chunk")
                    if self.repository:
                        self.repository.put(
                            "template_chunk", LIBRARY_SCOPE, key_for(material), data
                        )
                    report_task(
                        control,
                        task,
                        "needs_input" if result[1] else "succeeded",
                        model=response.model,
                        input_group_count=len(result[0].input_groups),
                    )
                    return result
                except Cancelled:
                    raise
                except ERPError as error:
                    if error.code in SPLIT_ERRORS and can_split:
                        report_task(control, task, "split", error=error.message)
                        return split()
                    if attempt == 0 and error.code in TRANSIENT_ERRORS:
                        control.checkpoint("retrying_template_reading")
                        continue
                    if attempt == 0 and (
                        response is not None or error.code == "llm_structured_response"
                    ):
                        payload = {
                            **material,
                            "error": error.message,
                            "instruction": "修正结构JSON和标出的范围；不要扩展到表外，不填数。",
                        }
                        continue
                    report_task(control, task, "failed", error=error.message)
                    raise ERPError(
                        error.code,
                        f"{template.source.filename} · {sheet.name}："
                        + error.message
                        + " 已保存完成的工作表，重试只处理未完成内容。",
                        error.status,
                    ) from error
            raise ERPError("template_reading_invalid", "空表理解失败。")

        def run(sheet: BudgetSheet):
            try:
                result = extract(sheet, materials[sheet.name])
            except ERPError as error:
                report_task(control, task_for(sheet), "failed", error=error.message)
                raise
            results[sheet.name] = result
            return result

        pending = [s for s in template.sheets if s.name not in results]
        outcomes = RunnableLambda(run).batch(
            pending, config={"max_concurrency": 3}, return_exceptions=True
        )
        errors = [r for r in outcomes if isinstance(r, Exception)]
        if errors:
            raise errors[0]
        return TemplateReading(
            template.source.id,
            template.source.sha256,
            tuple(results[s.name][0] for s in template.sheets),
            tuple(i for s in template.sheets for i in results[s.name][1]),
            prompt_version=TEMPLATE_READER_VERSION,
        )

    @staticmethod
    def _decode(
        data: JSONObject,
        template: BudgetTemplate,
        sheet: BudgetSheet,
        material: JSONObject | None = None,
    ) -> tuple[TemplateSheetReading, tuple[Issue, ...]]:
        data = obj(data, {"purpose", "periods", "input_groups", "issues"})
        groups = []
        input_ranges = array(
            obj((material or template_material(template, sheet))["sheet"])["input_ranges"]
        )
        for value in array(data["input_groups"]):
            entry = obj(value, {"label", "range", "unit", "description"})
            target = string(entry["range"], 100).strip().replace("$", "").upper()
            if "!" in target:
                name, target = target.rsplit("!", 1)
                if name.strip("'") != sheet.name.upper():
                    raise ERPError("template_group_range", "输入范围引用了其他工作表。")
            first, _, last = target.partition(":")
            start, end = cell_position(first), cell_position(last or first)
            if (
                not start
                or not end
                or not 1 <= start[0] <= end[0] <= sheet.rows
                or not 1 <= start[1] <= end[1] <= sheet.columns
            ):
                raise ERPError("template_group_range", f"{target} 超出 {sheet.name} 实际行列范围。")
            matches = []
            for value in input_ranges:
                address = string(array(value)[0])
                left, _, right = address.partition(":")
                lo, hi = cell_position(left), cell_position(right or left)
                assert lo and hi
                r1, c1 = max(start[0], lo[0]), max(start[1], lo[1])
                r2, c2 = min(end[0], hi[0]), min(end[1], hi[1])
                if r1 <= r2 and c1 <= c2:
                    first, last = cell_address(r1, c1), cell_address(r2, c2)
                    matches.append(first if first == last else first + ":" + last)
            if not matches:
                raise ERPError("template_group_range", f"{target} 没有可写空白输入。")
            for target in matches:
                groups.append(
                    TemplateInputGroup(
                        string(entry["label"]),
                        target,
                        string(entry["unit"]),
                        string(entry["description"]),
                    )
                )
        issues = []
        for value in array(data["issues"]):
            entry = obj(value, {"code", "severity", "target", "message", "evidence_ids"})
            severity = string(entry["severity"])
            if severity not in {"error", "warning", "info"}:
                raise ERPError("invalid_contract", "空表疑点等级无效。")
            issues.append(
                Issue(
                    "template." + string(entry["code"]),
                    severity,
                    template.source.id,
                    sheet.name + "：" + string(entry["message"]),
                )
            )
        return TemplateSheetReading(
            sheet.name,
            string(data["purpose"]),
            string(data["periods"]),
            tuple(dict.fromkeys(groups)),
        ), tuple(dict.fromkeys(issues))
