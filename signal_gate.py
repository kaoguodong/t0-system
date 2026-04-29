"""
v3.1 信号门禁系统（基于统一行情层）
====================================
所有信号计算基于 MarketSnapshot（唯一价格来源）
不再混用：实时价 vs 昨收价
"""

import pandas as pd
from typing import List
from dataclasses import dataclass
from market_data import MarketSnapshot, fetch_market_snapshot


# ═══════════════════════════════════════════════════════
# 门禁规则配置
# ═══════════════════════════════════════════════════════

GATE_RULES = {
    "score_min": 3,               # 评分门槛
    "amplitude_min": 0.04,         # 振幅门槛（硬过滤）
    "volume_ratio_min": 0.6,       # 量能门槛（相对MA5）
    "max_position_if_score3": 0.30,  # 3分 → 30%
    "max_position_if_score4": 0.50,   # 4分 → 50%
    "expired_data_allowed": False,    # 过期数据是否允许交易（默认不允许）
}


@dataclass
class GateResult:
    """门禁判定结果"""
    passed: bool
    score: int
    max_score: int
    reject_reason: str | None
    position_ratio: float
    buy_prices: tuple[float, float]
    sell_prices: tuple[float, float]
    gates: dict


def _calculate_gates(snap: MarketSnapshot, df: pd.DataFrame, amp_th: float) -> tuple[GateResult, pd.DataFrame]:
    """
    内部计算门禁
    使用 snap.price_for_gate 作为所有价格判断的统一基准
    """
    # ── 计算MA5指标 ──
    if len(df) < 5:
        ma5 = snap.close
    else:
        ma5 = df["close"].tail(5).mean()

    if len(df) < 20:
        volume_ma5 = df["volume"].tail(5).mean() if len(df) >= 2 else snap.volume
    else:
        volume_ma5 = df["volume"].tail(5).mean()

    # ── 因子打分（4分制） ──
    score = 0
    gates = {}

    # G1: 趋势（价格 vs MA5）
    price_vs_ma5 = snap.close > ma5
    score += 1 if price_vs_ma5 else 0
    gates["G1"] = {
        "name": "趋势",
        "pass": price_vs_ma5,
        "value": f"{'↑MA5上方' if price_vs_ma5 else '↓MA5下方'}（{snap.close:.2f} vs MA5:{ma5:.2f}）"
    }

    # G2: 振幅（硬条件）
    amplitude_ok = snap.amplitude >= amp_th
    score += 1 if amplitude_ok else 0
    gates["G2"] = {
        "name": "振幅(硬)",
        "pass": amplitude_ok,
        "value": f"振幅 {snap.amplitude:.2%} {'✓≥' if amplitude_ok else '✗<'}{amp_th:.2%}"
    }

    # G3: 情绪（涨跌幅）
    sentiment_ok = abs(snap.pct_change) < 5.0
    score += 1 if sentiment_ok else 0
    gates["G3"] = {
        "name": "情绪",
        "pass": sentiment_ok,
        "value": f"涨跌 {snap.pct_change:+.2f}%（{'正常' if sentiment_ok else '过热'})"
    }

    # G4: 量能
    if volume_ma5 > 0:
        vol_ratio = snap.volume / volume_ma5
    else:
        vol_ratio = 1.0
    volume_ok = vol_ratio >= GATE_RULES["volume_ratio_min"]
    score += 1 if volume_ok else 0
    gates["G4"] = {
        "name": "量能",
        "pass": volume_ok,
        "value": f"量比 {vol_ratio:.2f}（{'✓' if volume_ok else '✗'}{GATE_RULES['volume_ratio_min']}）"
    }

    # ── 做T价格区间（Pivot System） ──
    H, L, C = snap.high, snap.low, snap.close
    M = (H + L + C) / 3
    R = H - L
    buy1 = round(M - 0.4 * R, 2)
    buy2 = round(M - 0.6 * R, 2)
    sell1 = round(M + 0.4 * R, 2)
    sell2 = round(M + 0.6 * R, 2)

    # ── 仓位决策 ──
    amplitude_hard_pass = snap.amplitude >= GATE_RULES["amplitude_min"]

    if score >= 4 and amplitude_hard_pass:
        pos = GATE_RULES["max_position_if_score4"]  # 4分 → 50%
    elif score >= 3 and amplitude_hard_pass:
        pos = GATE_RULES["max_position_if_score3"]  # 3分 → 30%
    else:
        pos = 0.0

    # ── 门禁判定 ──
    if not amplitude_hard_pass:
        reject = f"振幅不足（{snap.amplitude:.2%}<{amp_th:.2%}）"
    elif score < GATE_RULES["score_min"]:
        reject = f"评分不足（{score}<{GATE_RULES['score_min']}）"
    elif snap.is_expired and not GATE_RULES["expired_data_allowed"]:
        reject = f"数据过期（{snap.age_display}，超过{GATE_RULES['SIGNAL_VALIDITY_MINUTES']}分钟）"
    else:
        reject = None

    passed = reject is None

    return GateResult(
        passed=passed,
        score=score,
        max_score=4,
        reject_reason=reject,
        position_ratio=pos,
        buy_prices=(buy1, buy2),
        sell_prices=(sell1, sell2),
        gates=gates
    ), df


def calc_signal_gate(snap: MarketSnapshot, df: pd.DataFrame | None = None) -> dict:
    """
    v3.1 信号门禁计算（基于统一行情快照）
    ======================================
    输入：MarketSnapshot（唯一价格来源）
    输出：完整信号字典（兼容旧接口）
    """
    if df is None:
        try:
            df = get_daily_df(snap.code, days=60)
        except Exception:
            df = pd.DataFrame()

    gate_result, df = _calculate_gates(snap, df, GATE_RULES["amplitude_min"])

    return {
        # 门禁判定
        "gate_passed": gate_result.passed,
        "score": gate_result.score,
        "max_score": gate_result.max_score,
        "reject_reason": gate_result.reject_reason,

        # 仓位
        "position_ratio": gate_result.position_ratio,

        # 做T价格（基于昨收计算，口径统一）
        "buy": gate_result.buy_prices,
        "sell": gate_result.sell_prices,

        # 原始行情（来自MarketSnapshot）
        "close": snap.close,
        "prev_close": snap.prev_close,
        "current_price": snap.current_price,
        "price_source": snap.price_source,
        "is_realtime": snap.is_realtime,
        "reference_price": snap.reference_price,

        # 数据新鲜度
        "data_date": snap.data_date,
        "data_age": snap.age_display,
        "is_expired": snap.is_expired,

        # 指标
        "amplitude": snap.amplitude,
        "pct_change": snap.pct_change,

        # 门禁明细
        "gates": gate_result.gates,
        "trend": "up" if snap.close > (df["close"].tail(5).mean() if len(df) >= 5 else snap.close) else "down",
    }


def get_daily_df(code: str, days: int = 60):
    """兼容旧接口，内部调用market_data"""
    from market_data import get_daily_df as _get_df
    return _get_df(code, days)
