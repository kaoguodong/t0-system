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
        print("⚠️  未配置 Webhook，跳过推送")
        return False
    try:
        data = {"msg_type": "text", "content": {"text": msg}}
        resp = requests.post(WEBHOOK, json=data, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"  ❌ 推送异常：{e}")
        return False


def push(msg: str):
    print("\n" + "=" * 55)
    print(msg)
    print("=" * 55)
    push_to_feishu(msg)


# ────────────────────────── 信号生成（统一数据层） ──────────────────────────
def gen_signal(name: str, code: str) -> dict:
    """
    数据层 → 统一行情快照 → 门禁系统 → 信号输出
    ==============================================
    整个流程中：所有价格来自同一个 MarketSnapshot
    不再混用：实时价 vs 昨收价
    """
    try:
        snap = fetch_market_snapshot(code, name)
    except Exception as e:
        return {"name": name, "code": code, "error": f"获取行情失败: {e}"}

    if not snap.fetch_success:
        return {"name": name, "code": code, "error": snap.error_msg}

    # Signal Gate（使用MarketSnapshot，口径统一）
    sig = calc_signal_gate(snap)

    # 合并输出
    return {
        "name": name,
        "code": code,
        **sig,
        "snap": snap,  # 保留原始快照引用
    }


def format_signal(s: dict) -> str:
    """格式化为飞书推送文本"""
    if "error" in s:
        return f"\n【{s['name']} {s['code']}】⚠️ {s['error']}"

    snap: MarketSnapshot = s.get("snap", None)

    # 基本信息行
    date_flag = f"✅今日数据" if not s.get("is_expired", False) else f"⚠️数据过期"
    realtime_icon = "✅实时" if s["is_realtime"] else "⚠️非实时"
    age = s.get("data_age", "")

    # 门禁状态
    gate_passed = s["gate_passed"]
    gate_str = f"✅ 门禁通过" if gate_passed else f"❌ 门禁拒绝：{s.get('reject_reason','')}"

    # 仓位
    pos_pct = int(s["position_ratio"] * 100)
    if pos_pct >= 40:
        pos_icon = "🔴"
    elif pos_pct >= 20:
        pos_icon = "🟡"
    else:
        pos_icon = "⚪"

    # 价格区间
    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]

    # 实时价 vs 参考价说明
    if snap and snap.is_realtime:
        price_note = f"实时:{s['current_price']:.2f}（{s['price_source']}）昨收:{s['close']:.2f}"
    else:
        price_note = f"昨收:{s['close']:.2f}"

    # 门禁明细
    gate_lines = []
    for gate_id, g in s.get("gates", {}).items():
        icon = "✅" if g["pass"] else "❌"
        gate_lines.append(f"  {icon} {g['value']}")

    # 时间锁警告
    expired_warn = ""
    if s.get("is_expired", False):
        expired_warn = f"\n  ⚠️ 数据过期（{age}），建议重新获取"

    return (
        f"\n【{s['name']}】{s.get('data_date','')} {date_flag}"
        f"\n  📌 价格：{price_note} {realtime_icon} {expired_warn}"
        f"\n  🔢 信号评分：{s['score']}/{s['max_score']} → {gate_str}"
        f"\n  📊 建议仓位：{pos_icon} {pos_pct}%（动用 {int(CAPITAL * s['position_ratio']):,} 元）"
        f"\n  🟢 低吸区间：{buy1} ~ {buy2}"
        f"\n  🔴 高抛区间：{sell1} ~ {sell2}"
        f"\n  📈 振幅：{s.get('amplitude',0)*100:.2f}% | 涨跌：{s.get('pct_change',0):+.2f}%"
        f"\n  ── 门禁明细 ──"
        f"\n  " + "\n  ".join(gate_lines)
        f"\n  ⏱ 数据获取：{age}"
    )


# ────────────────────────── 每日例行 ──────────────────────────
def run_daily_signal():
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"📊 **做T信号 v3.1 数据统一层** `{today}`\n"
    header += "🔗 所有价格同源同时间戳，无混用风险\n"
    lines = [header]

    for name, code in STOCKS.items():
        print(f"\n处理：{name}（{code}）…")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    footer = (
        f"\n─────────────────────────────"
        f"\n💡 v3.1新规：评分≥3 + 振幅≥4% → 同时满足才做T"
        f"\n   仓位：4分→50%，3分→30%（降仓）"
        f"\n   ⏱ 信号有效期5分钟，过期自动作废"
        f"\n   🔗 所有价格同源（实时价+昨收同批次获取）"
        f"\n⏰ 下次推送：明天 {SCHEDULE_TIME}（北京时间）"
    )
    lines.append(footer)

    msg = "\n".join(lines)
    push(msg)
    return True


# ────────────────────────── 回测入口 ──────────────────────────
def run_backtest():
    print("\n" + "🔁 回测模式（v3.1数据层）".center(50, "─"))
    for name, code in STOCKS.items():
        print(f"\n▶ 回测：{name}（{code}）")
        try:
            from market_data import get_daily_df
            df = get_daily_df(code, days=120)
            snap = fetch_market_snapshot(code, name)
            result = backtest(df, name, snap=snap, use_gate=True)
            print_backtest_report(result)
        except Exception as e:
            import traceback
            print(f"  ❌ 回测失败：{e}")
            traceback.print_exc()


# ────────────────────────── 手动运行 ──────────────────────────
def run_once():
    print(f"\n▶ 手动运行 @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run_daily_signal()


# ────────────────────────── 调度器 ──────────────────────────
def start_scheduler():
    schedule.every().day.at(SCHEDULE_TIME).do(run_daily_signal)
    print(f"""
╔════════════════════════════════════════╗
║  做T信号系统 v3.1 数据统一层 · 已启动   ║
║  推送时间：每日 {SCHEDULE_TIME}（北京时间）      ║
║  标的：{', '.join(STOCKS.keys())}             ║
║  信号有效期：5分钟（时间锁）           ║
╚════════════════════════════════════════╝
    """)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ────────────────────────── 入口 ──────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="做T信号系统 v3.1")
    parser.add_argument("--once", action="store_true", help="手动运行一次并推送")
    parser.add_argument("--backtest", action="store_true", help="回测模式")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.once:
        run_once()
    else:
        start_scheduler()
