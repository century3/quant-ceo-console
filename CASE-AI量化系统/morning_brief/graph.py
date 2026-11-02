# -*- coding: utf-8 -*-
# Author：@跟陈博士学AI
# 投资晨会 LangGraph 工作流（内嵌于本工作台）
"""
4 节点线性 DAG: industry -> stock_picker -> report -> push

数据为 MySQL wucai_trade；连接见项目根 .env 中 WUCAI_SQL_*。

命令行（CASE-AI 项目根目录）: python -m morning_brief.graph
"""
from __future__ import annotations
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Annotated, List, Dict, TypedDict
from operator import add
import re

from langgraph.graph import END, START, StateGraph

from .pusher import push_all

# 晨报落盘目录（本包内 outputs/reports）
THIS_DIR = Path(__file__).resolve().parent


class MorningState(TypedDict, total=False):
    trigger_time: str
    industry_level: int
    top_n_industries: int
    top_n_stocks: int
    lookback_days: int
    sample_stocks: int
    industry_rank: list
    stock_pool: list
    factor_rank: list
    picked_stocks: list
    report_md: str
    report_html: str
    push_result: dict
    market: str
    messages: Annotated[list, add]


def industry_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 1] industry_node -- 板块强度 + 一二阶导拐点")
    print("=" * 70)

    from .lib.rotation_runner import rank_industries_with_phase
    level   = state.get("industry_level", 2)
    top_n   = state.get("top_n_industries", 5)
    lookback = state.get("lookback_days", 90)
    market = state.get("market") or "cn"
    if market not in ("cn", "us"):
        raise ValueError(f"market 只能是 cn 或 us，收到 {market}")
    if market == "us":
        from .lib.rotation_runner import rank_us_sectors
        print(f"  参数: 美股板块, Top {top_n}, lookback {lookback} 日")
        df = rank_us_sectors(lookback_days=lookback, top_n=top_n)
        level_label = "美股分组"
    else:
        print(f"  参数: 申万{level}级, Top {top_n}, lookback {lookback} 日")
        df = rank_industries_with_phase(level=level, lookback_days=lookback, top_n=top_n)
        level_label = "申万一级" if level == 1 else "申万二级"

    rank_list = []
    for ind_name, row in df.iterrows():
        rank_list.append({
            "industry":   ind_name,
            "rank":       int(row["composite_rank"]),
            "score":      round(float(row["composite_score"]), 3),
            "raw_score":  round(float(row["score"]), 3),
            "MOM_21":     round(float(row["MOM_21"]) * 100, 2),
            "RS_60":      round(float(row["RS_60"]) * 100, 2),
            "VOL_R":      round(float(row["VOL_RATIO"]), 2),
            "phase":      row.get("phase", "neutral"),
            "phase_desc": row.get("phase_desc", "中性"),
            "ROC_20":     round(float(row.get("ROC_20", 0)), 2),
            "members":    int(row["member_count"]),
        })

    print(f"  Top {top_n} 板块 ({level_label}):")
    for r in rank_list:
        print(f"    [{r['rank']:>2}] {r['industry']:<14s} "
              f"score={r['score']:+.2f} ({r['phase_desc']:<6s})  "
              f"MOM21={r['MOM_21']:+5.2f}%  RS60={r['RS_60']:+5.2f}%  "
              f"ROC20={r['ROC_20']:+5.2f}%")

    return {
        "industry_rank": rank_list,
        "messages": [{"role": "industry", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"Top {top_n} 板块: " + ", ".join(r["industry"] for r in rank_list)}],
    }


