"""有版本的候选公式。这里只计算显式输入，不推断平台规则或初始状态。"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

from erp_agent.domain.errors import ERPError

FORMULA_VERSION = "erp-formulas-2"


@dataclass(frozen=True)
class CalculationRecord:
    id: str
    formula: str
    inputs: tuple[tuple[str, str], ...]
    value: str
    unit: str
    fact_ids: tuple[str, ...]
    rule_id: str
    assumption: str
    context_hash: str
    operands: tuple[tuple[str, str, str, str], ...] = ()
    period: int | None = None
    version: str = FORMULA_VERSION


@dataclass(frozen=True)
class Formula:
    parameters: tuple[str, ...]
    description: str
    evaluate: Callable[[dict[str, Decimal]], Decimal]


def _carbon(p: dict[str, Decimal]) -> Decimal:
    if p["total_emissions"] <= 0 or p["team_count"] <= 1:
        raise ERPError("calculation.domain", "碳指标要求总排放大于零、队伍数大于一。")
    if p["own_emissions"] > p["total_emissions"]:
        raise ERPError("calculation.domain", "本队排放不能超过全部队伍总排放。")
    return (
        p["total_emissions"]
        * (1 - p["own_emissions"] / p["total_emissions"])
        / (p["team_count"] - 1)
    )


def _depreciation(p: dict[str, Decimal]) -> Decimal:
    if p["years"] <= 0 or p["residual"] > p["original"]:
        raise ERPError("calculation.domain", "折旧年限须大于零，残值不得高于原值。")
    return (p["original"] - p["residual"]) / p["years"]


def _feature_research(p: dict[str, Decimal]) -> Decimal:
    if p["target_value"] < p["current_value"]:
        raise ERPError("calculation.domain", "特性研发目标不得低于当前值，不能产生退款。")
    return (p["target_value"] - p["current_value"]) * p["unit_cost"]


FORMULAS: dict[str, Formula] = {
    "feature_research": Formula(
        ("current_value", "target_value", "unit_cost"),
        "(拟定目标特性值-当前特性值)*每点研发费，操作当期扣现金；"
        "不推断初始值，特性上限须另核对当前规则；不是产品生产资质或每次图纸设计费。",
        _feature_research,
    ),
    "score": Formula(
        (
            "equity",
            "digital_cost",
            "budget_deduction",
            "goodwill",
            "carbon_current",
            "carbon_previous",
        ),
        "候选得分；数字化倍数十，碳中和率用比例。不是已验证得分。",
        lambda p: (
            (p["equity"] + p["digital_cost"] * 10 - p["budget_deduction"])
            * p["goodwill"]
            * (1 + p["carbon_current"] + p["carbon_previous"])
        ),
    ),
    "bid_index": Formula(
        ("advertising", "market_share", "goodwill", "reference_price", "offer", "feature_value"),
        "竞单指数；不推导胜率或必得订单，市场份额须显式传入。",
        lambda p: (
            p["advertising"]
            + p["market_share"] * p["goodwill"] * (p["reference_price"] - p["offer"])
            + 1000 * p["feature_value"]
        ),
    ),
    "production": Formula(
        ("base_capacity", "manual_efficiency_sum", "senior_efficiency_sum", "shift_multiplier"),
        "floor(基础产能*(1+手工效率之和/4+高级效率之和)*班次加成)，效率用比例。",
        lambda p: (
            p["base_capacity"]
            * (1 + p["manual_efficiency_sum"] / 4 + p["senior_efficiency_sum"])
            * p["shift_multiplier"]
        ).to_integral_value(rounding=ROUND_FLOOR),
    ),
    "piece_wages": Formula(
        ("quantity", "manual_piece_rate", "manual_count", "senior_piece_rate", "senior_count"),
        "产量*(手工单件工资*人数+高级单件工资*人数)。",
        lambda p: (
            p["quantity"]
            * (
                p["manual_piece_rate"] * p["manual_count"]
                + p["senior_piece_rate"] * p["senior_count"]
            )
        ),
    ),
    "startup_cost": Formula(
        ("quantity", "unit_cost"), "产量*单件开产费。", lambda p: p["quantity"] * p["unit_cost"]
    ),
    "wip_cost": Formula(
        ("materials", "monthly_wages", "production_months", "piece_wages", "startup_cost"),
        "材料+参与批次的月薪合计*生产月数+计件工资+开产费；不可重复作为现金付款。",
        lambda p: (
            p["materials"]
            + p["monthly_wages"] * p["production_months"]
            + p["piece_wages"]
            + p["startup_cost"]
        ),
    ),
    "dismissal": Formula(
        ("service_years", "monthly_salary"),
        "(ceil(在职年数)+1)*月薪。",
        lambda p: (
            (max(Decimal(1), p["service_years"].to_integral_value(rounding=ROUND_CEILING)) + 1)
            * p["monthly_salary"]
        ),
    ),
    "depreciation": Formula(("original", "residual", "years"), "年度直线折旧。", _depreciation),
    "member_index": Formula(
        ("advertising_heat", "traffic_factor", "goodwill"),
        "floor(热度*引流参数*商誉*0.01)。",
        lambda p: (
            p["advertising_heat"] * p["traffic_factor"] * p["goodwill"] / 100
        ).to_integral_value(rounding=ROUND_FLOOR),
    ),
    "retail_index": Formula(
        ("member_index", "affordability", "offer"),
        "会员指数*(承受价-报价)*0.01；不截断负数。",
        lambda p: p["member_index"] * (p["affordability"] - p["offer"]) / 100,
    ),
    "emissions": Formula(
        ("quantity", "unit_emissions", "line_emissions", "batches"),
        "数量*产品排放+产线单次排放*生产次数；分产品/产线计算后合计。",
        lambda p: p["quantity"] * p["unit_emissions"] + p["line_emissions"] * p["batches"],
    ),
    "carbon_quota": Formula(
        ("total_emissions", "own_emissions", "team_count"), "A*(1-Ax/A)/(N-1)。", _carbon
    ),
    "goodwill_loss": Formula(
        (
            "breaches",
            "unpaid_wages",
            "unreceived_goods",
            "unpaid_debt",
            "unpaid_payables",
            "unpaid_management",
            "marketing_without_stock",
        ),
        "按显式违规次数累计扣减；未付工资每次五，其余每次一，不推断边界或恢复。",
        lambda p: (
            p["breaches"]
            + 5 * p["unpaid_wages"]
            + p["unreceived_goods"]
            + p["unpaid_debt"]
            + p["unpaid_payables"]
            + p["unpaid_management"]
            + p["marketing_without_stock"]
        ),
    ),
    "interest": Formula(
        ("principal", "term_rate", "quantum"),
        "完整期限总息，按显式精度向上取整，不推断分期。",
        lambda p: _rounded(p["principal"] * p["term_rate"], p["quantum"], ROUND_CEILING),
    ),
    "half_up": Formula(
        ("value", "quantum"),
        "十进制四舍五入，含负数；精度显式给定。",
        lambda p: _rounded(p["value"], p["quantum"], ROUND_HALF_UP),
    ),
    "floor": Formula(
        ("value", "quantum"),
        "向下取整，含负数；精度显式给定。",
        lambda p: _rounded(p["value"], p["quantum"], ROUND_FLOOR),
    ),
    "multiply": Formula(
        ("quantity", "unit_price"),
        "显式数量*单价，或期间数*每期费用；如3月*元/月得到元，不隐式换算季/月。",
        lambda p: p["quantity"] * p["unit_price"],
    ),
}


def _rounded(value: Decimal, quantum: Decimal, rounding: str) -> Decimal:
    if quantum <= 0:
        raise ERPError("calculation.domain", "取整精度须大于零。")
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


class ERPCalculator:
    version = FORMULA_VERSION

    def calculate(self, formula: str, inputs: dict[str, Decimal]) -> Decimal:
        definition = FORMULAS.get(formula)
        if definition is None:
            raise ERPError(
                "calculation.arguments", f"未知公式 {formula}；请使用工具 Schema 中的公式名。"
            )
        if set(inputs) != set(definition.parameters):
            raise ERPError(
                "calculation.arguments",
                f"{formula} 要求 inputs.name：{', '.join(definition.parameters)}；"
                f"缺少 {sorted(set(definition.parameters) - set(inputs))}，"
                f"多余 {sorted(set(inputs) - set(definition.parameters))}。",
            )
        if any(not v.is_finite() or abs(v) > Decimal("1e15") for v in inputs.values()):
            raise ERPError("calculation.range", "公式输入必须是范围内有限十进制数。")
        signed = {"equity", "value"}
        if any(value < 0 for key, value in inputs.items() if key not in signed):
            raise ERPError("calculation.domain", "数量、成本、效率等输入不能为负。")
        integers = {
            "quantity",
            "manual_count",
            "senior_count",
            "team_count",
            "batches",
            "breaches",
            "unpaid_wages",
            "unreceived_goods",
            "unpaid_debt",
            "unpaid_payables",
            "unpaid_management",
            "marketing_without_stock",
        }
        if any(v != v.to_integral_value() for k, v in inputs.items() if k in integers):
            raise ERPError("calculation.domain", "件数、人数、次数与队伍数必须为整数。")
        with localcontext() as context:
            context.prec = 40
            value = definition.evaluate(inputs)
        if not value.is_finite() or abs(value) > Decimal("1e15"):
            raise ERPError("calculation.range", "公式结果超出允许范围。")
        return value


def validate_formula_units(formula: str, units: dict[str, str], output: str) -> None:
    """校验规范量纲；单位换算须由独立规则完成，工具不猜倍率。"""
    ratios = {"1", "比例", "倍", "无量纲"}
    money = {"元", "万元"}
    count = {"件", "个", "套", "台", "批", "人", "次", "队"}

    def same(names: tuple[str, ...], allowed: set[str] | None = None) -> None:
        values = {units[n] for n in names}
        if len(values) != 1 or (allowed is not None and not values <= allowed):
            raise ERPError("calculation.unit", "同一公式的金额/数量单位不一致。")

    ratio_fields = {
        "production": ("manual_efficiency_sum", "senior_efficiency_sum", "shift_multiplier"),
        "bid_index": ("market_share",),
        "score": ("carbon_current", "carbon_previous"),
        "interest": ("term_rate",),
    }
    if any(units[n] not in ratios for n in ratio_fields.get(formula, ())):
        raise ERPError(
            "calculation.unit", "百分数、效率、利率须以比例传入，不能把百分数直接当比例。"
        )
    if formula == "feature_research":
        same(("current_value", "target_value"), {"点", "1", "无量纲"})
        if output not in money or units["unit_cost"] != f"{output}/{units['current_value']}":
            raise ERPError("calculation.unit", "研发特性值与每点费用单位不匹配。")
    elif formula in {"multiply", "startup_cost"}:
        price = "unit_price" if formula == "multiply" else "unit_cost"
        quantities = count | {"月", "季", "期", "年"} if formula == "multiply" else count
        if units["quantity"] not in quantities or units[price] != f"{output}/{units['quantity']}":
            raise ERPError("calculation.unit", "数量乘单价的量纲不匹配。")
    elif formula == "production":
        base = units["base_capacity"]
        if output not in count or base not in {output, f"{output}/批", f"{output}/期"}:
            raise ERPError("calculation.unit", "产能与产量单位不匹配。")
    elif formula == "piece_wages":
        if output not in money or units["quantity"] not in count:
            raise ERPError("calculation.unit", "工资和产量单位不明确。")
        if any(
            units[n] != f"{output}/{units['quantity']}"
            for n in ("manual_piece_rate", "senior_piece_rate")
        ):
            raise ERPError("calculation.unit", "单件工资单位不匹配。")
        if any(units[n] != "人" for n in ("manual_count", "senior_count")):
            raise ERPError("calculation.unit", "工人人数单位须为人。")
    elif formula == "wip_cost":
        same(("materials", "piece_wages", "startup_cost"), money)
        if (
            units["materials"] != output
            or units["monthly_wages"] != f"{output}/月"
            or units["production_months"] != "月"
        ):
            raise ERPError("calculation.unit", "在制品月薪、生产月数或成本单位不匹配。")
    elif formula == "interest":
        same(("principal", "quantum"), money)
        if output != units["principal"]:
            raise ERPError("calculation.unit", "利息与本金单位不匹配。")
    elif formula in {"half_up", "floor"}:
        same(("value", "quantum"))
        if output != units["value"]:
            raise ERPError("calculation.unit", "取整不能改变单位。")
    elif formula == "depreciation":
        same(("original", "residual"), money)
        if units["years"] != "年" or output != f"{units['original']}/年":
            raise ERPError("calculation.unit", "折旧结果须为金额/年；不能直接当季度支出。")
    elif formula == "dismissal":
        if (
            units["service_years"] != "年"
            or units["monthly_salary"] != f"{output}/月"
            or output not in money
        ):
            raise ERPError("calculation.unit", "解雇赔偿的年限或月薪单位不匹配。")
    elif formula == "carbon_quota":
        same(("total_emissions", "own_emissions"))
        if output != units["total_emissions"] or units["team_count"] != "队":
            raise ERPError("calculation.unit", "碳指标与排放量或队伍单位不匹配。")
    elif formula == "score":
        same(("equity", "digital_cost", "budget_deduction"), money)
        if output not in {"分", "得分"}:
            raise ERPError("calculation.unit", "得分计算须显式声明得分单位。")
    elif formula == "bid_index":
        same(("reference_price", "offer"))
        if units["advertising"] not in money or output != "指数":
            raise ERPError("calculation.unit", "竞单广告须为金额，计算结果为指数。")
        if not units["reference_price"].startswith(units["advertising"] + "/"):
            raise ERPError("calculation.unit", "竞单报价币值口径须与广告一致。")
    elif formula == "retail_index":
        same(("affordability", "offer"))
        if units["member_index"] != "指数" or output != "指数":
            raise ERPError("calculation.unit", "零售/会员指数单位须为指数。")
    elif formula == "member_index":
        if units["goodwill"] not in {"商誉", "点", "1"} or output != "指数":
            raise ERPError("calculation.unit", "会员指数的商誉单位或结果单位无效。")
    elif formula == "emissions":
        if (
            units["quantity"] not in count
            or units["batches"] != "次"
            or units["unit_emissions"] != f"{output}/{units['quantity']}"
            or units["line_emissions"] != f"{output}/次"
        ):
            raise ERPError("calculation.unit", "排放计算的产品数量、排放量或生产次数单位不符。")
    elif formula == "goodwill_loss":
        if output not in {"商誉", "点"} or any(unit != "次" for unit in units.values()):
            raise ERPError("calculation.unit", "商誉扣减输入须为违规次数，结果须为商誉或点。")
