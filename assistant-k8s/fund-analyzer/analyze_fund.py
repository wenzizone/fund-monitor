#!/usr/bin/env python3
"""场外基金择时分析: 基准估值百分位 + 净值乖离率 + 持仓行业 + 同类排名 + 机构仓位情绪 -> 加仓/建仓建议。

用法: python3 analyze_fund.py <基金代码> [基金代码 ...]

数据源来自 akshare 对天天基金/雪球/乐咕乐股的抓取,部分接口偶发失效,
每个数据维度都做了独立 try/except,单个数据源失败不影响其余维度输出。
"""
import re
import sys

import akshare as ak
import pandas as pd

# stock_index_pe_lg 支持的指数名单
LG_INDEX_NAMES = {
    "上证50", "沪深300", "上证380", "创业板50", "中证500", "上证180",
    "深证红利", "深证100", "中证1000", "上证红利", "中证100", "中证800",
}

VALUATION_LOOKBACK_YEARS = 10


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 - 数据源抓取失败时降级展示,不中断整体报告
        return None, f"{type(e).__name__}: {e}"


def get_fund_basic(code: str) -> dict:
    info = ak.fund_individual_basic_info_xq(symbol=code)
    row = dict(zip(info["item"], info["value"]))
    return {
        "code": code,
        "name": row.get("基金名称", ""),
        "type": row.get("基金类型", ""),
        "benchmark": row.get("业绩比较基准", ""),
    }


def parse_benchmark_weights(benchmark: str) -> list[tuple[str, float]]:
    """从业绩比较基准字符串里解析出可用于估值百分位的指数及权重。"""
    matches = []
    for name in LG_INDEX_NAMES:
        idx = benchmark.find(name)
        if idx == -1:
            continue
        m = re.search(r"\*\s*(\d+(?:\.\d+)?)\s*%", benchmark[idx:idx + 20])
        weight = float(m.group(1)) / 100 if m else None
        matches.append((name, weight))
    if not matches:
        return []
    if any(w is None for _, w in matches):
        even = 1 / len(matches)
        return [(n, even) for n, _ in matches]
    total = sum(w for _, w in matches)
    return [(n, w / total) for n, w in matches]


def get_index_pe_percentile(index_name: str, lookback_years: int = VALUATION_LOOKBACK_YEARS) -> dict:
    df = ak.stock_index_pe_lg(symbol=index_name)
    df["日期"] = pd.to_datetime(df["日期"])
    cutoff = df["日期"].max() - pd.DateOffset(years=lookback_years)
    window = df[df["日期"] >= cutoff]
    latest = window.iloc[-1]
    pe = latest["滚动市盈率"]
    percentile = (window["滚动市盈率"] < pe).mean() * 100
    return {
        "index": index_name,
        "date": latest["日期"].strftime("%Y-%m-%d"),
        "pe_ttm": round(float(pe), 2),
        "percentile": round(float(percentile), 1),
    }


def get_nav_deviation(code: str) -> dict:
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    df["净值日期"] = pd.to_datetime(df["净值日期"])
    df = df.sort_values("净值日期")
    window = min(250, max(20, int(len(df) * 0.8)))
    ma = df["单位净值"].rolling(window).mean()
    latest_nav = df["单位净值"].iloc[-1]
    latest_ma = ma.iloc[-1]
    deviation = (latest_nav / latest_ma - 1) * 100 if pd.notna(latest_ma) else None
    return {
        "date": df["净值日期"].iloc[-1].strftime("%Y-%m-%d"),
        "nav": round(float(latest_nav), 4),
        "ma_window": window,
        "ma": round(float(latest_ma), 4) if latest_ma is not None else None,
        "deviation_pct": round(float(deviation), 1) if deviation is not None else None,
        "history_days": len(df),
    }


def get_top_holdings(code: str, top_n: int = 10) -> dict | None:
    df, err = _safe(ak.fund_portfolio_hold_em, symbol=code, date="2025")
    if df is None or df.empty:
        return {"error": err or "无持仓数据"}
    latest_q = df["季度"].iloc[-1]
    latest = df[df["季度"] == latest_q].sort_values("占净值比例", ascending=False).head(top_n)
    return {
        "quarter": latest_q,
        "holdings": list(zip(latest["股票名称"], latest["占净值比例"])),
        "total_weight": round(float(latest["占净值比例"].sum()), 1),
    }