def stock_picker_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 2] stock_picker_node -- Top 板块成分股做多因子选股")
    print("=" * 70)

    from .lib.rotation_runner import get_sector_member_codes
    from .lib.factor_runner import filter_tradable, calc_factors_batch, preprocess_factors

    industry_rank = state.get("industry_rank", [])
    level = state.get("industry_level", 2)
    market = state.get("market") or "cn"
    if not industry_rank:
        print("  [SKIP] 无行业排名, 跳过选股")
        return {"stock_pool": [], "factor_rank": [], "picked_stocks": []}

    sample_per_industry = state.get("sample_stocks", 30)
    top_stocks: List[str] = []
    industry_to_codes: Dict[str, List[str]] = {}
    kline_table = "trade_stock_us_daily" if market == "us" else "trade_stock_daily"
    us_map = None
    if market == "us":
        from .lib.rotation_runner import load_us_sector_map, us_sector_members
        us_map = load_us_sector_map()
    for r in industry_rank:
        ind_name = r["industry"]
        if market == "us":
            codes = us_sector_members(ind_name, us_map)
        else:
            codes = get_sector_member_codes(ind_name, level=level)
            codes = filter_tradable(codes)
        codes = codes[:sample_per_industry]
        industry_to_codes[ind_name] = codes
        top_stocks.extend(codes)
    top_stocks = sorted(set(top_stocks))
    print(f"  候选股票池: {len(top_stocks)} 只 (来自 {len(industry_rank)} 个 Top 板块, {kline_table})")

    factor_df = calc_factors_batch(top_stocks, table=kline_table)

    if factor_df.empty:
        print("  [WARN] 没有足够日K, 跳过选股")
        return {"stock_pool": top_stocks, "factor_rank": [], "picked_stocks": []}

    ind_map = {c: ind for ind, codes in industry_to_codes.items() for c in codes}

    factor_processed = preprocess_factors(factor_df, industry_map=ind_map, neutralize=True)

    alpha = factor_processed.mean(axis=1).dropna().sort_values(ascending=False)

    top_n = state.get("top_n_stocks", 5)
    picked = []
    factor_rank_list = []
    for code in alpha.head(top_n * 3).index:
        factor_rank_list.append({
            "code":     code,
            "industry": ind_map.get(code, "未分类"),
            "alpha":    round(float(alpha[code]), 3),
            "raw_factors": {k: round(float(v), 3) for k, v in factor_df.loc[code].items()
                            if k in ("MOM_1M", "MOM_3M", "VOL_20", "RSI_14", "BIAS_20")},
        })
    picked = factor_rank_list[:top_n]

    print(f"  Top {top_n} 选中标的:")
    for p in picked:
        print(f"    {p['code']}  [{p['industry']:<6s}]  alpha={p['alpha']:+.3f}  "
              f"MOM_3M={p['raw_factors'].get('MOM_3M', 0):+.2%}")

    return {
        "stock_pool":    top_stocks,
        "factor_rank":   factor_rank_list,
        "picked_stocks": picked,
        "messages": [{"role": "stock_picker", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"选中 {len(picked)} 只: " + ", ".join(p["code"] for p in picked)}],
    }


def report_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 3] report_node -- 拼装晨报")
    print("=" * 70)

    today_str = datetime.now().strftime("%Y-%m-%d %A")
    industries = state.get("industry_rank", [])
    picked = state.get("picked_stocks", [])
    market = state.get("market") or "cn"
    sector_label = "美股分组" if market == "us" else "申万二级"

    md_lines = [
        f"# 晨会分析简报 -- {today_str}",
        "",
        f"## Top {len(industries)} 强势板块 ({sector_label})",
        "",
        "| Rank | 板块 | 综合分 | 拐点信号 | 21日动量 | 60日相对强度 | 20日ROC |",
        "|------|------|--------|----------|----------|--------------|---------|",
    ]
    for r in industries:
        md_lines.append(
            f"| {r['rank']} | **{r['industry']}** | {r['score']:+.2f} | "
            f"{r.get('phase_desc', '中性')} | "
            f"{r['MOM_21']:+.2f}% | {r['RS_60']:+.2f}% | {r.get('ROC_20', 0):+.2f}% |"
        )
    md_lines += ["", f"## Top {len(picked)} 选中标的", ""]
    md_lines.append("| 代码 | 行业 | 综合alpha | 3M动量 |")
    md_lines.append("|------|------|-----------|--------|")
    for p in picked:
        md_lines.append(
            f"| `{p['code']}` | {p['industry']} | {p['alpha']:+.3f} | "
            f"{p['raw_factors'].get('MOM_3M', 0):+.2%} |"
        )

    md_lines += ["", "## 盘中应对建议", ""]
    if picked:
        for p in picked:
            md_lines.append(
                f"- `{p['code']}` ({p['industry']}): "
                f"alpha={p['alpha']:+.3f}, 关注开盘 30 分钟方向"
            )
    else:
        md_lines.append("- 无候选标的, 今日观望")

    md_lines += ["", "---", "",
                 "> 本简报由 AI 量化团队自动生成, 仅供参考, 不构成投资建议",
                 f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

    report_md = "\n".join(md_lines)

    report_html = _md_to_html(report_md)

    output_dir = THIS_DIR / "outputs" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = output_dir / f"morning_brief_{ts}.md"
    html_path = output_dir / f"morning_brief_{ts}.html"
    md_path.write_text(report_md, encoding="utf-8")
    html_path.write_text(report_html, encoding="utf-8")

    print(f"  晨报已落盘:")
    print(f"    Markdown: {md_path}")
    print(f"    HTML:     {html_path}")

    return {
        "report_md":   report_md,
        "report_html": str(html_path),
        "messages": [{"role": "report", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"晨报生成 {len(report_md)} 字节"}],
    }


def _md_to_html(md: str) -> str:
    lines = md.splitlines()
    out = ["<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>",
           "<title>晨会分析简报</title>",
           "<style>",
           "body{font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:900px;margin:30px auto;padding:0 24px;color:#2c3e50;line-height:1.7}",
           "h1{border-bottom:3px solid #3498db;padding-bottom:10px}",
           "h2{color:#3498db;margin-top:30px}",
           "table{border-collapse:collapse;width:100%;margin:14px 0}",
           "th{background:#34495e;color:#fff;padding:8px 12px;text-align:left}",
           "td{padding:8px 12px;border:1px solid #dee2e6}",
           "tr:nth-child(even){background:#f8f9fa}",
           "code{background:#e8ecef;padding:2px 6px;border-radius:4px;font-family:'Consolas',monospace}",
           "blockquote{border-left:3px solid #95a5a6;color:#555;padding-left:12px;background:#f1f3f5;padding-top:8px;padding-bottom:8px}",
           "</style></head><body>"]

    in_table = False
    table_rows = []
    for line in lines:
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.match(r"^-+$", c) for c in cells):
                continue
            if not in_table:
                in_table = True
                table_rows = ["<table><thead><tr>"]
                for c in cells:
                    table_rows.append(f"<th>{escape(c)}</th>")
                table_rows.append("</tr></thead><tbody>")
            else:
                table_rows.append("<tr>")
                for c in cells:
                    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(c))
                    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
                    table_rows.append(f"<td>{rendered}</td>")
                table_rows.append("</tr>")
            continue
        else:
            if in_table:
                table_rows.append("</tbody></table>")
                out.extend(table_rows)
                in_table = False
                table_rows = []

        if s.startswith("# "):
            out.append(f"<h1>{escape(s[2:])}</h1>")
        elif s.startswith("## "):
            out.append(f"<h2>{escape(s[3:])}</h2>")
        elif s.startswith("- "):
            li_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(s[2:]))
            out.append(f"<li>{li_html}</li>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{escape(s[2:])}</blockquote>")
        elif s == "---":
            out.append("<hr>")
        elif s == "":
            continue
        else:
            p_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(s))
            out.append(f"<p>{p_html}</p>")

    if in_table:
        table_rows.append("</tbody></table>")
        out.extend(table_rows)

    out.append("</body></html>")
    return "\n".join(out)


