"""
v3.2.1 交易频率控制 + 信号去重 + 反复横跳过滤
================================================
升级内容：
1. 展示层统一价格：不再展示多价格，只展示单一参考价
2. 交易频率控制：每日最多2次交易信号，30分钟冷静期
3. 趋势过滤：MA5下方仓位降至25%（逆势降仓）
4. 信号去重：同一标的5分钟内不重复推送
"""

import time
import pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List
from market_data import MarketSnapshot, fetch_market_snapshot


# ═══════════════════════════════════════════════════════
# 门禁规则配置
# ═══════════════════════════════════════════════════════

GATE_RULES = {
    "score_min": 3,
    "amplitude_min": 0.04,
    "volume_ratio_min": 0.6,
    # 仓位规则（趋势感知）
    "max_position_if_score4": 0.50,      # 4分 → 50%
    "max_position_if_score3_above_ma5": 0.30,  # 3分+MA5上方 → 30%
    "max_position_if_score3_below_ma5": 0.25,   # 3分+MA5下方 → 25%（逆势降仓）
    # 频率控制
    "max_trades_per_day": 2,             # 每日最多2次交易
    "cooldown_minutes": 30,              # 冷静期30分钟
    "signal_dedup_minutes": 5,           # 同标的不重复推送
}


# ═══════════════════════════════════════════════════════
# 交易频率状态机（全局）
# ═══════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """单笔交易记录"""
    timestamp: float
    code: str
    action: str       # "BUY" or "SELL"
    price: float
    quantity: int
    position_ratio: float
    signal_score: int
    gate_passed: bool


@dataclass
class FrequencyController:
    """
    频率控制器：控制交易频率、防重复、防反复横跳
    ==============================================
    策略：同一个标的，每天最多2笔，30分钟冷静期
    """
    daily_trades: List[TradeRecord] = field(default_factory=list)
    last_signal_time: dict = field(default_factory=dict)  # code -> timestamp
    trade_today_count: dict = field(default_factory=dict)  # code -> count

    def reset_if_new_day(self):
        """检查是否跨天，重置计数"""
        today = datetime.now().strftime("%Y-%m-%d")
        if not hasattr(self, '_check_date') or self._check_date != today:
            self.daily_trades = [t for t in self.daily_trades
                                  if datetime.fromtimestamp(t.timestamp).strftime("%Y-%m-%d") == today]
            self.trade_today_count = {}
            self._check_date = today

    def can_trade(self, code: str, signal_score: int, position_ratio: float) -> tuple[bool, str]:
        """
        检查是否可以交易
        返回：(允许, 拒绝原因)
        """
        self.reset_if_new_day()
        now = time.time()

        # 1. 每日次数限制
        count_today = self.trade_today_count.get(code, 0)
        if count_today >= GATE_RULES["max_trades_per_day"]:
            return False, f"今日已达{GATE_RULES['max_trades_per_day']}次上限"

        # 2. 冷静期检查
        last = self.last_signal_time.get(code, 0)
        elapsed = (now - last) / 60
        if elapsed < GATE_RULES["cooldown_minutes"]:
            return False, f"冷静期中（{int(GATE_RULES['cooldown_minutes'] - elapsed)}分钟）"

        return True, ""

    def record_trade(self, trade: TradeRecord):
        """记录一笔交易"""
        self.reset_if_new_day()
        code = trade.code
        self.daily_trades.append(trade)
        self.trade_today_count[code] = self.trade_today_count.get(code, 0) + 1
        self.last_signal_time[code] = trade.timestamp

    def signal_dedup(self, code: str) -> bool:
        """
        信号去重：5分钟内同标的只推送一次
        返回 True = 可以推送，False = 静默（重复）
        """
        self.reset_if_new_day()
        now = time.time()
        last = self.last_signal_time.get(code, 0)
        if (now - last) < GATE_RULES["signal_dedup_minutes"] * 60:
            return False  # 静默
        return True

    def get_status(self, code: str) -> dict:
        """获取该标的今日交易状态"""
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


# 全局单例
_freq_ctrl = FrequencyController()


# ═══════════════════════════════════════════════════════
# 门禁判定（整合频率控制）
# ═══════════════════════════════════════════════════════

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
    # v3.2.1 新增
    trend_label: str = ""          # "above_MA5" | "below_MA5"
    freq_status: dict = field(default_factory=dict)
    can_push: bool = True
    push_suppressed: bool = False


