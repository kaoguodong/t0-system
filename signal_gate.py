"""
v3.3 RR盈亏比过滤 + 自动止损 + 人话结论
=======================================
升级内容：
1. RR（盈亏比）过滤：RR<1.5直接拒绝，不划算不交易
2. 自动止损：基于做T区间计算止损位（区间下方R*10%）
3. RR决策仓位：RR>2.0可加仓25%，RR<1.5拒绝
4. 人话结论：先给结论，再给原因（可执行）
"""

import time
import pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List
from market_data import MarketSnapshot, fetch_market_snapshot


# ═══════════════════════════════════════════════════════
# 门禁规则配置 v3.3
# ═══════════════════════════════════════════════════════

GATE_RULES = {
    "score_min": 3,
    "amplitude_min": 0.04,
    "volume_ratio_min": 0.6,
    # 仓位规则（趋势感知）
    "max_position_if_score4": 0.50,      # 4分 → 50%
    "max_position_if_score3_above_ma5": 0.30,  # 3分+MA5上方 → 30%
    "max_position_if_score3_below_ma5": 0.25,   # 3分+MA5下方 → 25%
    # RR规则
    "rr_min": 1.5,          # RR门槛，低于此值直接拒绝
    "rr_bonus_threshold": 2.0,  # RR>2.0 → 可加仓
    "rr_bonus_factor": 0.25,    # RR>2.0时额外加仓25%
    # 止损
    "stop_loss_pct": 0.10,    # 止损 = buy1 - R * 10%（区间宽度的10%）
    # 频率控制
    "max_trades_per_day": 2,
    "cooldown_minutes": 30,
    "signal_dedup_minutes": 5,
}


# ═══════════════════════════════════════════════════════
# 交易频率状态机
# ═══════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    timestamp: float
    code: str
    action: str
    price: float
    quantity: int
    position_ratio: float
    signal_score: int
    rr: float = 0.0
    stop_loss: float = 0.0
    gate_passed: bool = False


@dataclass
class FrequencyController:
    daily_trades: List[TradeRecord] = field(default_factory=list)
    last_signal_time: dict = field(default_factory=dict)
    trade_today_count: dict = field(default_factory=dict)

    def reset_if_new_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if not hasattr(self, "_check_date") or self._check_date != today:
            self.daily_trades = [t for t in self.daily_trades
                                  if datetime.fromtimestamp(t.timestamp).strftime("%Y-%m-%d") == today]
            self.trade_today_count = {}
            self._check_date = today

    def can_trade(self, code: str) -> tuple[bool, str]:
        self.reset_if_new_day()
        now = time.time()
        count_today = self.trade_today_count.get(code, 0)
        if count_today >= GATE_RULES["max_trades_per_day"]:
            return False, "已达每日上限"
        last = self.last_signal_time.get(code, 0)
        elapsed = (now - last) / 60
        if elapsed < GATE_RULES["cooldown_minutes"]:
            return False, "冷静期"
        return True, ""

    def record_trade(self, trade: TradeRecord):
        self.reset_if_new_day()
        code = trade.code
        self.daily_trades.append(trade)
        self.trade_today_count[code] = self.trade_today_count.get(code, 0) + 1
        self.last_signal_time[code] = trade.timestamp

    def signal_dedup(self, code: str) -> bool:
        self.reset_if_new_day()
        now = time.time()
        last = self.last_signal_time.get(code, 0)
        if (now - last) < GATE_RULES["signal_dedup_minutes"] * 60:
            return False
        return True

    def get_status(self, code: str) -> dict:
        self.reset_if_new_day()
        today = datetime.now().strftime("%Y-%m-%d")
        trades = [t for t in self.daily_trades if t.code == code
                  and datetime.fromtimestamp(t.timestamp).strftime("%Y-%m-%d") == today]
        return {
            "trades_today": len(trades),
            "max_allowed": GATE_RULES["max_trades_per_day"],
            "remaining": GATE_RULES["max_trades_per_day"] - len(trades),
            "last_trade": trades[-1] if trades else None,
        }


