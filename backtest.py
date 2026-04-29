"""
回测引擎 v3.1（基于统一行情层）
"""
import pandas as pd
from market_data import MarketSnapshot, get_daily_df, fetch_market_snapshot
from signal_gate import calc_signal_gate, GATE_RULES


def backtest(df: pd.DataFrame, stock_name: str = "",
             snap: MarketSnapshot = None, use_gate: bool = True,
             use_rr: bool = True) -> dict:
    """
    回测引擎 v3.3（RR盈亏比过滤已内置于calc_signal_gate）
    """
    lookback = 55  # 交易日窗口
    amp_th = GATE_RULES["amplitude_min"]

    if len(df) < lookback:
        return {"error": f"数据不足（{len(df)}条 < {lookback}条）"}

    df_test = df.tail(lookback).reset_index(drop=True)

    cash = 100000.0
    position = 0
    cost_basis = 0.0
    trades = []
    equity_curve = []

    for i in range(5, len(df_test)):
        latest = df_test.iloc[i]
        day = latest["date"].strftime("%Y-%m-%d") if hasattr(latest["date"], "strftime") else str(latest["date"])

        if use_gate:
            # 构建模拟snap（使用历史数据，保证口径统一）
            from dataclasses import dataclass
            @dataclass
            class MockSnap:
                code: str
                name: str
                fetched_at: float
                prev_close: float
                open: float
                high: float
                low: float
                close: float
                volume: float
                amplitude: float
                pct_change: float
                current_price: float
                price_source: str
                is_realtime: bool
                data_date: str
                data_age_minutes: float
                fetch_success: bool
                error_msg: str

                @property
                def is_expired(self): return False
                @property
                def age_display(self): return "历史数据"
                @property
                def reference_price(self): return self.current_price
                @property
                def price_for_gate(self): return self.close
                @property
                def price_for_signal(self): return self.close

                def to_dict(self): return {}

            m = MockSnap(
                code="",
                name=stock_name,
                fetched_at=0,
                prev_close=float(latest.get("close", latest["close"])),
                open=float(latest["open"]),
                high=float(latest["high"]),
                low=float(latest["low"]),
                close=float(latest["close"]),
                volume=float(latest["volume"]),
                amplitude=float(latest["amplitude"]) if "amplitude" in latest else 0.0,
                pct_change=float(latest.get("pct_change", 0)),
                current_price=float(latest["close"]),
                price_source="历史日线",
                is_realtime=False,
                data_date=day,
                data_age_minutes=0.0,
                fetch_success=True,
                error_msg=""
            )
            sig = calc_signal_gate(m, df_test.iloc[:i+1])
        else:
            from strategy import calc_signal as old_calc
            res = old_calc(df_test.iloc[:i+1], amp_th)
            sig = {**res, "gate_passed": res["do_trade"]}

        if not sig.get("gate_passed", False):
            equity = cash + position * latest["close"]
            equity_curve.append({"date": day, "equity": round(equity, 2), "action": None})
            continue

        buy_price = sig["buy"][0]
        sell_price = sig["sell"][0]
        pos_ratio = sig["position_ratio"]
        t_capital = cash * pos_ratio

        bought = False
        sold = False

        # 低吸
        if latest["low"] <= buy_price and cash >= buy_price * 100:
            qty = int(t_capital / buy_price / 100) * 100
            if qty > 0:
                cost_basis = buy_price
                cash -= qty * buy_price
                position += qty
                trades.append({
                    "date": day, "action": "BUY", "price": buy_price,
                    "qty": qty, "reason": f"低点触发 ≤{buy_price}"
                })
                bought = True

        # 高抛
        if latest["high"] >= sell_price and position > 0:
            profit = (sell_price - cost_basis) * position
            cash += position * sell_price
            trades.append({
                "date": day, "action": "SELL", "price": sell_price,
                "qty": position, "profit": round(profit, 2),
                "reason": f"高点触发 ≥{sell_price}"
            })
            position = 0
            sold = True

        equity = cash + position * latest["close"]
        equity_curve.append({
            "date": day,
            "equity": round(equity, 2),
            "action": "BUY" if bought else ("SELL" if sold else None)
        })

    # 汇总
    if not equity_curve:
        return {"error": "无有效交易日"}

    df_eq = pd.DataFrame(equity_curve)
    total_return = (df_eq["equity"].iloc[-1] - 100000.0) / 100000.0 * 100
    df_eq["peak"] = df_eq["equity"].cummax()
    df_eq["drawdown"] = (df_eq["equity"] - df_eq["peak"]) / df_eq["peak"] * 100
    max_drawdown = df_eq["drawdown"].min()

    sell_trades = [t for t in trades if t["action"] == "SELL"]
    win_trades = [t for t in sell_trades if t.get("profit", 0) > 0]
    loss_trades = [t for t in sell_trades if t.get("profit", 0) <= 0]

    return {
        "stock": stock_name,
        "start_date": df_eq["date"].iloc[0],
        "end_date": df_eq["date"].iloc[-1],
        "trading_days": len(df_eq),
        "total_return_pct": round(total_return, 2),
        "final_equity": round(df_eq["equity"].iloc[-1], 2),
        "initial_cash": 100000.0,
        "max_drawdown_pct": round(max_drawdown, 2),
        "total_trades": len([t for t in trades if t["action"] == "BUY"]),
        "sell_trades": len(sell_trades),
        "win_trades": len(win_trades),
        "loss_trades": len(loss_trades),
        "win_rate": round(len(win_trades) / max(len(sell_trades), 1) * 100, 1),
        "trades": trades[-20:],
        "equity_curve": df_eq
    }


def print_backtest_report(result: dict):
    if "error" in result:
        print(f"  ⚠️ {result['error']}")
        return
    print(f"\n{'='*50}")
    print(f"  📊 回测报告（v3.1统一行情层）：{result['stock']}")
    print(f"{'='*50}")
    print(f"  区间：{result['start_date']} → {result['end_date']}")
    print(f"  交易日数：{result['trading_days']} 天")
    print(f"  初始资金：{result['initial_cash']:,.0f} 元")
    print(f"  最终资产：{result['final_equity']:,.2f} 元")
    print(f"  总收益率：{result['total_return_pct']:+.2f}%")
    print(f"  最大回撤：{result['max_drawdown_pct']:.2f}%")
    print(f"  买入次数：{result['total_trades']} | 卖出：{result['sell_trades']}")
    print(f"  胜率：{result['win_rate']:.1f}%（{result['win_trades']}胜/{result['loss_trades']}负）")
    print(f"{'='*50}")
    if result["trades"]:
        print(f"\n  最近交易：")
        for t in result["trades"][-5:]:
            profit_str = f"  盈利 {t.get('profit', 0):+.2f}" if "profit" in t else ""
            print(f"    {t['date']} {t['action']:4s} {t['price']:.2f} × {t['qty']} {profit_str}")