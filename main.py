#!/usr/bin/env python3
"""
做T信号系统 v3.3 · 主程序
=================================
升级：RR盈亏比过滤 + 自动止损 + 人话结论
手动：python main.py --once
日内多时段：python main.py --intraday
回测：python main.py --backtest
"""

import os
import sys
import time
import datetime
import argparse
import requests
import schedule

sys.path.insert(0, os.path.dirname(__file__))

from config import STOCKS, WEBHOOK, CAPITAL, T_POSITION_RATIO, SCHEDULE_TIMES
from market_data import fetch_market_snapshot, MarketSnapshot
from signal_gate import calc_signal_gate, GATE_RULES, get_freq_status
from backtest import backtest, print_backtest_report


# ────────────────────────── 推送 ──────────────────────────
def push_to_feishu(msg: str) -> bool:
    if "REPLACE_WITH_YOUR_WEBHOOK" in WEBHOOK or not WEBHOOK.strip():
        print("WARNING: Webhook not configured")
        return False
    try:
        data = {"msg_type": "text", "content": {"text": msg}}
        resp = requests.post(WEBHOOK, json=data, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print("Push error: " + str(e))
        return False


def push(msg: str):
    sep = "=" * 55
    print(sep)
    print(msg)
    print(sep)
    push_to_feishu(msg)


# ────────────────────────── 信号生成 ──────────────────────────
def gen_signal(name: str, code: str) -> dict:
    try:
        snap = fetch_market_snapshot(code, name)
    except Exception as e:
        return {"name": name, "code": code, "error": "fetch failed: " + str(e)}
    if not snap.fetch_success:
        return {"name": name, "code": code, "error": snap.error_msg}
    sig = calc_signal_gate(snap)
    return {"name": name, "code": code, **sig, "snap": snap}


def format_signal(s: dict) -> str:
    """
    v3.3 人话格式：结论在前，原因在后
    """
    if "error" in s:
        return "\n【" + s["name"] + " " + s["code"] + "】ERROR: " + s["error"]

    snap = s.get("snap")

    # ── 数据新鲜度 ──
    fresh = "CURRENT" if not s.get("is_expired") else "STALE"
    age = s.get("data_age", "")

    # ── 人话结论（第一行，醒目） ──
    verdict = s.get("human_verdict", "[UNKNOWN]")
    pos_pct = int(s["position_ratio"] * 100)
    pos_note = s.get("position_note", "")

    # ── 做T区间 ──
    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]

    # ── RR信息 ──
    rr = s.get("rr", 0)
    sl_price = s.get("sl_price", 0)
    tp_price = s.get("tp_price", 0)
    sl_dist = s.get("sl_distance", 0)
    tp_dist = s.get("tp_distance", 0)

    # ── 仓位描述 ──
    if pos_pct >= 45:
        pos_icon = "HIGH"
    elif pos_pct >= 25:
        pos_icon = "MED"
    elif pos_pct > 0:
        pos_icon = "LOW"
    else:
        pos_icon = "NONE"

    used_capital = int(CAPITAL * s["position_ratio"])

    # ── 门禁详情 ──
    gate_lines = []
    for gid, g in s.get("gates", {}).items():
        icon = "[OK]" if g["pass"] else "[X]"
        gate_lines.append("  " + icon + " " + g["name"] + ": " + g["value"])

    # ── 趋势 ──
    trend = "MA5上方" if s.get("above_ma5") else "MA5下方"

    # ── 频率 ──
    freq = s.get("freq_display", "")

    # ── 组装 ──
    parts = []

    # 标题
    parts.append("\n" + "=" * 48)
    parts.append("【" + s["name"] + "】" + str(s.get("data_date", "")) + " | " + fresh)
    parts.append("=" * 48)

    # 第1段：结论（最醒目）
    parts.append(">>> " + verdict)
    parts.append("")

    # 第2段：操作计划
    if s["gate_passed"]:
        parts.append("[操作计划]")
        parts.append("  买区间: " + str(buy1) + " ~ " + str(buy2))
        parts.append("  卖区间: " + str(sell1) + " ~ " + str(sell2))
        parts.append("  仓位: " + pos_icon + " " + str(pos_pct) + "%" +
                     (" (" + pos_note + ")" if pos_note else "") +
                     " = " + str(used_capital) + "元")
    else:
        parts.append("[操作计划] 无建议，观望")

    # 第3段：RR盈亏比
    parts.append("")
    parts.append("[盈亏比分析]")
    parts.append("  买价(参考): " + str(buy1) + " | 卖价(目标): " + str(sell1))
    parts.append("  止损: " + str(sl_price) + " (跌" +
                 ("%.1f%%" % (s.get("stop_loss_pct", 0) * 100)) +
                 "，风险" + str(sl_dist) + "元)")
    parts.append("  止盈: " + str(tp_price) + " (盈利" + str(tp_dist) + "元)")
    parts.append("  RR = " + ("%.2f" % rr) + "  (" + s.get("rr_reason", "") + ")")

    # 第4段：基础信号
    parts.append("")
    parts.append("[信号详情]")
    parts.append("  评分: " + str(s["score"]) + "/4 -> " + str(s.get("reject_reason") or "GATE PASS"))
    parts.append("  趋势: " + trend)
    parts.append("  振幅: " + ("%.2f%%" % (s["amplitude"] * 100)) +
                 " | 涨跌: " + ("%+.2f%%" % s["pct_change"]))
    parts.append("  频率: " + freq)
    parts.append("  昨收价: " + str(s.get("close", 0)))

    # 第5段：门禁明细
    parts.append("")
    parts.append("[门禁明细]")
    parts.extend(gate_lines)

    # 第6段：数据状态
    parts.append("")
    parts.append("[数据状态]")
    parts.append("  Unified价格: " + ("%.2f" % s.get("unified_price", 0)))
    parts.append("  数据获取: " + age + " | " + s.get("price_label", ""))

    return "\n".join(parts)