_freq_ctrl = FrequencyController()


# ═══════════════════════════════════════════════════════
# RR + 止损计算
# ═══════════════════════════════════════════════════════

@dataclass
class RiskReward:
    """盈亏比分析结果"""
    rr: float                    # 盈亏比 = TP / SL距离
    tp_price: float             # 止盈价（sell1）
    sl_price: float             # 止损价
    sl_distance: float          # 止损距离（元）
    tp_distance: float          # 止盈距离（元）
    verdict: str                # "DO" / "SKIP"
    verdict_reason: str         # 人话原因
    position_modifier: float    # 仓位修正系数（RR加仓/减仓）
    stop_loss_pct: float        # 止损比例（相对于buy1）


def calc_risk_reward(buy1: float, sell1: float, H: float, L: float) -> RiskReward:
    """
    计算RR（盈亏比）和自动止损
    ==============================
    止损 = buy1 - R * 10%  （区间下方10%处）
    止盈 = sell1           （区间上方）
    RR   = tp_distance / sl_distance

    决策：
    - RR < 1.5  → SKIP（不划算）
    - RR 1.5~2.0 → 正常仓位
    - RR > 2.0  → 可加仓25%
    """
    R = H - L
    sl_price = round(buy1 - R * GATE_RULES["stop_loss_pct"], 2)
    tp_price = sell1

    sl_distance = abs(buy1 - sl_price)
    tp_distance = abs(tp_price - buy1)

    if sl_distance <= 0:
        # 区间太窄，无法计算有效止损
        return RiskReward(
            rr=0.0, tp_price=tp_price, sl_price=sl_price,
            sl_distance=0.0, tp_distance=tp_distance,
            verdict="SKIP", verdict_reason="区间过窄，无有效止损",
            position_modifier=0.0, stop_loss_pct=0.0
        )

    rr = tp_distance / sl_distance

    # 决策
    if rr < GATE_RULES["rr_min"]:
        verdict = "SKIP"
        verdict_reason = "RR %.1f < %.1f，不划算" % (rr, GATE_RULES["rr_min"])
        position_modifier = 0.0
    elif rr >= GATE_RULES["rr_bonus_threshold"]:
        verdict = "DO"
        verdict_reason = "RR %.1f > %.1f，优质信号，可加仓" % (
            rr, GATE_RULES["rr_bonus_threshold"])
        position_modifier = GATE_RULES["rr_bonus_factor"]   # +25%
    else:
        verdict = "DO"
        verdict_reason = "RR %.1f 在正常区间（%.1f~%.1f）" % (
            rr, GATE_RULES["rr_min"], GATE_RULES["rr_bonus_threshold"])
        position_modifier = 0.0

    return RiskReward(
        rr=rr,
        tp_price=tp_price,
        sl_price=sl_price,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
        verdict=verdict,
        verdict_reason=verdict_reason,
        position_modifier=position_modifier,
        stop_loss_pct=GATE_RULES["stop_loss_pct"],
    )


# ═══════════════════════════════════════════════════════
# 信号门禁 v3.3（整合RR过滤）
# ═══════════════════════════════════════════════════════

@dataclass
class GateResult:
    passed: bool
    score: int
    max_score: int
    reject_reason: str | None
    position_ratio: float          # 修正后最终仓位
    raw_position_ratio: float      # 原始仓位（RR修正前）
    buy_prices: tuple[float, float]
    sell_prices: tuple[float, float]
    gates: dict
    trend_label: str
    freq_status: dict
    can_push: bool = True
    push_suppressed: bool = False
    rr_result: RiskReward | None = None


