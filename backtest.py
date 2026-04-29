import pandas as pd
from strategy import calc_signal
from config import T_CONFIG, BACKTEST_INITIAL_CASH, BACKTEST_START_DAYS_AGO


def backtest(df: pd.DataFrame, stock_name: str = "标的") -> dict:
    """
    日内区间触发回测（近似做T）

    成交规则：
    - 当日最低价 ≤ buy[0] → 视为低吸成交
    - 当日最高价 ≥ sell[0] → 视为高抛成交
    - 做T仓位：每次动用总资金的50%
    """
    amp_th = T_CONFIG["amplitude_threshold"]
    lookback = BACKTEST_START_DAYS_AGO

    # 需要足够历史数据计算均线
    if len(df) < lookback:
        return {"error": f"数据不足，仅有{len(df)}条"}

    df_test = df.tail(lookback).reset_index(drop=True)

    cash = BACKTEST_INITIAL_CASH
    position = 0
    cost_basis = 0  # 持仓成本（用于计算浮动盈亏）
    trades = []
    equity_curve = []

    for i in range(5, len(df_test)):
        sub = df_test.iloc[:i+1]
        latest = df_test.iloc[i]
        day = latest["date"].strftime("%Y-%m-%d")

        sig = calc_signal(sub, amp_th)
        if not sig["do_trade"]:
            equity_curve.append({"date": day, "equity": cash + position * latest["close"]})
            continue

        buy_price = sig["buy"][0]
        sell_price = sig["sell"][0]
        t_capital = cash * 0.5  # 每次动用一半仓位做T

        bought = False
        sold = False

        # --- 低吸触发 ---
        if latest["low"] <= buy_price and cash >= buy_price * 100:
            qty = int(t_capital / buy_price / 100) * 100  # 按手取整
            if qty > 0:
                cost_basis = buy_price
                cash -= qty * buy_price
                position += qty
                trades.append({
                    "date": day,
                    "action": "BUY",
                    "price": buy_price,
                    "qty": qty,
                    "reason": f"日内低点触发 ≤ {buy_price}"
                })
                bought = True

        # --- 高抛触发 ---
        if latest["high"] >= sell_price and position > 0:
            revenue = position * sell_price
            profit = (sell_price - cost_basis) * position
            cash += revenue
            trades.append({
                "date": day,
                "action": "SELL",
                "price": sell_price,
                "qty": position,
                "profit": round(profit, 2),
                "reason": f"日内高点触发 ≥ {sell_price}"
            })
            position = 0
            sold = True

        equity = cash + position * latest["close"]
        equity_curve.append({
            "date": day,
            "equity": round(equity, 2),
            "action": "BUY" if bought else ("SELL" if sold else None)
        })

    # --- 汇总 ---
    df_eq = pd.DataFrame(equity_curve)
    total_return = (df_eq["equity"].iloc[-1] - BACKTEST_INITIAL_CASH) / BACKTEST_INITIAL_CASH * 100

    # 最大回撤
    df_eq["peak"] = df_eq["equity"].cummax()
    df_eq["drawdown"] = (df_eq["equity"] - df_eq["peak"]) / df_eq["peak"] * 100
    max_drawdown = df_eq["drawdown"].min()

    win_trades = [t for t in trades if t["action"] == "SELL" and t.get("profit", 0) > 0]
    loss_trades = [t for t in trades if t["action"] == "SELL" and t.get("profit", 0) <= 0]

    summary = {
        "stock": stock_name,
        "start_date": df_eq["date"].iloc[0],
        "end_date": df_eq["date"].iloc[-1],
        "trading_days": len(df_eq),
        "total_return_pct": round(total_return, 2),
        "final_equity": round(df_eq["equity"].iloc[-1], 2),
        "initial_cash": BACKTEST_INITIAL_CASH,
        "max_drawdown_pct": round(max_drawdown, 2),
        "total_trades": len([t for t in trades if t["action"] == "BUY"]),
        "sell_trades": len([t for t in trades if t["action"] == "SELL"]),
        "win_trades": len(win_trades),
        "loss_trades": len(loss_trades),
        "win_rate": round(len(win_trades) / max(len([t for t in trades if t["action"] == "SELL"]), 1) * 100, 1),
        "trades": trades[-20:],  # 最近20条
        "equity_curve": df_eq
    }
    return summary


def print_backtest_report(result: dict):
    """打印回测报告"""
    if "error" in result:
        print(f"  ⚠️ {result['error']}")
        return

    print(f"\n{'='*50}")
    print(f"  📊 回测报告：{result['stock']}")
    print(f"{'='*50}")
    print(f"  区间：{result['start_date']} → {result['end_date']}")
    print(f"  交易日数：{result['trading_days']} 天")
    print(f"  初始资金：{result['initial_cash']:,.0f} 元")
    print(f"  最终资产：{result['final_equity']:,.2f} 元")
    print(f"  总收益率：{result['total_return_pct']:+.2f}%")
    print(f"  最大回撤：{result['max_drawdown_pct']:.2f}%")
    print(f"  买入次数：{result['total_trades']} | 卖出次数：{result['sell_trades']}")
    print(f"  胜率：{result['win_rate']:.1f}%（{result['win_trades']}胜/{result['loss_trades']}负）")
    print(f"{'='*50}")

    if result["trades"]:
        print(f"\n  最近交易：")
        for t in result["trades"][-5:]:
            profit_str = f"  盈利 {t.get('profit', 0):+.2f} 元" if "profit" in t else ""
            print(f"    {t['date']} {t['action']:4s} {t['price']:.2f} × {t['qty']} {t.get('reason','')} {profit_str}")