def calc_signal_gate(snap: MarketSnapshot, df: pd.DataFrame | None = None) -> dict:
    """
    v3.2.1 信号门禁（整合频率控制 + 去重）
    =========================================
    改进：
    - MA5下方 → 仓位降至25%
    - 频率控制：每日≤2次，冷静期30分钟
    - 信号去重：5分钟静默重复
    - 展示统一价格（无多价格混用）
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

    # G1: 趋势
    above_ma5 = snap.close > ma5
    score += 1 if above_ma5 else 0
    gates["G1"] = {
        "name": "Trend",
        "pass": above_ma5,
        "value": ("above MA5" if above_ma5 else "below MA5")
                 + " (price:%.2f vs MA5:%.2f)" % (snap.close, ma5)
    }

    # G2: 振幅（硬条件）
    amplitude_ok = snap.amplitude >= amp_th
    score += 1 if amplitude_ok else 0
    gates["G2"] = {
        "name": "Amplitude",
        "pass": amplitude_ok,
        "value": "Amp %.2f%% %s %.0f%%" % (
            snap.amplitude * 100,
            "OK" if amplitude_ok else "FAIL",
            amp_th * 100
        )
    }

    # G3: 情绪
    sentiment_ok = abs(snap.pct_change) < 5.0
    score += 1 if sentiment_ok else 0
    gates["G3"] = {
        "name": "Sentiment",
        "pass": sentiment_ok,
        "value": "Chg %+.2f%% (%s)" % (
            snap.pct_change,
            "OK" if sentiment_ok else "HOT"
        )
    }

    # G4: 量能
    vol_ratio = snap.volume / volume_ma5 if volume_ma5 > 0 else 1.0
    volume_ok = vol_ratio >= GATE_RULES["volume_ratio_min"]
    score += 1 if volume_ok else 0
    gates["G4"] = {
        "name": "Volume",
        "pass": volume_ok,
        "value": "Vol ratio %.2f %s %.1f" % (
            vol_ratio,
            "OK" if volume_ok else "LOW",
            GATE_RULES["volume_ratio_min"]
        )
    }

    # ── 趋势标签 ──
    trend_label = "above_MA5" if above_ma5 else "below_MA5"

    # ── 仓位决策（趋势感知） ──
    amplitude_hard_pass = snap.amplitude >= GATE_RULES["amplitude_min"]

    if score >= 4 and amplitude_hard_pass:
        pos = GATE_RULES["max_position_if_score4"]
    elif score >= 3 and amplitude_hard_pass:
        if above_ma5:
            pos = GATE_RULES["max_position_if_score3_above_ma5"]   # 30%
        else:
            pos = GATE_RULES["max_position_if_score3_below_ma5"]   # 25%（逆势降仓）
    else:
        pos = 0.0

    # ── 频率检查 ──
    freq_status = _freq_ctrl.get_status(code)
    can_trade, freq_reject = _freq_ctrl.can_trade(code, score, pos)

    # ── 做T价格区间 ──
    H, L, C = snap.high, snap.low, snap.close
    M = (H + L + C) / 3
    R = H - L
    buy1 = round(M - 0.4 * R, 2)
    buy2 = round(M - 0.6 * R, 2)
    sell1 = round(M + 0.4 * R, 2)
    sell2 = round(M + 0.6 * R, 2)

    # ── 门禁综合判定 ──
    if not amplitude_hard_pass:
        reject = "Amplitude FAIL (%.2f%% < %.0f%%)" % (
            snap.amplitude * 100, amp_th * 100)
    elif score < GATE_RULES["score_min"]:
        reject = "Score FAIL (%d < %d)" % (score, GATE_RULES["score_min"])
    elif snap.is_expired:
        reject = "Data EXPIRED (%s)" % snap.age_display
    elif not can_trade:
        reject = freq_reject
    else:
        reject = None

    passed = reject is None

    # ── 去重检查 ──
    can_push = _freq_ctrl.signal_dedup(code)
    push_suppressed = (passed and not can_push)

    # ── 频率状态 ──
    freq_display = ("%d/%d trades today" % (
        freq_status["trades_today"], freq_status["max_allowed"]))

    gate_result = GateResult(
        passed=passed,
        score=score,
        max_score=4,
        reject_reason=reject,
        position_ratio=pos,
        buy_prices=(buy1, buy2),
        sell_prices=(sell1, sell2),
        gates=gates,
        trend_label=trend_label,
        freq_status=freq_status,
        can_push=can_push,
        push_suppressed=push_suppressed,
    )

    # ── 统一输出 ──
    return {
        # 门禁
        "gate_passed": gate_result.passed,
        "score": gate_result.score,
        "max_score": gate_result.max_score,
        "reject_reason": gate_result.reject_reason,

        # 仓位
        "position_ratio": gate_result.position_ratio,

        # 区间
        "buy": gate_result.buy_prices,
        "sell": gate_result.sell_prices,

        # 趋势
        "trend_label": gate_result.trend_label,
        "above_ma5": above_ma5,

        # 频率
        "freq_status": gate_result.freq_status,
        "freq_display": freq_display,
        "can_push": gate_result.can_push,
        "push_suppressed": gate_result.push_suppressed,

        # 统一价格（唯一价格，无多价格混用）
        "unified_price": snap.reference_price,
        "price_label": "Unified",  # 统一价格标签

        # 数据新鲜度
        "data_date": snap.data_date,
        "data_age": snap.age_display,
        "is_expired": snap.is_expired,

        # 指标
        "amplitude": snap.amplitude,
        "pct_change": snap.pct_change,

        # 门禁明细
        "gates": gate_result.gates,
    }


def record_buy(code: str, price: float, qty: int, score: int, position_ratio: float):
    """记录一笔买入（供execution层调用）"""
    trade = TradeRecord(
        timestamp=time.time(),
        code=code,
        action="BUY",
        price=price,
        quantity=qty,
        position_ratio=position_ratio,
        signal_score=score,
        gate_passed=True,
    )
    _freq_ctrl.record_trade(trade)


def record_sell(code: str, price: float, qty: int, score: int, position_ratio: float):
    """记录一笔卖出（供execution层调用）"""
    trade = TradeRecord(
        timestamp=time.time(),
        code=code,
        action="SELL",
        price=price,
        quantity=qty,
        position_ratio=position_ratio,
        signal_score=score,
        gate_passed=True,
    )
    _freq_ctrl.record_trade(trade)


def get_freq_status(code: str) -> dict:
    return _freq_ctrl.get_status(code)
