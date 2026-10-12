from dataclasses import replace

from erp_agent.application.contracts import (
    JSONObject, JSONValue, array, decode_plan, decode_reading, decimal, dumps, integer, obj, string, strings,
)
from erp_agent.domain.evidence import contextualize_document
from erp_agent.domain.errors import ERPError
from erp_agent.domain.models import (
    BudgetCell, BudgetFormulaReference, BudgetSheet, BudgetTemplate, BudgetValidation, Cell,
    DocumentAnnotation, DocumentBlock, Evaluation, Evidence, GenerationConfig, Issue,
    ParsedDocument, PlanTone, ProblemSnapshot, ReadingInputs, SolutionBundle, SolverMode,
    SourceDocument, WorkbookEntry, WorkbookMapping,
)


def solution_bundle(data: JSONObject) -> SolutionBundle:
    evaluation = obj(data["evaluation"])
    issues = tuple(Issue(string(i["code"]), string(i["severity"]), string(i["target"]),
                         string(i["message"]), strings(i.get("evidence_ids", [])))
                   for value in array(evaluation["issues"]) for i in (obj(value),))
    return SolutionBundle(
        string(data["id"]), string(data["project_id"]), integer(data["problem_version"]),
        SolverMode(string(data["mode"])), decode_plan(data["plan"]),
        Evaluation(bool(evaluation["feasible"]), bool(evaluation["verified"]),
                   None if evaluation["score"] is None else decimal(evaluation["score"]),
                   issues, string(evaluation["scope"])),
        None if data["baseline_score"] is None else decimal(data["baseline_score"]),
        integer(data["evaluated_candidates"], 0), integer(data["feasible_candidates"], 0),
        None if data["seed"] is None else integer(data["seed"], 0, 2**32 - 1),
        string(data["model"]), "", generation=generation_config(obj(data.get("generation", {}))),
        prompt_version=string(data.get("prompt_version", "solver-1")),
    )


def workbook_entries(value: JSONValue) -> tuple[WorkbookEntry, ...]:
    entries = []
    for value in array(value):
        item = obj(value, {"sheet", "cell", "label", "source_ids", "conversion", "explanation"})
        conversion = string(item["conversion"])
        if conversion not in {"identity", "sum", "yuan_to_wan", "wan_to_yuan", "ratio_to_percent"}:
            raise ERPError("invalid_mapping", "不支持的填表转换方式。")
        entries.append(WorkbookEntry(string(item["sheet"], 100), string(item["cell"], 20),
                                     string(item["label"]), strings(item["source_ids"]), conversion,
                                     string(item["explanation"])))
    if len(entries) > 10000:
        raise ERPError("mapping_limit", "映射条目过多。")
    return tuple(entries)


def workbook_mapping(data: JSONObject) -> WorkbookMapping:
    issues = tuple(Issue(string(i["code"]), string(i["severity"]), string(i["target"]),
                         string(i["message"])) for value in array(data["issues"]) for i in (obj(value),))
    return WorkbookMapping(string(data["id"]), string(data["project_id"]), string(data["solution_id"]),
                           integer(data["problem_version"]), string(data["template_id"]),
                           string(data["template_sha256"]), workbook_entries(data["entries"]), issues,
                           integer(data["revision"]), bool(data["confirmed"]), string(data["model"]),
                           string(data["prompt_version"]))


def generation_config(data: JSONObject) -> GenerationConfig:
    try:
        tone = PlanTone(string(data.get("tone", "balanced")))
    except ValueError as exc:
        raise ERPError("invalid_plan_tone", "请选择保守、平稳、激进或自定义基调。") from exc
    custom = string(data.get("custom_prompt", ""), 10000).strip()
    if tone == PlanTone.CUSTOM and not custom:
        raise ERPError("custom_prompt_required", "自定义基调需要填写方案要求。")
    template_ids = strings(data.get("template_ids", []))
    if len(template_ids) > 3 or len(set(template_ids)) != len(template_ids):
        raise ERPError("invalid_templates", "最多选择 3 份预算表且不能重复。")
    previous = data.get("previous_solution_id")
    revision = string(data.get("revision_prompt", ""), 10000).strip()
    if bool(previous) != bool(revision):
        raise ERPError("revision_prompt_required", "调整方案需选择已有方案并填写修改要求。")
    return GenerationConfig(tone, custom if tone == PlanTone.CUSTOM else "", template_ids,
                            None if previous is None else string(previous, 100), revision,
                            strings(data.get("requirements", [])))


