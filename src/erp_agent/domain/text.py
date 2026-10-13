import re


def normalized_text(value: str) -> str:
    """消除中文排版空格，保留数字及英文词之间的分隔。"""
    text = re.sub(r"\s+", " ", value).strip()
    text = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff0-9])", "", text)
    return re.sub(r"(?<=[0-9]) (?=[\u3400-\u9fff])", "", text)
