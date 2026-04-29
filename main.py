#!/usr/bin/env python3
"""
做T信号系统 · 主程序
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

from config import STOCKS, WEBHOOK, CAPITAL, T_POSITION_RATIO, SCHEDULE_TIME, T_CONFIG
from data import get_daily_df, get_realtime_price
from strategy import calc_signal
from backtest import backtest, print_backtest_report


# ────────────────────────── 推送 ──────────────────────────
def push_to_feishu(msg: str) -> bool:
    """飞书机器人 Webhook 推送"""
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
            print(f"  ❌ 推送失败：{resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"  ❌ 推送异常：{e}")
        return False


def push(msg: str):
    """双保险：飞书 + 打印到终端"""
    print("\n" + "=" * 50)
    print(msg)
    print("=" * 50)
    push_to_feishu(msg)


# ────────────────────────── 信号计算 ──────────────────────────
def gen_signal(name: str, code: str) -> dict:
    """获取数据 + 计算信号 + 格式化输出"""
    days = T_CONFIG.get("lookback_days", 60)
    try:
        df = get_daily_df(code, days=days)
    except Exception as e:
        return {"name": name, "code": code, "error": str(e)}

    if len(df) < 10:
        return {"name": name, "code": code, "error": f"数据不足（{len(df)}条）"}

    sig = calc_signal(df, T_CONFIG["amplitude_threshold"])

    try:
        current_price = get_realtime_price(code)
        current_price_str = f"{current_price:.2f}"
    except Exception:
        current_price_str = "(无法获取实时价)"

    latest = df.iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d") if hasattr(latest["date"], "strftime") else str(latest["date"])

    return {
        "name": name,
        "code": code,
        "date": latest_date,
        "close": float(latest["close"]),
        "current_price": current_price_str,
        **sig
    }


def format_signal(s: dict) -> str:
    """将信号 dict 格式化为飞书推送文本"""
    if "error" in s:
        return f"\n【{s['name']} {s['code']}】⚠️ 数据获取失败：{s['error']}"

    buy1, buy2 = s["buy"]
    sell1, sell2 = s["sell"]
    do_str = "✅ 建议做T" if s["do_trade"] else "❌ 跳过"
    pos_str = f"{int(s['position_ratio']*100)}%"

    reasons = "\n      ".join(s.get("reasons", []))

    return (
        f"\n【{s['name']}】{s['date']}"
        f"\n  📌 当前价：{s['current_price']}（昨收：{s['close']:.2f}）"
        f"\n  🔢 信号评分：{s['score']}/{s['max_score']} → {do_str}"
        f"\n      因子：{reasons}"
        f"\n  📊 建议仓位：{pos_str}（动用 {int(CAPITAL * T_POSITION_RATIO):,} 元）"
        f"\n  🟢 低吸区间：{buy1} ~ {buy2}"
        f"\n  🔴 高抛区间：{sell1} ~ {sell2}"
        f"\n  📈 日振幅：{s.get('amplitude', 0)*100:.2f}%  |  日涨跌：{s.get('change_pct', 0):+.2f}%"
    )


# ────────────────────────── 每日例行任务 ──────────────────────────
def run_daily_signal():
    """每日盘后信号例行程序"""
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"📊 **做T信号推送** `{today}`\n"
    lines = [header]

    for name, code in STOCKS.items():
        print(f"\n处理：{name} ({code}) …")
        sig = gen_signal(name, code)
        lines.append(format_signal(sig))

    footer = (
        f"\n─────────────────────────────"
        f"\n💡 规则：评分≥3分才做T | 只按区间挂单 | 连续执行≥10天"
        f"\n⏰ 下次推送：明天 15:10（北京时间）"
    )
    lines.append(footer)

    msg = "\n".join(lines)
    push(msg)
    return True


# ────────────────────────── 回测入口 ──────────────────────────
def run_backtest():
    """对所有标的执行回测"""
    print("\n" + "🔁 回测模式".center(50, "─"))
    for name, code in STOCKS.items():
        print(f"\n▶ 回测：{name}（{code}）")
        try:
            df = get_daily_df(code, days=120)
            result = backtest(df, name)
            print_backtest_report(result)
        except Exception as e:
            print(f"  ❌ 回测失败：{e}")


# ────────────────────────── 手动单次运行 ──────────────────────────
def run_once():
    """手动触发一次信号推送（不调度）"""
    print(f"\n▶ 手动运行 @ {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    run_daily_signal()


# ────────────────────────── 调度器 ──────────────────────────
def start_scheduler():
    """启动定时调度（每日 SCHEDULE_TIME 执行）"""
    schedule.every().day.at(SCHEDULE_TIME).do(run_daily_signal)

    print(f"""
╔════════════════════════════════════════╗
║    做T信号系统 · 已启动                  ║
║    推送时间：每日 {SCHEDULE_TIME}（北京时间）      ║
║    标的：{', '.join(STOCKS.keys())}           ║
╚════════════════════════════════════════╝
    """)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ────────────────────────── 入口 ──────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="做T信号系统")
    parser.add_argument("--once", action="store_true", help="手动运行一次并推送（不调度）")
    parser.add_argument("--backtest", action="store_true", help="回测模式")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
    elif args.once:
        run_once()
    else:
        start_scheduler()