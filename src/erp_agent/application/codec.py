from dataclasses import replace

from erp_agent.application.contracts import (
    JSONObject, array, decode_reading, dumps, integer, obj, string, strings,
)
from erp_agent.domain.models import (
    Cell, DocumentBlock, Evidence, ParsedDocument, ProblemSnapshot, SourceDocument,
)


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
                              integer(cell["column_span"]), string(cell["vertical_merge"])))
        blocks.append(DocumentBlock(integer(block["index"], 0), string(block["kind"]),
                                    string(block["text"]), string(block["evidence_id"]),
                                    tuple(cells)))
    evidence = []
    for value in array(data["evidence"]):
        item = obj(value)
        evidence.append(Evidence(string(item["id"]), string(item["document_id"]),
                                 string(item["location"]), string(item["text"])))
    return ParsedDocument(
        SourceDocument(string(source["id"]), string(source["filename"]), string(source["sha256"]),
                       integer(source["size"], 0, 100 * 1024 * 1024), string(source["format"])),
        tuple(blocks), tuple(evidence), strings(data["warnings"]), string(data["parser_version"]),
        None if data["converted_sha256"] is None else string(data["converted_sha256"]),
    )


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
                           string(data["revision_note"]))
