import pandas as pd


def calc_signal(df: pd.DataFrame, amp_th: float = 0.04):
    """
    做T信号计算
    返回: score, do_trade, buy区间, sell区间, 仓位比例
    """
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["volume_ma5"] = df["volume"].rolling(5).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    reasons = []

    # --- 因子1：趋势（价格在MA5之上）---
    if latest["close"] > latest["ma5"]:
        score += 1
        reasons.append("↗ 价格在MA5上方")
    else:
        reasons.append("↘ 价格在MA5下方")

    # --- 因子2：波动（振幅达标）---
    amplitude = (latest["high"] - latest["low"]) / latest["close"]
    if amplitude >= amp_th:
        score += 1
        reasons.append(f"⚡ 振幅 {amplitude:.2%} ≥ {amp_th:.2%}")
    else:
        reasons.append(f"➖ 振幅 {amplitude:.2%} < {amp_th:.2%}")

    # --- 因子3：情绪（涨幅不过热）---
    change = (latest["pct_change"] / 100) if "pct_change" in latest else \
             (latest["close"] - prev["close"]) / prev["close"]
    if abs(change) < 0.05:
        score += 1
        reasons.append(f"{'📈' if change > 0 else '📉'} 日涨幅 {change:.2%} < 5%")
    else:
        reasons.append(f"🔥 日涨幅 {change:.2%} 过热/过冷")

    # --- 因子4：量能（成交量稳定）---
    if latest["volume"] > df["volume_ma5"].iloc[-1] * 0.6:
        score += 1
        reasons.append("📊 量能支撑")
    else:
        reasons.append("⚠️ 量能不足")

    do_trade = score >= 3

    # === 做T价格区间（Pivot区间）===
    H, L, C = latest["high"], latest["low"], latest["close"]
    M = (H + L + C) / 3          # 枢纽点
    R = H - L                    # 日内波幅

    buy1 = round(M - 0.4 * R, 2)   # 低吸一档
    buy2 = round(M - 0.6 * R, 2)   # 低吸二档（更低）
    sell1 = round(M + 0.4 * R, 2)  # 高抛一档
    sell2 = round(M + 0.6 * R, 2)  # 高抛二档（更高）

    # === 仓位建议 ===
    if score >= 4:
        pos = 1.0
    elif score == 3:
        pos = 0.5
    else:
        pos = 0.0

    return {
        "score": score,
        "max_score": 4,
        "do_trade": do_trade,
        "buy": (buy1, buy2),
        "sell": (sell1, sell2),
        "position_ratio": pos,
        "amplitude": amplitude,
        "change_pct": change * 100,
        "reasons": reasons
    }
