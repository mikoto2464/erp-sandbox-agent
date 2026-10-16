"""每个节点只接收公共资料契约及本阶段职责。"""

from importlib.resources import files


def planning_prompt(stage: str) -> str:
    if stage not in {"operations", "finance", "route", "window", "synthesis"}:
        raise ValueError(f"Unknown planning stage: {stage}")
    root = files("erp_agent").joinpath("prompts")
    parts = [root.joinpath("planning.txt").read_text(encoding="utf-8")]
    parts.append(root.joinpath("planning_platform.txt").read_text(encoding="utf-8"))
    parts.append(root.joinpath(f"planning_{stage}.txt").read_text(encoding="utf-8"))
    if stage in {"route", "window", "synthesis"}:
        parts.append(root.joinpath("planning_decisions.txt").read_text(encoding="utf-8"))
    return "\n\n".join(parts)