def get_industry_allocation(code: str, top_n: int = 5) -> dict | None:
    df, err = _safe(ak.fund_portfolio_industry_allocation_em, symbol=code, date="2025")
    if df is None or df.empty:
        return {"error": err or "无行业配置数据"}
    latest_date = df["截止时间"].max()
    latest = df[df["截止时间"] == latest_date].sort_values("占净值比例", ascending=False).head(top_n)
    return {
        "date": latest_date,
        "allocation": list(zip(latest["行业类别"], latest["占净值比例"])),
        "granularity": "证监会行业分类(一级,较粗)",
    }


def get_performance_rank(code: str) -> dict | None:
    df, err = _safe(ak.fund_individual_achievement_xq, symbol=code)
    if df is None or df.empty:
        return {"error": err or "无同类排名数据"}
    rows = {}
    for _, r in df.iterrows():
        period = r["周期"]
        m = re.match(r"(\d+)/(\d+)", str(r["周期收益同类排名"]))
        rank_pct = round(int(m.group(1)) / int(m.group(2)) * 100, 1) if m else None
        rows[period] = {
            "return_pct": r["本产品区间收益"],
            "max_drawdown_pct": r["本产品最大回撒"],
            "rank_pct": rank_pct,
        }
    return rows


def get_current_position(code: str) -> dict | None:
    df, err = _safe(ak.fund_individual_detail_hold_xq, symbol=code)
    if df is None or df.empty:
        return {"error": err or "无仓位数据"}
    return dict(zip(df["资产类型"], df["仓位占比"]))


def get_market_position_sentiment() -> dict | None:
    df, err = _safe(ak.fund_stock_position_lg)
    if df is None:
        return {"error": err or "无市场仓位数据"}
    df = df.dropna()
    if df.empty:
        return {"error": "无有效市场仓位数据"}
    latest = df.iloc[-1]
    percentile = (df["position"] < latest["position"]).mean() * 100
    return {
        "date": latest["date"],
        "position_pct": round(float(latest["position"]), 1),
        "percentile": round(float(percentile), 1),
        "history_points": len(df),
    }


def build_advice(
    equity_percentile: float | None,
    nav_deviation_pct: float | None,
    market_position: dict | None = None,
    perf_rank: dict | None = None,
) -> str:
    if equity_percentile is None:
        base = "未能解析业绩基准中的可比指数,无法给出估值百分位建议"
    elif equity_percentile < 20:
        base = "对应指数估值处于近10年低位(<20%分位),可正常或加倍定投/建仓"
    elif equity_percentile < 50:
        base = "对应指数估值中性偏低(20-50%分位),维持正常定投节奏"
    elif equity_percentile < 80:
        base = "对应指数估值偏高(50-80%分位),不建议加大额度,维持或减半定投"
    else:
        base = "对应指数估值处于近10年高位(>80%分位),不建议新增建仓,可考虑分批止盈"

    notes = []
    if nav_deviation_pct is not None and nav_deviation_pct > 20:
        notes.append(f"该基金净值短期大幅偏离均线(+{nav_deviation_pct:.1f}%),近期涨幅过热,警惕追高回撤风险")
    elif nav_deviation_pct is not None and nav_deviation_pct < -20:
        notes.append(f"该基金净值大幅低于均线({nav_deviation_pct:.1f}%),需确认是否基本面恶化,并非单纯估值便宜")

    if market_position and market_position.get("percentile") is not None:
        p = market_position["percentile"]
        if p > 85:
            notes.append(f"全市场股票型基金平均仓位处于近{market_position['history_points']}周里的{p}%分位(接近历史高位),机构整体已偏满仓,追高空间和加仓弹性都有限")
        elif p < 15:
            notes.append(f"全市场股票型基金平均仓位处于历史低位({p}%分位),机构整体偏谨慎,若基本面配合,左侧布局的性价比相对更高")

    if perf_rank:
        one_year = perf_rank.get("近1年")
        if one_year and one_year.get("rank_pct") is not None and one_year["rank_pct"] <= 10:
            notes.append(
                f"该基金近1年收益同类排名前{one_year['rank_pct']}%,历史上业绩排名越极端靠前,后续风格降温、跑输均值的概率通常越高,不宜简单线性外推未来收益"
            )

    if notes:
        base += "；" + "；".join(notes)
    return base