def calc_signal_gate(snap: MarketSnapshot, df: pd.DataFrame | None = None) -> dict:
    """
    v3.3 信号门禁（整合RR过滤 + 人话结论）
    =========================================
    改进：
    - 先过4门禁（score/amplitude/sentiment/volume）
    - 再过RR过滤（RR<1.5 → 直接拒绝）
    - 最终仓位 = 原始仓位 * (1 + position_modifier)
    - 人话结论：先给结论，再给原因
    """
    if df is None:
        try:
            from market_data import get_daily_df as _get_df
            df = _get_df(snap.code, days=60)
        except Exception:
            df = pd.DataFrame()

    code = snap.code
    amp_th = GATE_RULES["amplitude_min"]

    # ── 计算MA5 ──
    ma5 = df["close"].tail(5).mean() if len(df) >= 5 else snap.close
    volume_ma5 = df["volume"].tail(5).mean() if len(df) >= 5 else snap.volume

    # ── 4门禁打分 ──
    score = 0
    gates = {}

    above_ma5 = snap.close > ma5
    score += 1 if above_ma5 else 0
    gates["G1"] = {
        "name": "趋势",
        "pass": above_ma5,
        "value": ("MA5上方" if above_ma5 else "MA5下方") +
                 " (%.2f vs MA5:%.2f)" % (snap.close, ma5)
    }

    amplitude_ok = snap.amplitude >= amp_th
    score += 1 if amplitude_ok else 0
    gates["G2"] = {
        "name": "振幅",
        "pass": amplitude_ok,
        "value": ("%.2f%% OK" if amplitude_ok else "%.2f%% FAIL") % (
            snap.amplitude * 100) + " >= %.0f%%" % (amp_th * 100)
    }

    sentiment_ok = abs(snap.pct_change) < 5.0
    score += 1 if sentiment_ok else 0
    gates["G3"] = {
        "name": "情绪",
        "pass": sentiment_ok,
        "value": ("%.2f%% OK" if sentiment_ok else "%.2f%% 过热") % snap.pct_change
    }

    vol_ratio = snap.volume / volume_ma5 if volume_ma5 > 0 else 1.0
    volume_ok = vol_ratio >= GATE_RULES["volume_ratio_min"]
    score += 1 if volume_ok else 0
    gates["G4"] = {
        "name": "量能",
        "pass": volume_ok,
        "value": "量比 %.2f %s %.1f" % (
            vol_ratio, "OK" if volume_ok else "LOW", GATE_RULES["volume_ratio_min"])
    }

    trend_label = "above_MA5" if above_ma5 else "below_MA5"

    # ── 原始仓位（趋势感知） ──
    amplitude_hard_pass = snap.amplitude >= GATE_RULES["amplitude_min"]
    if score >= 4 and amplitude_hard_pass:
        raw_pos = GATE_RULES["max_position_if_score4"]
    elif score >= 3 and amplitude_hard_pass:
        raw_pos = (GATE_RULES["max_position_if_score3_above_ma5"] if above_ma5
                   else GATE_RULES["max_position_if_score3_below_ma5"])
    else:
        raw_pos = 0.0

    # ── 做T区间 + RR计算 ──
    H, L, C = snap.high, snap.low, snap.close
    M = (H + L + C) / 3
    R = H - L
    buy1 = round(M - 0.4 * R, 2)
    buy2 = round(M - 0.6 * R, 2)
    sell1 = round(M + 0.4 * R, 2)
    sell2 = round(M + 0.6 * R, 2)

    rr_result = calc_risk_reward(buy1, sell1, H, L)

    # ── 频率检查 ──
    freq_status = _freq_ctrl.get_status(code)
    can_trade, freq_reject = _freq_ctrl.can_trade(code)

    # ── 综合门禁判定 ──
    if not amplitude_hard_pass:
        reject = "振幅不足（%.2f%% < %.0f%%）" % (snap.amplitude * 100, amp_th * 100)
    elif score < GATE_RULES["score_min"]:
        reject = "评分不足（%d/4）" % score
    elif snap.is_expired:
        reject = "数据过期"
    elif not can_trade:
        reject = freq_reject
    elif rr_result.verdict == "SKIP":
        reject = rr_result.verdict_reason
    else:
        reject = None

    passed = reject is None

    # ── 最终仓位（原始仓位 * RR修正） ──
    if passed:
        pos_modifier = rr_result.position_modifier
        final_pos = raw_pos * (1.0 + pos_modifier)
        final_pos = min(final_pos, 0.60)   # 上限60%
    else:
        final_pos = 0.0

    # ── 去重 ──
    can_push = _freq_ctrl.signal_dedup(code)
    push_suppressed = passed and not can_push

    # ── 人话结论 ──
    if passed:
        verdict_emoji = "GO" if rr_result.rr >= GATE_RULES["rr_min"] else "WAIT"
        verdict_icon = "做" if verdict_emoji == "GO" else "等"
        human_verdict = "[%s] %s" % (verdict_emoji, rr_result.verdict_reason)
    else:
        human_verdict = "[SKIP] " + str(reject)

    gate_result = GateResult(
        passed=passed,
        score=score,
        max_score=4,
        reject_reason=reject,
        position_ratio=final_pos,
        raw_position_ratio=raw_pos,
        buy_prices=(buy1, buy2),
        sell_prices=(sell1, sell2),
        gates=gates,
        trend_label=trend_label,
        freq_status=freq_status,
        can_push=can_push,
        push_suppressed=push_suppressed,
        rr_result=rr_result,
    )

    return {
        # 门禁
        "gate_passed": gate_result.passed,
        "score": gate_result.score,
        "max_score": gate_result.max_score,
        "reject_reason": gate_result.reject_reason,

        # 仓位
        "position_ratio": round(gate_result.position_ratio, 4),
        "raw_position_ratio": gate_result.raw_position_ratio,
        "position_note": ("RR加仓+" if rr_result and rr_result.position_modifier > 0
                          else ("RR正常" if passed else "")),

        # 区间
        "buy": gate_result.buy_prices,
        "sell": gate_result.sell_prices,

        # 趋势
        "trend_label": gate_result.trend_label,
        "above_ma5": above_ma5,

        # 频率
        "freq_status": gate_result.freq_status,
        "freq_display": ("%d/%d今日 | 剩%d次" % (
            freq_status["trades_today"], freq_status["max_allowed"],
            freq_status["remaining"])),
        "can_push": gate_result.can_push,
        "push_suppressed": gate_result.push_suppressed,

        # RR + 止损
        "rr": round(rr_result.rr, 2) if rr_result else 0.0,
        "tp_price": rr_result.tp_price if rr_result else 0.0,
        "sl_price": rr_result.sl_price if rr_result else 0.0,
        "sl_distance": round(rr_result.sl_distance, 2) if rr_result else 0.0,
        "tp_distance": round(rr_result.tp_distance, 2) if rr_result else 0.0,
        "rr_verdict": rr_result.verdict if rr_result else "SKIP",
        "rr_reason": rr_result.verdict_reason if rr_result else "",
        "stop_loss_pct": rr_result.stop_loss_pct if rr_result else 0.0,

        # 人话结论
        "human_verdict": human_verdict,

        # 统一价格
        "unified_price": snap.reference_price,
        "price_label": "Unified",

        # 数据新鲜度
        "data_date": snap.data_date,
        "data_age": snap.age_display,
        "is_expired": snap.is_expired,

        # 指标
        "amplitude": snap.amplitude,
        "pct_change": snap.pct_change,
        "close": snap.close,

        # 门禁明细
        "gates": gate_result.gates,
    }


def record_buy(code: str, price: float, qty: int, score: int,
               position_ratio: float, rr: float, sl_price: float):
    trade = TradeRecord(
        timestamp=time.time(), code=code, action="BUY", price=price,
        quantity=qty, position_ratio=position_ratio, signal_score=score,
        rr=rr, stop_loss=sl_price, gate_passed=True,
    )
    _freq_ctrl.record_trade(trade)


def record_sell(code: str, price: float, qty: int, score: int, position_ratio: float):
    trade = TradeRecord(
        timestamp=time.time(), code=code, action="SELL", price=price,
        quantity=qty, position_ratio=position_ratio, signal_score=score,
        gate_passed=True,
    )
    _freq_ctrl.record_trade(trade)


def get_freq_status(code: str) -> dict:
    return _freq_ctrl.get_status(code)