def budget_template(data: JSONObject) -> BudgetTemplate:
    source = obj(data["source"])
    sheets = []
    for value in array(data["sheets"]):
        sheet = obj(value)
        cells = tuple(BudgetCell(string(c["coordinate"]), string(c["text"]),
                                 string(c["number_format"]), bool(c["formula"]), bool(c["writable"]))
                      for value in array(sheet["cells"]) for c in (obj(value),))
        sheets.append(BudgetSheet(string(sheet["name"]), integer(sheet["rows"], maximum=1048576),
                                  integer(sheet["columns"], maximum=16384), cells,
                                  strings(sheet["merged_ranges"]), bool(sheet["hidden"]),
                                  bool(sheet["protected"]),
                                  tuple(BudgetFormulaReference(string(r["formula_cell"]), string(r["sheet"]),
                                                               string(r["range"]))
                                        for v in array(sheet.get("formula_references", [])) for r in (obj(v),)),
                                  tuple(BudgetValidation(string(r["range"]), string(r["kind"]),
                                                         string(r["formula1"]), string(r["formula2"]))
                                        for v in array(sheet.get("validations", [])) for r in (obj(v),))))
    return BudgetTemplate(SourceDocument(
        string(source["id"]), string(source["filename"]), string(source["sha256"]),
        integer(source["size"], 0, 100 * 1024 * 1024), string(source["format"])),
        tuple(sheets), strings(data["warnings"]), string(data["parser_version"]))


def parsed_document(data: JSONObject) -> ParsedDocument:
    source = obj(data["source"])
    blocks = []
    for value in array(data["blocks"]):
        block = obj(value)
        cells = []
        for item in array(block["cells"]):
            cell = obj(item)
            cells.append(Cell(integer(cell["row"], 0), integer(cell["column"], 0),
                              string(cell["text"]), string(cell["evidence_id"]),
                              integer(cell["column_span"]), string(cell["vertical_merge"]),
                              string(cell.get("number_format", ""))))
        blocks.append(DocumentBlock(integer(block["index"], 0), string(block["kind"]),
                                    string(block["text"]), string(block["evidence_id"]),
                                    tuple(cells)))
    evidence = []
    for value in array(data["evidence"]):
        item = obj(value)
        evidence.append(Evidence(string(item["id"]), string(item["document_id"]),
                                 string(item["location"]), string(item["text"])))
    return contextualize_document(ParsedDocument(
        SourceDocument(string(source["id"]), string(source["filename"]), string(source["sha256"]),
                       integer(source["size"], 0, 100 * 1024 * 1024), string(source["format"])),
        tuple(blocks), tuple(evidence), strings(data["warnings"]), string(data["parser_version"]),
        None if data["converted_sha256"] is None else string(data["converted_sha256"]),
    ))


def problem_snapshot(data: JSONObject) -> ProblemSnapshot:
    result = obj(data["reading"])
    problem = obj(result["problem"])
    rules = obj(result["rules"])
    reading = decode_reading(dumps({"title": problem["title"], "facts": problem["facts"],
                                    "rules": rules["rules"], "issues": result["issues"]}),
                             string(result["model"]))
    reading = replace(reading,
                      problem=replace(reading.problem,
                                      years=None if problem["years"] is None
                                      else integer(problem["years"]),
                                      periods_per_year=None if problem["periods_per_year"] is None
                                      else integer(problem["periods_per_year"])),
                      rules=replace(reading.rules, version=integer(rules["version"])),
                      prompt_version=string(result["prompt_version"]),
                      raw_response=string(result["raw_response"], maximum=4 * 1024 * 1024))
    return ProblemSnapshot(string(data["project_id"]), integer(data["version"]),
                           string(data["created_at"]), strings(data["document_ids"]), reading,
                           string(data["revision_note"]), reading_inputs(obj(data.get("inputs", {}))),
                           tuple(parsed_document(obj(d)) for d in array(data.get("additional_sources", []))))


def reading_inputs(data: JSONObject) -> ReadingInputs:
    obj(data, {"revision", "problem_text", "supplement_text", "annotations"})
    annotations = tuple(DocumentAnnotation(string(entry["document_id"], maximum=100),
                                            string(entry["text"], maximum=5000).strip())
                        for value in array(data.get("annotations", []))
                        for entry in (obj(value, {"document_id", "text"}),))
    if len(annotations) > 100 or len({a.document_id for a in annotations}) != len(annotations):
        raise ERPError("invalid_inputs", "文件说明数量过多或重复。")
    return ReadingInputs(integer(data.get("revision", 0), 0, 1000000),
                         string(data.get("problem_text", ""), maximum=100000).strip(),
                         string(data.get("supplement_text", ""), maximum=50000).strip(), annotations)