def analyze(code: str) -> dict:
    basic = get_fund_basic(code)
    weights = parse_benchmark_weights(basic["benchmark"])

    equity_percentile = None
    index_details = []
    if weights:
        for name, weight in weights:
            detail = get_index_pe_percentile(name)
            detail["weight"] = round(weight, 2)
            index_details.append(detail)
        equity_percentile = sum(d["percentile"] * d["weight"] for d in index_details) / sum(
            d["weight"] for d in index_details
        )

    nav = get_nav_deviation(code)
    holdings = get_top_holdings(code)
    industry = get_industry_allocation(code)
    perf_rank = get_performance_rank(code)
    position = get_current_position(code)
    market_position = get_market_position_sentiment()

    advice = build_advice(equity_percentile, nav["deviation_pct"], market_position, perf_rank)

    return {
        "basic": basic,
        "index_details": index_details,
        "equity_valuation_percentile": round(equity_percentile, 1) if equity_percentile is not None else None,
        "nav": nav,
        "holdings": holdings,
        "industry": industry,
        "perf_rank": perf_rank,
        "position": position,
        "market_position": market_position,
        "advice": advice,
    }


def format_report(result: dict) -> str:
    lines = []
    b = result["basic"]
    lines.append(f"=== {b['code']} {b['name']} ({b['type']}) ===")
    lines.append(f"业绩比较基准: {b['benchmark']}")
    if result["index_details"]:
        lines.append("基准指数估值:")
        for d in result["index_details"]:
            lines.append(f"  - {d['index']} (权重{d['weight']:.0%}): PE(TTM)={d['pe_ttm']}, "
                          f"近10年百分位={d['percentile']}% (截至{d['date']})")
        lines.append(f"加权估值百分位: {result['equity_valuation_percentile']}%")
    else:
        lines.append("基准指数估值: 未匹配到可比指数")

    nav = result["nav"]
    dev = nav["deviation_pct"]
    dev_str = f"{dev:+.1f}%" if dev is not None else "N/A"
    lines.append(f"净值乖离率: 最新净值={nav['nav']} vs MA{nav['ma_window']}={nav['ma']} -> {dev_str} "
                 f"(截至{nav['date']}, 历史{nav['history_days']}个交易日)")

    holdings = result.get("holdings") or {}
    if holdings.get("holdings"):
        lines.append(f"\n前{len(holdings['holdings'])}大重仓股 ({holdings['quarter']}, 合计占净值{holdings['total_weight']}%):")
        lines.append("  " + "、".join(f"{name}({w}%)" for name, w in holdings["holdings"]))
    elif holdings.get("error"):
        lines.append(f"\n重仓股数据: 获取失败({holdings['error']})")

    industry = result.get("industry") or {}
    if industry.get("allocation"):
        lines.append(f"\n行业配置 (截至{industry['date']}, {industry['granularity']}):")
        lines.append("  " + "、".join(f"{name} {w}%" for name, w in industry["allocation"]))
    elif industry.get("error"):
        lines.append(f"\n行业配置数据: 获取失败({industry['error']})")

    position = result.get("position") or {}
    if position and "error" not in position:
        lines.append("\n当前资产仓位: " + "、".join(f"{k} {v}%" for k, v in position.items()))

    perf = result.get("perf_rank") or {}
    if perf and "error" not in perf:
        lines.append("\n阶段业绩及同类排名:")
        for period in ["近3月", "近6月", "近1年", "今年以来", "成立以来"]:
            row = perf.get(period)
            if not row:
                continue
            rank_str = f"同类前{row['rank_pct']}%" if row["rank_pct"] is not None else "排名N/A"
            dd_str = f"最大回撤{row['max_drawdown_pct']:.2f}%" if pd.notna(row["max_drawdown_pct"]) else ""
            lines.append(f"  {period}: 收益{row['return_pct']:.1f}%, {dd_str} {rank_str}")

    mp = result.get("market_position") or {}
    if mp and "error" not in mp:
        lines.append(f"\n全市场股票型基金平均仓位: {mp['position_pct']}% (截至{mp['date']}, 近{mp['history_points']}周历史分位={mp['percentile']}%)")
    elif mp.get("error"):
        lines.append(f"\n全市场基金仓位数据: 获取失败({mp['error']})")

    lines.append(f"\n综合建议: {result['advice']}")
    return "\n".join(lines)


def print_report(result: dict) -> None:
    print(format_report(result))
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 analyze_fund.py <基金代码> [基金代码 ...]")
        sys.exit(1)
    for code in sys.argv[1:]:
        print_report(analyze(code))