# ────────────────────────── 每日例行 ──────────────────────────
def run_daily_signal(intraday: bool = False):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "[INTRA]" if intraday else "[CLOSE]"
    lines = []
    lines.append("=== T0 Signal v3.3 " + mode + " === " + now)

    for name, code in STOCKS.items():
        print("\nProcessing: " + name + " (" + code + ")...")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    footer_lines = [
        "=" * 48,
        "[规则说明]",
        "GATE: Score>=3 + Amp>=4% 同时满足",
        "RR:   RR<1.5 = SKIP（不划算）| RR>2.0 可加仓25%",
        "仓位: Score4=50% | Score3+MA5上=30% | Score3+MA5下=25%",
        "止损: buy1 - R*10%（区间下方10%处）",
        "频率: 每日最多2次 | 冷静期30分钟 | 5分钟去重",
    ]
    lines.extend(footer_lines)

    msg = "\n".join(lines)
    push(msg)
    return True


# ────────────────────────── 回测入口 ──────────────────────────
def run_backtest():
    sep = "=" * 50
    print("\n" + sep)
    print("  BACKTEST MODE (v3.3 RR)")
    print(sep)
    for name, code in STOCKS.items():
        print("\n> " + name + " (" + code + ")")
        try:
            from market_data import get_daily_df
            df = get_daily_df(code, days=120)
            snap = fetch_market_snapshot(code, name)
            result = backtest(df, name, snap=snap, use_gate=True, use_rr=True)
            print_backtest_report(result)
        except Exception as e:
            import traceback
            print("  Error: " + str(e))
            traceback.print_exc()


# ────────────────────────── 入口 ──────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="T0 Signal v3.3")
    parser.add_argument("--once", action="store_true", help="Run once (close)")
    parser.add_argument("--intraday", action="store_true", help="Run intraday scheduler")
    parser.add_argument("--backtest", action="store_true", help="Backtest mode")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.once:
        run_daily_signal(intraday=False)
    else:
        # 默认：日内多时段调度
        for t in SCHEDULE_TIMES:
            schedule.every().day.at(t).do(run_daily_signal, intraday=True)
        banner = [
            "========================================",
            "  T0 Signal v3.3 - Scheduler Started",
            "  Slots: " + ", ".join(SCHEDULE_TIMES),
            "  Stocks: " + ", ".join(STOCKS.keys()),
            "  RR filter: >=1.5 required",
            "========================================",
        ]
        print("\n".join(banner))
        while True:
            schedule.run_pending()
            time.sleep(30)