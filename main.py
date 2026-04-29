#!/usr/bin/env python3
"""
做T信号系统 v3.0 工业级 · 主程序
=========================
调度：每日 15:10（北京时间）自动推送信号
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
from signal_gate import get_daily_df, get_realtime_price, calc_signal_gate, GATE_RULES
from backtest import backtest, print_backtest_report


# ────────────────────────── 推送 ──────────────────────────
def push_to_feishu(msg: str) -> bool:
    if "REPLACE_WITH_YOUR_WEBHOOK" in WEBHOOK or not WEBHOOK.strip():
        print("⚠️  未配置 Webhook，跳过推送")
        return False
    try:
        data = {"msg_type": "text", "content": {"text": msg}}
        resp = requests.post(WEBHOOK, json=data, timeout=10)
        if resp.status_code == 200:
            print("  ✅ 飞书推送成功")
            return True
        else:
            print(f"  ❌ 推送失败：{resp.status_code}")
            return False
    except Exception as e:
        print(f"  ❌ 推送异常：{e}")
        return False


def push(msg: str):
    print("\n" + "=" * 55)
    print(msg)
    print("=" * 55)
    push_to_feishu(msg)


# ────────────────────────── 信号计算 ──────────────────────────
def gen_signal(name: str, code: str) -> dict:
    """数据层 → 门禁系统 → 信号输出"""
    try:
        df, date_status = get_daily_df(code, days=60)
    except Exception as e:
        return {"name": name, "code": code, "error": str(e)}

    if len(df) < 10:
        return {"name": name, "code": code, "error": f"数据不足（{len(df)}条）"}

    # 实时价
    current_price, price_source, is_realtime = get_realtime_price(code)
    current_price_str = f"{current_price:.2f}（{price_source}）"

    # 数据时间
    latest = df.iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d") if hasattr(latest["date"], "strftime") else str(latest["date"])

    # Signal Gate
    sig = calc_signal_gate(df, amp_th=GATE_RULES["amplitude_min"])

    return {
        "name": name,
        "code": code,
        "date": latest_date,
        "date_status": date_status,
        "close": float(latest["close"]),
        "current_price": current_price_str,
        "is_realtime": is_realtime,
        "price_source": price_source,
        **sig
    }


def format_signal(s: dict) -> str:
    """格式化为飞书推送文本"""
    if "error" in s:
        return f"\n【{s['name']} {s['code']}】⚠️ 数据失败：{s['error']}"

    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]
    gate_passed = s["gate_passed"]
    do_str = "✅ 门禁通过" if gate_passed else f"❌ 门禁拒绝：{s.get('reject_reason','')}"
    pos_pct = int(s['position_ratio'] * 100)

    # Gate状态
    gate_lines = []
    for gate_id, gate in s.get("gates", {}).items():
        status = "✅" if gate["pass"] else "❌"
        gate_lines.append(f"    {status} {gate['value']}")

    realtime_str = "✅实时" if s["is_realtime"] else "⚠️非实时"
    date_flag = "✅今日" if s["date_status"] == "latest" else "⚠️昨日数据"

    # 仓位颜色
    if pos_pct >= 40:
        pos_icon = "🔴"
    elif pos_pct >= 20:
        pos_icon = "🟡"
    else:
        pos_icon = "⚪"

    return (
        f"\n【{s['name']}】{s['date']} {date_flag}"
        f"\n  📌 当前价：{s['current_price']}（昨收：{s['close']:.2f}）{realtime_str}"
        f"\n  🔢 信号评分：{s['score']}/{s['max_score']} → {do_str}"
        f"\n  📊 建议仓位：{pos_icon} {pos_pct}%（动用 {int(CAPITAL * s['position_ratio']):,} 元）"
        f"\n  🟢 低吸区间：{buy1} ~ {buy2}"
        f"\n  🔴 高抛区间：{sell1} ~ {sell2}"
        f"\n  📈 振幅：{s.get('amplitude',0)*100:.2f}% | 涨跌：{s.get('change_pct',0):+.2f}%"
        f"\n  ── 门禁明细 ──"
        f"\n  " + "\n  ".join(gate_lines)
    )


# ────────────────────────── 每日例行任务 ──────────────────────────
def run_daily_signal():
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"📊 **做T信号 v3.0 工业级** `{today}`\n"
    lines = [header]

    for name, code in STOCKS.items():
        print(f"\n处理：{name}（{code}）…")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    footer = (
        f"\n─────────────────────────────"
        f"\n💡 规则：评分≥3/4 + 振幅≥4% → 同时满足才做T"
        f"\n   仓位：4分→50%，3分→30%（降仓）"
        f"\n⏰ 下次推送：明天 15:10（北京时间）"
    )
    lines.append(footer)

    msg = "\n".join(lines)
    push(msg)
    return True


# ────────────────────────── 回测入口 ──────────────────────────
def run_backtest():
    print("\n" + "🔁 回测模式".center(50, "─"))
    for name, code in STOCKS.items():
        print(f"\n▶ 回测：{name}（{code}）")
        try:
            df, _ = get_daily_df(code, days=120)
            result = backtest(df, name, use_gate=True)
            print_backtest_report(result)
        except Exception as e:
            print(f"  ❌ 回测失败：{e}")


# ────────────────────────── 手动单次运行 ──────────────────────────
def run_once():
    print(f"\n▶ 手动运行 @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run_daily_signal()


# ────────────────────────── 调度器 ──────────────────────────
def start_scheduler():
    schedule.every().day.at(SCHEDULE_TIME).do(run_daily_signal)
    print(f"""
╔════════════════════════════════════════╗
║  做T信号系统 v3.0 工业级 · 已启动      ║
║  推送时间：每日 {SCHEDULE_TIME}（北京时间）     ║
║  标的：{', '.join(STOCKS.keys())}           ║
╚════════════════════════════════════════╝
    """)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ────────────────────────── 入口 ──────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="做T信号系统 v3.0")
    parser.add_argument("--once", action="store_true", help="手动运行一次并推送")
    parser.add_argument("--backtest", action="store_true", help="回测模式")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.once:
        run_once()
    else:
        start_scheduler()
