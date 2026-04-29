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
    v3.3 人话格式：结论 + 原因（简洁列表式）
    核心逻辑：趋势权重 > RR权重，RR失真直接降权
    """
    if "error" in s:
        return "\n【" + s["name"] + " " + s["code"] + "】ERROR: " + s["error"]

    # ── 基本信息 ──
    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]
    rr = s.get("rr", 0)
    sl_price = s.get("sl_price", 0)
    rr_verdict = s.get("rr_verdict", "SKIP")   # DO / WARN / SKIP
    rr_flag = s.get("rr_flag", "NORMAL")       # NORMAL / FAKE_RR / SUSPICIOUS
    rr_reason = s.get("rr_reason", "")
    pos_pct = int(s["position_ratio"] * 100)
    stop_loss_warning = ""  # 提前初始化，防止UnboundLocalError

    # ── 判定结论 ──
    if rr_verdict == "SKIP" or pos_pct == 0:
        verdict_icon = "SKIP"
        pos_label = "0%"
    elif rr_verdict == "WARN":
        verdict_icon = "WARN"
        pos_label = str(pos_pct) + "%"
    elif rr_verdict == "DO":
        if rr >= GATE_RULES["rr_bonus_threshold"] and s.get("above_ma5"):
            verdict_icon = "GO"
            pos_label = str(pos_pct) + "%"
        else:
            verdict_icon = "GO"
            pos_label = str(pos_pct) + "%"
    else:
        verdict_icon = "SKIP"
        pos_label = "0%"

    # ── 结论文字 ──
    if verdict_icon == "SKIP":
        verdict_text = "X 不做"
    elif verdict_icon == "WARN":
        verdict_text = "WARN 轻仓做T（" + str(pos_pct) + "%）"
        if stop_loss_warning:
            verdict_text += "，" + stop_loss_warning
    else:
        if pos_pct <= 20:
            verdict_text = "GO 小仓做T（" + str(pos_pct) + "%）"
        elif pos_pct >= 45:
            verdict_text = "GO 做T（" + str(pos_pct) + "%）"
        else:
            verdict_text = "GO 做T（" + str(pos_pct) + "%）"

    # ── 收集原因 ──
    reasons = []
    stop_loss_warning = ""

    if s["gate_passed"]:
        gates = s.get("gates", {})
        g2 = gates.get("G2", {})

        # 振幅
        if g2["pass"]:
            amp_pct = s["amplitude"] * 100
            reasons.append("振幅达标（%.2f%% >= 4%%）" % amp_pct)
        else:
            amp_pct = s["amplitude"] * 100
            reasons.append("波动不足（%.2f%% < 4%%）" % amp_pct)

        # RR质量
        rr_conf = s.get("rr_confidence", "UNKNOWN")
        if rr_flag == "FAKE_RR":
            reasons.append("盈亏比失真（止损过近，RR=%.1f无效）" % rr)
            stop_loss_warning = "止损过紧（%.1f%%），建议拓宽" % s.get("stop_loss_pct", 0)
        elif rr_flag == "SUSPICIOUS":
            reasons.append("盈亏比可疑（RR=%.1f>5，疑似失真）" % rr)
        elif rr_conf == "LOW":
            reasons.append("盈亏比可信度低（止损仅%.1f%%，小于0.8%%）" % s.get("stop_loss_pct", 0))
            stop_loss_warning = "止损过紧，建议拓宽至约1.2%%以上"
        elif rr_conf == "MEDIUM" and not s.get("above_ma5"):
            reasons.append("盈亏比可信度中等（逆趋势，胜率压低）")
        elif rr >= GATE_RULES["rr_bonus_threshold"]:
            reasons.append("盈亏比优秀（RR=%.1f > 2.0）" % rr)
        elif rr >= GATE_RULES["rr_min"]:
            reasons.append("盈亏比合格（RR=%.1f）" % rr)
        else:
            reasons.append("盈亏比不足（RR=%.1f < 1.5）" % rr)

        # 趋势
        if s.get("above_ma5"):
            reasons.append("处于MA5上方（顺势）")
        else:
            reasons.append("处于MA5下方（逆趋势 -> 降仓）")

        # 止损过紧警告
        sl_pct = s.get("stop_loss_pct", 0)
        if sl_pct > 0 and sl_pct < 1.0 and stop_loss_warning == "":
            stop_loss_warning = "止损偏紧（%.1f%%），建议拓宽至1.2%%以上" % sl_pct

    else:
        gates = s.get("gates", {})
        if gates.get("G2", {}).get("pass") is False:
            amp_pct = s["amplitude"] * 100
            reasons.append("波动不足（%.2f%% < 4%%）" % amp_pct)
        if "RR" in s.get("reject_reason", "") or "rr" in s.get("reject_reason", "").lower():
            reasons.append("盈亏比不成立")
        if "评分" in s.get("reject_reason", ""):
            reasons.append("评分不足")
        if not reasons:
            reasons.append(s.get("reject_reason", "门禁拒绝"))

    # ── 操作区间（第二次定义，覆盖上面的空壳）──
    if s["gate_passed"] and rr_verdict != "SKIP":
        op_lines = []
        op_lines.append("  买：" + str(buy1) + " 附近")
        op_lines.append("  卖：" + str(sell1) + " 附近")
        op_lines.append("  止损：" + str(sl_price) + " 附近（" +
                        ("%.1f%%" % s.get("stop_loss_pct", 0)) + "）")
        if stop_loss_warning:
            op_lines.append("  ！建议：" + stop_loss_warning)
        op_str = "\n".join(op_lines)
    else:
        op_str = "  观望，不操作"

    # ── 组装 ──
    parts = []
    parts.append("【" + s["name"] + "】")
    parts.append("结论：" + verdict_text)
    parts.append("原因：")
    for r in reasons:
        parts.append("- " + r)
    parts.append("操作：")
    parts.append(op_str)
    parts.append("")

    return "\n".join(parts)


# ────────────────────────── 每日例行 ──────────────────────────
def run_daily_signal(intraday: bool = False):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "[INTRA]" if intraday else "[CLOSE]"
    lines = []
    lines.append("=== T0 Signal v3.3 === " + now + " ===")

    for name, code in STOCKS.items():
        print("\nProcessing: " + name + " (" + code + ")...")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    # 今日剩余交易次数
    if STOCKS:
        sample_code = list(STOCKS.values())[0]
        freq = get_freq_status(sample_code)
        remaining = freq.get("remaining", 0)
    else:
        remaining = GATE_RULES["max_trades_per_day"]

    lines.append("今日剩余交易次数：" + str(remaining))
    lines.append("规则：")
    lines.append("- RR < 1.5 -> 禁止交易")
    lines.append("- RR 1.5~2 -> 小仓")
    lines.append("- RR > 2 -> 正常")

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