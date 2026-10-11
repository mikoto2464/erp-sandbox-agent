import re


def normalize_model_json(content: str) -> str:
    text = content.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text, re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text
