"""只读题目检索和有界公式执行。工具没有文件、网络或状态写入权限。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from erp_agent.application.contracts import JSONObject, decimal, dumps, obj, to_json
from erp_agent.application.planning_handoff import provided_references, reference_packet
from erp_agent.domain.calculations import (
    FORMULAS,
    CalculationRecord,
    ERPCalculator,
    validate_formula_units,
)
from erp_agent.domain.errors import ERPError

MAX_BATCH_CALCULATIONS = 24
MAX_LOOP_CALCULATIONS = 96


def tool_error(error: ERPError | ValidationError) -> JSONObject:
    if isinstance(error, ValidationError):
        return {
            "error": "tool_arguments",
            "message": "请根据字段诊断修正参数，可批量重试失败项。",
            "fields": [
                {"path": ".".join(map(str, e["loc"])), "message": e["msg"][:240]}
                for e in error.errors(include_input=False, include_url=False)[:8]
            ],
        }
    return {
        "error": error.code,
        "message": (
            "工具已由服务端绑定当前题目；不能指定其它题目。请删除 context_hash 参数后重试。"
            if error.code == "planning.context"
            else error.message
        ),
    }


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_hash: str
    kind: Literal[
        "facts",
        "rules",
        "issues",
        "evidence",
        "formulas",
        "previous_actions",
        "calculations",
        "template_requirements",
    ]
    ids: list[str] = Field(
        default_factory=list,
        max_length=24,
        description="查询 evidence 时必填事实/规则来源中的证据 ID；其它目录可按 ID 或检索条件查询",
    )
    entity_id: str = ""
    category: str = ""
    query: str = Field(default="", max_length=160)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=12, ge=1, le=24)


class Operand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    fact_id: str = ""
    calculation_id: str = ""
    value: str | None = None
    unit: str
    reason: str = Field(
        default="", description="非事实输入的拟定决策或显式情景说明；不可伪装已知状态"
    )


class CalculationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    formula: str
    inputs: list[Operand] = Field(min_length=1, max_length=16)
    unit: str = Field(min_length=1, max_length=40)
    rule_id: str = ""
    period: int | None = Field(default=None, ge=1)
    assumption: str = Field(default="", description="未确认适用的候选公式必须给出命名情景和口径")


class CalculateInput(CalculationItem):
    context_hash: str


class CalculateBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_hash: str
    # 每项独立校验，单项无效不阻止其它计算；对模型仍展示完整条目 Schema。
    calculations: list[object] = Field(min_length=1, max_length=MAX_BATCH_CALCULATIONS)


class PlanningTools:
    def __init__(self, context: JSONObject) -> None:
        self.context = context
        from erp_agent.application.planning_codec import decode_calculation

        self.records: dict[str, CalculationRecord] = {
            r["id"]: decode_calculation(r) for r in context.get("calculation_records", [])
        }
        self.available_records = {
            r["id"]: decode_calculation(r) for r in context.get("available_calculation_records", [])
        }
        if any(
            r.context_hash != context["context_hash"]
            for r in (*self.records.values(), *self.available_records.values())
        ):
            raise ERPError("planning.context", "前置计算引用了其它题目或版本。")
        allowed = {v["id"] for k in ("facts", "rules", "issues") for v in context[k]}
        self.accessed_reference_ids = set(context.get("accessed_reference_ids", [])) & allowed
        self.calculation_attempts = 0

    @staticmethod
    def schemas() -> list[JSONObject]:
        def bound_schema(model: type[BaseModel]) -> JSONObject:
            schema = model.model_json_schema()
            schema["properties"].pop("context_hash")
            schema["required"].remove("context_hash")
            return schema

        calculation = bound_schema(CalculateInput)
        formula_schema = {
            "type": "string",
            "enum": list(FORMULAS),
            "description": "inputs 中的 name 必须逐一对应所选公式签名，不使用 a/b 等别名："
            + "；".join(f"{key}({','.join(f.parameters)})" for key, f in FORMULAS.items()),
        }
        calculation["properties"]["formula"] = formula_schema
        batch = bound_schema(CalculateBatchInput)
        item = CalculationItem.model_json_schema()
        item["properties"]["formula"] = formula_schema
        batch["$defs"] = item.pop("$defs", {})
        batch["properties"]["calculations"]["items"] = item
        return [
            {
                "name": "planning_lookup",
                "description": "按职责需要分页查询当前冻结题目的事实、"
                "规则、原文证据、已执行计算或公式签名；未载入不等于缺失，不允许跨项目查询。",
                "parameters": bound_schema(LookupInput),
            },
            {
                "name": "erp_calculate_batch",
                "description": "优先使用：一批最多24项计算，按数组顺序执行，"
                "逐项返回结果或字段错误。"
                "可引用前批或本批前项的计算 ID；有依赖的项放在来源之后。"
                "可连续多轮分批调用，只重试失败项；单项失败不取消其它成功项。"
                "共享单项计算的来源、单位、情景与题目绑定约束。",
                "parameters": batch,
            },
            {
                "name": "erp_calculate",
                "description": "调用 Decimal 候选公式；参数来自事实 ID、"
                "前次计算或明确拟定决策。参数名见 formula 签名，详细口径按需查询 formulas；"
                "不得传 Python/表达式。"
                "返回值和单位可在最终行动中通过 calculation_id 绑定。",
                "parameters": calculation,
            },
        ]

    def invoke(self, name: str, arguments: JSONObject) -> JSONObject:
        if (
            "context_hash" in arguments
            and arguments["context_hash"] != self.context["context_hash"]
        ):
            raise ERPError("planning.context", "工具调用引用了其它题目或版本。")
        bound = {**arguments, "context_hash": self.context["context_hash"]}
        if name == "planning_lookup":
            return self.lookup(LookupInput.model_validate(bound))
        if name == "erp_calculate":
            from erp_agent.application.task_budget import current_run_control, remaining_model_calls

            remaining_model_calls()  # 检查共享截止时间，不消耗额外模型调用。
            control = current_run_control()
            if control:
                control.checkpoint("planning_calculation")
            if self.calculation_attempts >= MAX_LOOP_CALCULATIONS:
                raise ERPError(
                    "calculation.limit", "本职责工具循环已达96项计算上限，请提交已有结果。"
                )
            self.calculation_attempts += 1
            return self.calculate(CalculateInput.model_validate(bound))
        if name == "erp_calculate_batch":
            request = CalculateBatchInput.model_validate(bound)
            results = []
            previous_access = set(self.accessed_reference_ids)
            sources = []
            for index, arguments in enumerate(request.calculations):
                try:
                    result = self.invoke("erp_calculate", obj(arguments))
                except (ERPError, ValidationError) as error:
                    if isinstance(error, ERPError) and error.code in {"cancelled", "task_deadline"}:
                        raise
                    result = tool_error(error)
                packet = result.pop("calculation_sources", {})
                sources.extend(
                    entry["id"]
                    for kind in ("facts", "rules", "issues")
                    for entry in packet.get(kind, [])
                )
                results.append(
                    {
                        "index": index,
                        "id": arguments.get("id") if isinstance(arguments, dict) else None,
                        **result,
                    }
                )
            response = {
                "context_hash": self.context["context_hash"],
                "results": results,
                "calculation_sources": reference_packet(self.context, sources),
            }
            self.accessed_reference_ids = previous_access | provided_references(response)
            return response
        raise ERPError("planning.tool", "未授权的解题工具。")

    def lookup(self, request: LookupInput) -> JSONObject:
        if request.kind == "formulas":
            entries = [
                {"id": key, "parameters": list(f.parameters), "description": f.description}
                for key, f in FORMULAS.items()
            ]
        elif request.kind in {"facts", "rules", "issues"}:
            entries = self.context[request.kind]
        elif request.kind == "previous_actions":
            entries = [
                {"id": a["action_id"], **a} for a in self.context.get("previous_actions", [])
            ]
        elif request.kind == "calculations":
            entries = [to_json(r) for r in {**self.available_records, **self.records}.values()]
        elif request.kind == "template_requirements":
            entries = [
                {
                    "id": template["template_id"] + ":" + sheet["name"],
                    "template_id": template["template_id"],
                    **sheet,
                }
                for template in self.context.get("template_requirements", [])
                for sheet in template["sheets"]
            ]
        elif request.kind == "evidence":
            if not request.ids:
                raise ERPError("planning.lookup", "原文查询必须提供当前事实或规则的证据 ID。")
            entries = list(self.context["evidence"].values())
        else:
            raise ERPError("planning.lookup", "未知查询目录。")
        selected = [
            e
            for e in entries
            if (not request.ids or e["id"] in request.ids)
            and (not request.entity_id or e.get("entity_id") == request.entity_id)
            and (not request.category or e.get("category") == request.category)
            and (not request.query or request.query.casefold() in dumps(e).casefold())
        ]
        result = []
        for entry in selected[request.offset : request.offset + request.limit]:
            if len(dumps(result + [entry])) > 12000:
                if not result:
                    raise ERPError(
                        "planning.lookup_limit", "单条原文超出查询容量，请使用对应规范事实。"
                    )
                break
            result.append(entry)
        if request.kind in {"facts", "rules", "issues"}:
            self.accessed_reference_ids.update(e["id"] for e in result)
        extra = {}
        if request.kind == "calculations":
            from erp_agent.application.planning_codec import decode_calculation

            references = []
            for entry in result:
                self.records[entry["id"]] = decode_calculation(entry)
                references.extend(entry["fact_ids"])
                if entry["rule_id"]:
                    references.append(entry["rule_id"])
            extra["calculation_sources"] = reference_packet(self.context, references)
            self.accessed_reference_ids.update(provided_references(extra))
        return {
            "context_hash": self.context["context_hash"],
            "kind": request.kind,
            "items": result,
            **extra,
            "total": len(selected),
            "next_offset": request.offset + len(result)
            if request.offset + len(result) < len(selected)
            else None,
        }

    def calculate(self, request: CalculateInput) -> JSONObject:
        horizon = (self.context.get("years") or 0) * (self.context.get("periods_per_year") or 0)
        if request.period is not None and request.period > horizon:
            raise ERPError("calculation.period", "计算期间超出题目经营周期。")
        facts = {f["id"]: f for f in self.context["facts"]}
        rules = {r["id"]: r for r in self.context["rules"]}
        rule = rules.get(request.rule_id)
        if request.rule_id and rule is None:
            raise ERPError("calculation.rule", "公式引用不存在的题目规则。")
        if not request.assumption.strip():
            raise ERPError(
                "calculation.assumption",
                "工具公式须说明适用情景与口径；规则 ID 本身不证明公式适用。",
            )
        values, sources, assumptions = {}, [], []
        for operand in request.inputs:
            if operand.name in values:
                raise ERPError("calculation.arguments", "公式参数重复。")
            if (
                sum(
                    bool(v)
                    for v in (operand.fact_id, operand.calculation_id, operand.value is not None)
                )
                != 1
            ):
                raise ERPError("calculation.source", "每个参数只能指定事实、计算或决策值中的一种。")
            if operand.fact_id:
                fact = facts.get(operand.fact_id)
                if not fact or fact["state"] not in {"known", "assumed"} or fact["value"] is None:
                    raise ERPError("calculation.source", "缺失、冲突或未知事实不能进入计算。")
                if fact["unit"] != operand.unit:
                    raise ERPError("calculation.unit", "事实单位不匹配；不得隐式换算。")
                value = decimal(fact["value"])
                sources.append(operand.fact_id)
                if fact["state"] == "assumed":
                    assumptions.append(f"{operand.name}:题目假设 {operand.fact_id}")
            elif operand.calculation_id:
                record = self.records.get(operand.calculation_id)
                if not record or record.unit != operand.unit:
                    raise ERPError("calculation.source", "前置计算不存在或单位不符。")
                value = Decimal(record.value)
                sources.extend(record.fact_ids)
                if record.assumption:
                    assumptions.append(record.assumption)
            else:
                if not operand.reason.strip():
                    raise ERPError("calculation.source", "非事实输入必须说明拟定决策或情景。")
                value = decimal(operand.value)
                assumptions.append(
                    f"{operand.name}={operand.value} {operand.unit}: {operand.reason}"
                )
            values[operand.name] = value
        result = ERPCalculator().calculate(request.formula, values)
        validate_formula_units(
            request.formula, {o.name: o.unit for o in request.inputs}, request.unit
        )
        # 货币等量纲由显式参数单位与调用情景记录，绝不猜测转换倍率。
        record = CalculationRecord(
            request.id,
            request.formula,
            tuple((k, str(v)) for k, v in values.items()),
            str(result),
            request.unit,
            tuple(dict.fromkeys(sources)),
            request.rule_id,
            "；".join(filter(None, [request.assumption, *assumptions])),
            str(self.context["context_hash"]),
            period=request.period,
            operands=tuple(
                (o.name, o.fact_id or o.calculation_id or "decision", o.unit, o.reason)
                for o in request.inputs
            ),
        )
        if request.id in self.records and self.records[request.id] != record:
            raise ERPError("calculation.id", "计算 ID 已用于不同参数，不允许覆盖计算证据。")
        self.records[request.id] = record
        result = {
            "id": record.id,
            "value": record.value,
            "unit": record.unit,
            "fact_ids": list(record.fact_ids),
            "assumption": record.assumption,
            "context_hash": record.context_hash,
            "formula_version": record.version,
            "period": record.period,
            "calculation_sources": reference_packet(
                self.context, (*record.fact_ids, *((record.rule_id,) if record.rule_id else ()))
            ),
        }
        self.accessed_reference_ids.update(provided_references(result))
        return result
