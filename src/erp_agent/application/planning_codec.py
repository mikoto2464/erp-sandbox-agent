from erp_agent.application.contracts import (
    JSONObject,
    JSONValue,
    array,
    integer,
    obj,
    string,
    strings,
)
from erp_agent.domain.budget import BudgetGap, BudgetTarget
from erp_agent.domain.calculations import CalculationRecord
from erp_agent.domain.planning import PlanPreparation
from erp_agent.domain.reporting import ReportNote, ReportOutline


def decode_calculation(c: JSONObject) -> CalculationRecord:
    return CalculationRecord(
        id=string(c["id"]),
        formula=string(c["formula"]),
        inputs=tuple(tuple(string(v) for v in array(row)) for row in array(c["inputs"])),
        value=string(c["value"]),
        unit=string(c["unit"]),
        fact_ids=strings(c["fact_ids"]),
        rule_id=string(c["rule_id"]),
        assumption=string(c["assumption"]),
        context_hash=string(c["context_hash"]),
        operands=tuple(
            tuple(string(v) for v in array(row)) for row in array(c.get("operands", []))
        ),
        period=None if c.get("period") is None else integer(c["period"]),
        version=string(c["version"]),
    )


def decode_preparation(data: JSONObject) -> PlanPreparation:
    outline = obj(data["outline"])

    def note(raw: JSONValue) -> ReportNote:
        entry = obj(raw)
        return ReportNote(
            title=string(entry["title"]),
            explanation=string(entry["explanation"]),
            fact_ids=strings(entry.get("fact_ids", [])),
            rule_ids=strings(entry.get("rule_ids", [])),
            budget_indices=tuple(integer(i, 0) for i in array(entry.get("budget_indices", []))),
            period=None if entry.get("period") is None else integer(entry["period"]),
            condition=string(entry.get("condition", "")),
            response=string(entry.get("response", "")),
            action_ids=strings(entry.get("action_ids", [])),
        )

    def pairs(value: JSONValue) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(string(part) for part in array(row)) for row in array(value))

    return PlanPreparation(
        context_hash=string(data["context_hash"]),
        template_hashes=pairs(data["template_hashes"]),
        calculations=tuple(decode_calculation(c) for c in map(obj, array(data["calculations"]))),
        outline=ReportOutline(
            **{
                section: tuple(note(value) for value in array(outline[section]))
                for section in ("strategy", "milestones", "risks")
            }
        ),
        budget_gaps=tuple(
            BudgetGap(
                string(g["id"]),
                string(g["code"]),
                string(g["message"]),
                tuple(
                    BudgetTarget(string(t["template_id"]), string(t["sheet"]), string(t["cell"]))
                    for t in map(obj, array(g["targets"]))
                ),
                strings(g["source_ids"]),
                string(g["rule_id"]),
            )
            for g in map(obj, array(data.get("budget_gaps", [])))
        ),
        completed_sheets=pairs(data.get("completed_sheets", [])),
        failed_sheets=pairs(data.get("failed_sheets", [])),
        snapshot_hash=string(data.get("snapshot_hash", "")),
        version=string(data.get("version", "solver-16")),
    )
