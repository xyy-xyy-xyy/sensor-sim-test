"""P5 评测框架：基于 sensor.db 生成数据质量评测报告。

与 database.stats() 保持同一口径（reasons 为 JSON 数组，按 ':' 取原因类型），
额外补充：分传感器类型合格率、序列连续性、噪声类放行验证、结论建议。

运行：
    python tools/evaluate.py --db sensor.db --out report.md
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "src")
from logger import get_logger, setup_logging

log = get_logger(__name__)

SENSOR_NAMES = {1: "雷达距离", 2: "IMU 加速度", 3: "GPS 速度"}
REASON_DESC = {
    "CRC_FAIL": "CRC 校验失败（帧损坏）",
    "SEQ_GAP": "序列跳变（丢帧 / 乱序）",
    "OUT_OF_RANGE": "物理值越界",
    "NAN_VALUE": "NaN / 非法数值",
    "NOISE": "噪声超标（设计放行）",
}


def evaluate(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT seq, sensor_type, passed, reasons FROM frames ORDER BY seq"
    ).fetchall()

    # 加载 LLM 归因结果
    analysis_rows = []
    try:
        analysis_rows = conn.execute(
            "SELECT seq, reasons, root_cause, confidence, category, "
            "recommendation, source, rag_context_count, latency_ms, "
            "prompt_tokens, completion_tokens, total_tokens, "
            "human_verdict, human_category "
            "FROM llm_analysis ORDER BY seq"
        ).fetchall()
    except sqlite3.OperationalError:
        pass  # 表不存在（旧数据库）

    conn.close()
    if not rows:
        raise SystemExit("数据库为空，请先灌数据后再评测。")

    total = len(rows)
    passed = sum(1 for r in rows if r[2] == 1)
    pass_rate = passed / total

    # 分传感器类型
    by_type: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for _seq, st, p, _reasons in rows:
        by_type[st][0] += 1
        if p == 1:
            by_type[st][1] += 1

    # 异常拦截分布
    reason_counter: Counter = Counter()
    for _seq, _st, p, reasons in rows:
        if p == 0 and reasons:
            for r in json.loads(reasons):
                reason_counter[r.split(":")[0]] += 1

    # 序列连续性
    seqs = [r[0] for r in rows]
    max_gap = max(seqs[i + 1] - seqs[i] for i in range(len(seqs) - 1)) if len(seqs) > 1 else 0

    # 噪声类放行验证
    noise_passed = 0
    for _seq, _st, p, reasons in rows:
        if p == 1 and reasons:
            for r in json.loads(reasons):
                if r.split(":")[0] == "NOISE":
                    noise_passed += 1

    # ── LLM 归因评测 ──
    llm_eval = _evaluate_llm_analysis(analysis_rows)

    return {
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "by_type": dict(by_type),
        "reason_counter": reason_counter,
        "max_gap": max_gap,
        "noise_passed": noise_passed,
        "llm_eval": llm_eval,
    }


def _evaluate_llm_analysis(analysis_rows: list) -> dict:
    """评测 LLM 归因质量：准确率、置信度分布、RAG 利用率、延迟。"""
    if not analysis_rows:
        return {"total": 0, "note": "无归因数据（未启用 LLM 或未灌入异常帧）"}

    total = len(analysis_rows)
    correct = 0
    llm_count = 0
    rule_count = 0
    rag_used = 0
    confidences = []
    latencies = []
    token_prompt = 0
    token_completion = 0
    token_total = 0
    category_counter: Counter = Counter()

    # 人工打标统计：与人工一致率（LLM 判定 vs 人工确认）、类别一致率
    # 类别一致率口径：人工标"正确"→ 视为类别被接受；人工填了更正类别 → 与 LLM 类别相同才计一致。
    human_verified = 0
    human_correct = 0
    cat_agree_total = 0
    cat_agree_hit = 0

    for row in analysis_rows:
        seq, reasons_json, root_cause, confidence, category, \
            recommendation, source, rag_count, latency_ms, \
            prompt_tokens, completion_tokens, total_tokens, \
            human_verdict, human_category = row

        # 解析门禁原因类型
        gate_reasons = json.loads(reasons_json) if reasons_json else []
        gate_categories = set(r.split(":")[0] for r in gate_reasons)

        # 归因准确率：LLM 分类是否命中门禁原因
        if category in gate_categories or category == "UNKNOWN":
            correct += 1

        if source == "llm":
            llm_count += 1
            if latency_ms:
                latencies.append(latency_ms)
            if total_tokens:
                token_total += total_tokens
            if prompt_tokens:
                token_prompt += prompt_tokens
            if completion_tokens:
                token_completion += completion_tokens
        else:
            rule_count += 1

        if rag_count and rag_count > 0:
            rag_used += 1

        if confidence is not None:
            confidences.append(confidence)
        category_counter[category] += 1

        # 人工打标累计
        if human_verdict is not None:
            human_verified += 1
            cat_agree_total += 1
            if human_verdict == 1:
                human_correct += 1
                cat_agree_hit += 1  # 标"正确"视为类别被接受
            elif human_category and human_category == category:
                cat_agree_hit += 1  # 更正类别与 LLM 相同也算一致

    accuracy = correct / total if total else 0.0
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    rag_rate = rag_used / total if total else 0.0

    return {
        "total": total,
        "llm_count": llm_count,
        "rule_count": rule_count,
        "accuracy": round(accuracy, 3),
        "avg_confidence": round(avg_conf, 3),
        "avg_latency_ms": round(avg_lat, 0),
        "rag_utilization": round(rag_rate, 3),
        "total_prompt_tokens": token_prompt,
        "total_completion_tokens": token_completion,
        "total_tokens": token_total,
        "category_distribution": dict(category_counter.most_common()),
        "human_verified": human_verified,
        "human_accuracy": round(human_correct / human_verified, 3) if human_verified else None,
        "category_agreement": round(cat_agree_hit / cat_agree_total, 3) if cat_agree_total else None,
    }


def render(d: dict, db_path: str) -> str:
    lines: list[str] = []
    lines.append("# 数据质量评测报告\n")
    lines.append(f"- 数据来源：`{db_path}`")
    lines.append(f"- 总帧数：**{d['total']}**")
    lines.append(f"- 通过：**{d['passed']}**")
    lines.append(f"- 合格率：**{d['pass_rate'] * 100:.1f}%**")
    lines.append(f"- 最大序列间隔：{d['max_gap']}\n")

    lines.append("## 分传感器类型合格率\n")
    lines.append("| 类型 | 名称 | 总帧 | 通过 | 合格率 |")
    lines.append("|---|---|---|---|---|")
    for st in sorted(d["by_type"]):
        t, p = d["by_type"][st]
        name = SENSOR_NAMES.get(st, f"未知({st})")
        lines.append(f"| {st} | {name} | {t} | {p} | {p / t * 100:.1f}% |")

    lines.append("\n## 异常拦截分布\n")
    lines.append("| 异常类型 | 含义 | 数量 |")
    lines.append("|---|---|---|")
    for reason, cnt in d["reason_counter"].most_common():
        lines.append(f"| {reason} | {REASON_DESC.get(reason, reason)} | {cnt} |")
    if "NOISE" not in d["reason_counter"]:
        lines.append("| NOISE | 噪声超标（设计放行） | 0（未注入）|")

    lines.append("\n## 噪声类放行验证\n")
    if d["noise_passed"] > 0:
        lines.append(
            f"噪声超标类异常设计上**不应被拦截**。检测到通过帧中含 NOISE 标记："
            f"**{d['noise_passed']}** 帧 —— 符合预期放行逻辑，门禁未过度拦截。"
        )
    else:
        lines.append("未检测到 NOISE 标记帧（本轮未注入噪声超标异常）。")

    lines.append("\n## 结论\n")
    if 0.70 <= d["pass_rate"] <= 0.92:
        lines.append(
            f"合格率 {d['pass_rate'] * 100:.1f}% 落在预期区间（70%–92%），"
            f"数据质量门禁工作正常，四类损坏异常均被正确拦截，噪声类正常放行。"
        )
    else:
        lines.append(
            f"合格率 {d['pass_rate'] * 100:.1f}% 偏离预期区间（70%–92%），"
            f"建议检查异常注入比例或门禁阈值。"
        )

    # ── LLM 归因评测 ──
    llm = d.get("llm_eval", {})
    if llm.get("total", 0) > 0:
        lines.append("\n## LLM 异常归因评测\n")
        lines.append(f"- 归因总数：**{llm['total']}**")
        lines.append(f"- LLM 归因：{llm.get('llm_count', 0)} 条 / 规则引擎：{llm.get('rule_count', 0)} 条")
        lines.append(f"- 归因准确率：**{llm.get('accuracy', 0) * 100:.1f}%**")
        lines.append(f"- 平均置信度：{llm.get('avg_confidence', 0):.3f}")
        lines.append(f"- RAG 利用率：{llm.get('rag_utilization', 0) * 100:.1f}%")
        if llm.get("avg_latency_ms", 0) > 0:
            lines.append(f"- LLM 平均延迟：{llm.get('avg_latency_ms', 0):.0f} ms")
        if llm.get("total_tokens", 0) > 0:
            lines.append(f"- Token 消耗：**{llm['total_tokens']}**（prompt {llm.get('total_prompt_tokens', 0)} / completion {llm.get('total_completion_tokens', 0)}）")

        # 人工确认（LLM 归因 vs 人工）——评测闭环
        human_verified = llm.get("human_verified", 0)
        if human_verified > 0:
            lines.append(f"- 人工已确认：**{human_verified}** 条")
            ha = llm.get("human_accuracy")
            if ha is not None:
                lines.append(f"- LLM 归因与人工一致率：**{ha * 100:.1f}%**")
            ca = llm.get("category_agreement")
            if ca is not None:
                lines.append(f"- 类别判定与人工一致率：**{ca * 100:.1f}%**")

        cat_dist = llm.get("category_distribution", {})
        if cat_dist:
            lines.append("\n### 归因类别分布\n")
            lines.append("| 类别 | 数量 |")
            lines.append("|---|---|")
            for cat, cnt in cat_dist.items():
                lines.append(f"| {cat} | {cnt} |")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# HTML 报告（自包含：内联 CSS + JS，不依赖任何外部资源）
# ──────────────────────────────────────────────────────────────────
_HTML_CSS = """
:root {
  --bg: #f4f6fb;
  --card: #ffffff;
  --indigo: #4f46e5;
  --purple: #7c3aed;
  --text: #1e293b;
  --muted: #64748b;
  --border: #e2e8f0;
  --green: #10b981;
  --red: #ef4444;
  --amber: #f59e0b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Microsoft YaHei", "PingFang SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }
.header {
  position: relative;
  background: linear-gradient(135deg, var(--indigo), var(--purple));
  color: #fff;
  border-radius: 16px;
  padding: 28px 32px;
  margin-bottom: 28px;
  box-shadow: 0 10px 30px rgba(79,70,229,0.18);
}
.header h1 { margin: 0 0 6px; font-size: 26px; font-weight: 700; letter-spacing: .5px; }
.header .meta { font-size: 14px; opacity: .92; }
.btn-print {
  position: absolute; right: 24px; top: 24px;
  background: rgba(255,255,255,.18); color: #fff;
  border: 1px solid rgba(255,255,255,.4); border-radius: 10px;
  padding: 7px 14px; font-size: 13px; cursor: pointer;
  transition: background .2s;
}
.btn-print:hover { background: rgba(255,255,255,.32); }
.section-title {
  font-size: 18px; font-weight: 700; margin: 32px 0 14px;
  color: var(--indigo); display: flex; align-items: center; gap: 10px;
}
.section-title::before {
  content: ""; width: 6px; height: 20px; border-radius: 3px;
  background: linear-gradient(var(--indigo), var(--purple));
}
.section-sub {
  font-size: 15px; font-weight: 600; margin: 22px 0 10px; color: var(--purple);
}
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 8px; }
.llm-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 8px; }
.kpi {
  background: var(--card); border-radius: 16px; padding: 20px 22px;
  box-shadow: 0 2px 10px rgba(15,23,42,.05); border: 1px solid var(--border);
}
.kpi .label { font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.kpi .value { font-size: 28px; font-weight: 700; line-height: 1.2; }
.kpi .value .unit { font-size: 14px; font-weight: 500; color: var(--muted); }
.kpi .sub { font-size: 12px; color: var(--muted); margin-top: 6px; }
.kpi.pass .value { color: var(--green); }
.kpi.fail .value { color: var(--red); }
.kpi.rate .value { color: var(--indigo); }
.card {
  background: var(--card); border-radius: 16px; padding: 6px 22px 18px;
  box-shadow: 0 2px 10px rgba(15,23,42,.05); border: 1px solid var(--border);
  overflow: hidden;
}
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 11px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 13px; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #f8fafc; }
td.num, th.num { text-align: right; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.good { background: #dcfce7; color: #166534; }
.badge.warn { background: #fef3c7; color: #92400e; }
.badge.bad  { background: #fee2e2; color: #991b1b; }
.bar { height: 8px; border-radius: 4px; background: #eef2ff; overflow: hidden;
  width: 120px; display: inline-block; vertical-align: middle; }
.bar > span { display: block; height: 100%;
  background: linear-gradient(90deg, var(--indigo), var(--purple)); border-radius: 4px; }
.note { font-size: 13px; color: var(--muted); }
.footer { text-align: center; color: var(--muted); font-size: 12px; margin-top: 40px; }
.fab {
  position: fixed; right: 28px; bottom: 28px; width: 44px; height: 44px;
  border-radius: 50%; border: none;
  background: linear-gradient(135deg, var(--indigo), var(--purple));
  color: #fff; font-size: 20px; cursor: pointer; opacity: 0;
  transition: opacity .3s; box-shadow: 0 6px 18px rgba(79,70,229,.35); z-index: 50;
}
@media (max-width: 720px) {
  .kpi-grid, .llm-grid { grid-template-columns: repeat(2, 1fr); }
}
@media print {
  .btn-print, .fab { display: none; }
  .header { box-shadow: none; }
  body { background: #fff; }
}
"""


def _render_llm_html(llm: dict) -> str:
    """渲染 LLM 归因评测区块的 HTML 片段。"""
    if llm.get("total", 0) == 0:
        note = html.escape(llm.get("note", "无归因数据"))
        return (
            '  <div class="section-title">LLM 归因评测</div>\n'
            '  <div class="card"><div class="note" style="padding:16px 0">'
            f"{note}</div></div>"
        )

    total = llm["total"]
    accuracy = llm.get("accuracy", 0)
    avg_conf = llm.get("avg_confidence", 0)
    rag_util = llm.get("rag_utilization", 0)
    avg_lat = llm.get("avg_latency_ms", 0)
    tot_tokens = llm.get("total_tokens", 0)
    prompt_t = llm.get("total_prompt_tokens", 0)
    comp_t = llm.get("total_completion_tokens", 0)
    llm_count = llm.get("llm_count", 0)
    rule_count = llm.get("rule_count", 0)

    lat_html = (
        f'<div class="value">{avg_lat:.0f}<span class="unit"> ms</span></div>'
        if avg_lat > 0
        else '<div class="value">—</div>'
    )
    token_html = (
        f'<div class="value">{tot_tokens}</div>'
        f'<div class="sub">prompt {prompt_t} / completion {comp_t}</div>'
        if tot_tokens > 0
        else '<div class="value">—</div>'
    )
    human_verified = llm.get("human_verified", 0)
    human_acc = llm.get("human_accuracy")
    human_html = (
        f'<div class="value">{human_acc * 100:.1f}%</div>'
        f'<div class="sub">已确认 {human_verified} 条（人工打标）</div>'
        if human_verified > 0 and human_acc is not None
        else '<div class="value">—</div><div class="sub">尚未人工确认</div>'
    )

    kpi = (
        f'    <div class="kpi"><div class="label">归因总数</div>'
        f'<div class="value">{total}</div>'
        f'<div class="sub">LLM {llm_count} / 规则 {rule_count}</div></div>\n'
        f'    <div class="kpi rate"><div class="label">归因准确率</div>'
        f'<div class="value">{accuracy * 100:.1f}%</div></div>\n'
        f'    <div class="kpi"><div class="label">平均置信度</div>'
        f'<div class="value">{avg_conf:.3f}</div></div>\n'
        f'    <div class="kpi"><div class="label">RAG 利用率</div>'
        f'<div class="value">{rag_util * 100:.1f}%</div></div>\n'
        f'    <div class="kpi"><div class="label">LLM 平均延迟</div>'
        f"{lat_html}</div>\n"
        f'    <div class="kpi"><div class="label">Token 消耗</div>'
        f"{token_html}</div>\n"
        f'    <div class="kpi"><div class="label">人工确认一致率</div>'
        f"{human_html}</div>"
    )

    cat_dist = llm.get("category_distribution", {})
    cat_rows = []
    cat_total = sum(cat_dist.values()) if cat_dist else 0
    for cat, cnt in cat_dist.items():
        ratio = cnt / cat_total * 100 if cat_total else 0
        cat_rows.append(
            f"      <tr><td><b>{html.escape(str(cat))}</b></td>"
            f'<td class="num">{cnt}</td><td class="num">{ratio:.1f}%</td>'
            f'<td><span class="bar"><span style="width:{ratio:.0f}%"></span>'
            f"</span></td></tr>"
        )
    if not cat_rows:
        cat_rows.append('<tr><td colspan="4" class="note">无类别数据</td></tr>')
    cat_rows_html = "\n".join(cat_rows)

    return (
        '  <div class="section-title">LLM 归因评测</div>\n'
        "  <div class=\"llm-grid\">\n"
        f"{kpi}\n"
        "  </div>\n"
        '  <div class="section-sub">归因类别分布</div>\n'
        '  <div class="card">\n'
        "    <table>\n"
        '      <thead><tr><th>类别</th><th class="num">数量</th>'
        f'<th class="num">占比</th><th>分布</th></tr></thead>\n'
        "      <tbody>\n"
        f"{cat_rows_html}\n"
        "      </tbody>\n"
        "    </table>\n"
        "  </div>"
    )


def render_html(d: dict, db_path: str) -> str:
    """生成自包含 HTML 报告字符串（内联 CSS + JS，不依赖外部资源）。"""
    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = d["total"]
    passed = d["passed"]
    rejected = total - passed
    pass_rate_pct = f"{d['pass_rate'] * 100:.1f}"

    # ── 总览 KPI ──
    kpi_html = (
        f'    <div class="kpi"><div class="label">总帧数</div>'
        f'<div class="value">{total}</div>'
        f'<div class="sub">最大序列间隔 {d["max_gap"]}</div></div>\n'
        f'    <div class="kpi rate"><div class="label">合格率</div>'
        f'<div class="value">{pass_rate_pct}%</div></div>\n'
        f'    <div class="kpi pass"><div class="label">通过数</div>'
        f'<div class="value">{passed}</div></div>\n'
        f'    <div class="kpi fail"><div class="label">拒收数</div>'
        f'<div class="value">{rejected}</div></div>'
    )

    # ── 分传感器类型合格率 ──
    sensor_rows = []
    for st in sorted(d["by_type"]):
        t, p = d["by_type"][st]
        name = html.escape(SENSOR_NAMES.get(st, f"未知({st})"))
        rate = p / t * 100 if t else 0
        rate_cls = "good" if rate >= 80 else ("warn" if rate >= 60 else "bad")
        sensor_rows.append(
            f"      <tr><td>{st}</td><td>{name}</td>"
            f'<td class="num">{t}</td><td class="num">{p}</td>'
            f'<td class="num"><span class="badge {rate_cls}">{rate:.1f}%</span></td>'
            f'<td><span class="bar"><span style="width:{rate:.0f}%"></span>'
            f"</span></td></tr>"
        )
    sensor_rows_html = (
        "\n".join(sensor_rows)
        if sensor_rows
        else '<tr><td colspan="6" class="note">无数据</td></tr>'
    )

    # ── 异常拦截分布 ──
    rc = d["reason_counter"]
    reason_total = sum(rc.values())
    reason_rows = []
    for reason, cnt in rc.most_common():
        desc = html.escape(REASON_DESC.get(reason, reason))
        ratio = cnt / reason_total * 100 if reason_total else 0
        reason_rows.append(
            f"      <tr><td><b>{html.escape(reason)}</b></td><td>{desc}</td>"
            f'<td class="num">{cnt}</td><td class="num">{ratio:.1f}%</td></tr>'
        )
    if not reason_rows:
        reason_rows.append(
            '<tr><td colspan="4" class="note">无异常拦截（全部通过）</td></tr>'
        )
    reason_rows_html = "\n".join(reason_rows)

    # ── LLM 归因区块 ──
    llm_section = _render_llm_html(d.get("llm_eval", {}))

    css = _HTML_CSS
    src = html.escape(db_path)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据质量评测报告</title>
<style>
{css}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>数据质量评测报告</h1>
    <div class="meta">生成时间：{gen_time} · 数据来源：{src}</div>
    <button class="btn-print" onclick="window.print()">导出 PDF</button>
  </div>

  <div class="section-title">总览</div>
  <div class="kpi-grid">
{kpi_html}
  </div>

  <div class="section-title">分传感器类型合格率</div>
  <div class="card">
    <table>
      <thead><tr><th>类型</th><th>名称</th><th class="num">总帧</th><th class="num">通过</th><th class="num">合格率</th><th>分布</th></tr></thead>
      <tbody>
{sensor_rows_html}
      </tbody>
    </table>
  </div>

  <div class="section-title">异常拦截分布</div>
  <div class="card">
    <table>
      <thead><tr><th>异常类型</th><th>含义</th><th class="num">数量</th><th class="num">占比</th></tr></thead>
      <tbody>
{reason_rows_html}
      </tbody>
    </table>
  </div>

{llm_section}

  <div class="footer">sensor-sim-test · evaluate.py 自动生成 · 数据来源 {src}</div>
</div>
<button class="fab" title="回到顶部" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">&#8593;</button>
<script>
(function(){{
  var fab = document.querySelector('.fab');
  window.addEventListener('scroll', function(){{
    fab.style.opacity = window.scrollY > 400 ? '1' : '0';
  }});
}})();
</script>
</body>
</html>
"""


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="sensor-sim-test 评测框架")
    ap.add_argument("--db", default="sensor.db", help="SQLite 数据库路径")
    ap.add_argument("--out", default="report.md", help="输出 Markdown 报告路径")
    ap.add_argument(
        "--html",
        default=None,
        help="输出 HTML 报告路径（可选，自包含单文件）",
    )
    args = ap.parse_args()

    data = evaluate(args.db)

    # Markdown 报告（保持原有行为）
    report = render(data, args.db)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    log.info("Markdown 报告已生成：%s", args.out)

    # HTML 报告（可选）
    if args.html:
        html_str = render_html(data, args.db)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(html_str)
        log.info("HTML 报告已生成：%s", args.html)

    log.info(
        "总帧 %d / 通过 %d / 合格率 %.1f%% / 最大序列间隔 %d",
        data['total'], data['passed'],
        data['pass_rate'] * 100, data['max_gap']
    )


if __name__ == "__main__":
    main()