def push_node(state: dict) -> dict:
    print("\n" + "=" * 70)
    print("  [节点 4] push_node -- 推送钉钉 / 企业微信")
    print("=" * 70)

    title = f"晨会分析 {datetime.now().strftime('%m-%d')}"
    md = state.get("report_md", "")
    if not md:
        print("  [SKIP] 无内容可推送")
        return {"push_result": {}}

    result = push_all(title=title, content=md)
    return {
        "push_result": result,
        "messages": [{"role": "push", "time": datetime.now().strftime("%H:%M:%S"),
                      "content": f"推送结果: {result}"}],
    }


def build_graph():
    g = StateGraph(MorningState)
    g.add_node("industry",     industry_node)
    g.add_node("stock_picker", stock_picker_node)
    g.add_node("report",       report_node)
    g.add_node("push",         push_node)

    g.add_edge(START, "industry")
    g.add_edge("industry", "stock_picker")
    g.add_edge("stock_picker", "report")
    g.add_edge("report", "push")
    g.add_edge("push", END)

    return g.compile()


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    import argparse
    parser = argparse.ArgumentParser(description="晨会分析工作流（读库）")
    parser.add_argument("--level", type=int, choices=[1, 2], default=2,
                        help="申万级别 (默认 2 二级板块)")
    parser.add_argument("--top-industries", type=int, default=5,
                        help="选 Top N 强势板块 (默认 5)")
    parser.add_argument("--top-stocks", type=int, default=5,
                        help="最终输出 Top N 选股 (默认 5)")
    parser.add_argument("--sample-per-industry", type=int, default=20,
                        help="每个板块选取多少只候选股 (默认 20)")
    parser.add_argument("--lookback", type=int, default=90,
                        help="拉多少日 K 线 (默认 90)")
    parser.add_argument("--market", choices=["cn", "us"], default="cn",
                        help="cn=申万+A股日线, us=美股分组+trade_stock_us_daily")
    args = parser.parse_args()

    print()
    print("#" * 70)
    print("# 投资晨会工作流启动")
    print(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 70)

    graph = build_graph()
    result = graph.invoke({
        "trigger_time":     datetime.now().isoformat(timespec="seconds"),
        "industry_level":   args.level,
        "top_n_industries": args.top_industries,
        "top_n_stocks":     args.top_stocks,
        "lookback_days":    args.lookback,
        "sample_stocks":    args.sample_per_industry,
        "market":           args.market,
        "messages":         [],
    })

    print("\n" + "#" * 70)
    print("# 工作流执行完成")
    print("#" * 70)
    print()
    print("--- 节点对话历史 ---")
    for m in result.get("messages", []):
        print(f"  [{m['time']}] {m['role']:<14s} | {m['content']}")
    print()
    print(f"晨报路径 (HTML): {result.get('report_html', '')}")
    print(f"推送结果:        {result.get('push_result', {})}")


if __name__ == "__main__":
    main()
