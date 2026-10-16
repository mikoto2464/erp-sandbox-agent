"""现金金额的有限单位换算；只认明确币种，不解释每月费用或猜测倍率。"""

from decimal import Decimal

from erp_agent.domain.errors import ERPError

_CNY = {
    "元": Decimal(1),
    "人民币元": Decimal(1),
    "人民币": Decimal(1),
    "CNY": Decimal(1),
    "RMB": Decimal(1),
    "万元": Decimal(10000),
}


def money_value(value: Decimal, source: str, target: str) -> Decimal:
    source, target = source.strip(), target.strip()
    if not source or not target:
        raise ERPError("money.unit", "现金金额必须具有明确单位。")
    if any("/" in u or "／" in u or u in {"万", "%", "％"} for u in (source, target)):
        raise ERPError("money.unit", "现金单位必须是金额单位，不能使用费率或省略币种的倍率。")
    if source == target:
        return value
    if source in _CNY and target in _CNY:
        return value * _CNY[source] / _CNY[target]
    raise ERPError("money.unit", f"不能将现金单位“{source}”确定换算为“{target}”。")
