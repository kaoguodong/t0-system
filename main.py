#!/usr/bin/env python3
"""
做T信号系统 v3.1 工业级 · 主程序
=================================
核心升级：
- 统一行情层（Single Source of Truth）：MarketSnapshot
- 时间锁：信号5分钟过期机制
- 价格口径统一：门禁/信号/执行均用同一价格基准
调度：每日 15:10（北京时间）自动推送
手动运行：python main.py --once
回测模式：python main.py --backtest
"""

import os
import sys
import time
import datetime
import argparse
import requests
import schedule

sys.path.insert(0, os.path.dirname(__file__))

from config import STOCKS, WEBHOOK, CAPITAL, T_POSITION_RATIO, SCHEDULE_TIME
from market_data import fetch_market_snapshot, MarketSnapshot
from signal_gate import calc_signal_gate, GATE_RULES
from backtest import backtest, print_backtest_report


# ────────────────────────── 推送 ──────────────────────────
def push_to_feishu(msg: str) -> bool:
    if "REPLACE_WITH_YOUR_WEBHOOK" in WEBHOOK or not WEBHOOK.strip():
        print("WARNING: Webhook not configured, skipping push")
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


# ────────────────────────── 信号生成（统一数据层） ──────────────────────────
def gen_signal(name: str, code: str) -> dict:
    """
    数据层 -> 统一行情快照 -> 门禁系统 -> 信号输出
    所有价格来自同一个 MarketSnapshot，不再混用
    """
    try:
        snap = fetch_market_snapshot(code, name)
    except Exception as e:
        return {"name": name, "code": code, "error": "fetch failed: " + str(e)}

    if not snap.fetch_success:
        return {"name": name, "code": code, "error": snap.error_msg}

    sig = calc_signal_gate(snap)
    return {"name": name, "code": code, **sig, "snap": snap}


def format_signal(s: dict) -> str:
    """格式化为飞书推送文本"""
    if "error" in s:
        return "\n【" + s["name"] + " " + s["code"] + "】ERROR: " + s["error"]

    snap = s.get("snap")

    # 基本状态
    date_flag = "OK(Today)" if not s.get("is_expired") else "WARNING(Stale)"
    realtime_icon = "RT" if s["is_realtime"] else "NRT"
    age = s.get("data_age", "")

    # 门禁
    gate_passed = s["gate_passed"]
    if gate_passed:
        gate_str = "GATE PASS"
    else:
        gate_str = "GATE REJECT: " + str(s.get("reject_reason", ""))

    # 仓位
    pos_pct = int(s["position_ratio"] * 100)
    if pos_pct >= 40:
        pos_icon = "HIGH"
    elif pos_pct >= 20:
        pos_icon = "MED"
    else:
        pos_icon = "LOW"

    # 区间
    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]

    # 价格说明
    if snap and snap.is_realtime:
        price_note = ("RT:" + str(s["current_price"]) + " (" +
                      s["price_source"] + ") Prev:" + str(s["close"]))
    else:
        price_note = "Prev:" + str(s["close"])

    # 门禁明细
    gate_lines = []
    for gate_id, g in s.get("gates", {}).items():
        icon = "[OK]" if g["pass"] else "[REJ]"
        gate_lines.append("  " + icon + " " + g["value"])

    # 时间锁
    expired_warn = ""
    if s.get("is_expired"):
        expired_warn = "\n  WARNING: Data expired (" + age + ")"

    # 组装
    amp = s.get("amplitude", 0)
    pct = s.get("pct_change", 0)
    score = s["score"]
    max_score = s["max_score"]
    used_capital = int(CAPITAL * s["position_ratio"])

    parts = []
    parts.append("\n【" + s["name"] + "】" + str(s.get("data_date", "")) + " " + date_flag)
    parts.append("  Price: " + price_note + " " + realtime_icon + expired_warn)
    parts.append("  Score: " + str(score) + "/" + str(max_score) + " -> " + gate_str)
    parts.append("  Position: " + pos_icon + " " + str(pos_pct) + "% (use " + str(used_capital) + " CNY)")
    parts.append("  BUY zone: " + str(buy1) + " ~ " + str(buy2))
    parts.append("  SELL zone: " + str(sell1) + " ~ " + str(sell2))
    parts.append("  Amp: " + ("%.2f%%" % (amp * 100)) + " | Chg: " + ("%+.2f%%" % pct))
    parts.append("  --- Gate Details ---")
    parts.extend(gate_lines)
    parts.append("  Fetched: " + age)

    return "\n".join(parts)


# ────────────────────────── 每日例行 ──────────────────────────
def run_daily_signal():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("=== T0 Signal v3.1 === " + now + " ===")
    lines.append("[UNIFIED] All prices from single source, no mixing")

    for name, code in STOCKS.items():
        print("\nProcessing: " + name + " (" + code + ")...")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    footer = [
        "----------------------------",
        "RULE: Score>=3 + Amp>=4% -> Both required to trade",
        "Position: Score4=50%, Score3=30% (reduced)",
        "TTL: Signal expires after 5 minutes",
        "SOURCE: All prices unified (RT + PrevClose from same batch)",
        "NEXT: Tomorrow " + SCHEDULE_TIME + " CST",
    ]
    lines.extend(footer)

    msg = "\n".join(lines)
    push(msg)
    return True


# ────────────────────────── 回测入口 ──────────────────────────
def run_backtest():
    sep = "=" * 50
    print("\n" + sep)
    print("  BACKTEST MODE (v3.1 unified)")
    print(sep)
    for name, code in STOCKS.items():
        print("\n> Backtest: " + name + " (" + code + ")")
        try:
            from market_data import get_daily_df
            df = get_daily_df(code, days=120)
            snap = fetch_market_snapshot(code, name)
            result = backtest(df, name, snap=snap, use_gate=True)
            print_backtest_report(result)
        except Exception as e:
            import traceback
            print("  Backtest error: " + str(e))
            traceback.print_exc()


# ────────────────────────── 手动运行 ──────────────────────────
def run_once():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n> Manual run @ " + ts)
    run_daily_signal()


# ────────────────────────── 调度器 ──────────────────────────
def start_scheduler():
    schedule.every().day.at(SCHEDULE_TIME).do(run_daily_signal)
    banner = [
        "========================================",
        "  T0 Signal v3.1 - Started",
        "  Schedule: Daily " + SCHEDULE_TIME + " CST",
        "  Stocks: " + ", ".join(STOCKS.keys()),
        "  Signal TTL: 5 minutes",
        "========================================",
    ]
    print("\n".join(banner))
    while True:
        schedule.run_pending()
        time.sleep(30)


# ────────────────────────── 入口 ──────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="T0 Signal v3.1")
    parser.add_argument("--once", action="store_true", help="Run once and push")
    parser.add_argument("--backtest", action="store_true", help="Backtest mode")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.once:
        run_once()
    else:
        start_scheduler()
