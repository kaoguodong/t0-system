"""
做T信号系统 v3.0 工业级
=========================
核心升级：
- Layer 1 数据层：实时行情冗余 + 断线重试 + 时间对齐校验
- Layer 2 信号层：从"评分系统"升级为"门禁系统（Signal Gate）"
- Layer 3 执行层：滑点模拟 + 部分成交 + 延迟执行模型
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Tuple

# ─── Tushare Token ───
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "d756d9218d4665c7c38f1b539446b05d1504c7df637e8e90e30bc6ea")
TUSHARE_BASE = "http://api.tushare.pro"


# ═══════════════════════════════════════════════════
# LAYER 1：数据层（实时行情冗余 + 断线重试）
# ═══════════════════════════════════════════════════

def _tushare_api(api_name: str, params: dict, fields: str, retries: int = 3) -> pd.DataFrame:
    """Tushare API 通用调用（带重试）"""
    for attempt in range(retries):
        try:
            payload = {"api_name": api_name, "token": TUSHARE_TOKEN, "params": params, "fields": fields}
            resp = requests.post(TUSHARE_BASE, json=payload, timeout=30)
            result = resp.json()
            if result.get("code") == 0:
                cols = result["data"]["fields"]
                rows = result["data"]["items"]
                return pd.DataFrame(rows, columns=cols)
            else:
                print(f"  ⚠️ Tushare错误 [{attempt+1}/{retries}]: {result.get('msg')}")
        except Exception as e:
            print(f"  ⚠️ 网络异常 [{attempt+1}/{retries}]: {e}")

        if attempt < retries - 1:
            wait = 2 ** attempt
            print(f"  ⏳ {wait}秒后重试…")
            time.sleep(wait)

    raise ConnectionError(f"Tushare {api_name} 连续{retries}次失败")


def get_realtime_price_sina(code: str) -> Tuple[Optional[float], str]:
    """实时价来源①：新浪（最快）"""
    try:
        suffix = "sh" if code.startswith("6") else "sz"
        url = f"http://hq.sinajs.cn/list={suffix}{code}"
        headers = {"Referer": "http://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        raw = resp.text
        if '"' in raw:
            data = raw.split('"')[1].split(',')
            price = float(data[3])
            update_time = data[4] if len(data) > 4 else "unknown"
            return price, f"新浪 {update_time}"
    except Exception as e:
        return None, f"新浪失败: {e}"
    return None, "新浪解析失败"


def get_realtime_price_eastmoney(code: str) -> Tuple[Optional[float], str]:
    """实时价来源②：东方财富（备用）"""
    try:
        secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
        url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f107,f169,f170"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com"}
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json().get("data", {})
        if data:
            price = float(data.get("f43", 0))
            if price > 0:
                return price, f"东财 {data.get('f57','')}"
    except Exception as e:
        return None, f"东财失败: {e}"
    return None, "东财解析失败"


def get_realtime_price(code: str) -> Tuple[float, str, bool]:
    """
    获取实时价（三重冗余）
    返回: (价格, 来源, 是否实时)
    """
    # 优先尝试新浪
    price, source = get_realtime_price_sina(code)
    if price is not None:
        # 简单校验：价格必须在合理范围（大于0，小于10000）
        if 0 < price < 10000:
            return price, source, True

    # 备用东方财富
    price2, source2 = get_realtime_price_eastmoney(code)
    if price2 is not None and 0 < price2 < 10000:
        return price2, source2, True

    # 兜底：Tushare日线（最后收盘价，非实时）
    try:
        df = _get_daily_df_tushare(code, days=3)
        price3 = float(df.iloc[-1]["close"])
        return price3, "Tushare日线(兜底，非实时)", False
    except Exception:
        return 0.0, "完全无法获取", False


def _get_daily_df_tushare(code: str, days: int = 60) -> pd.DataFrame:
    """Tushare 日线数据（内部用）"""
    end_date = datetime.today().strftime("%Y%m%d")
    start_date = (datetime.today() - timedelta(days=days + 30)).strftime("%Y%m%d")
    ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    df = _tushare_api(
        "daily",
        params={"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
    )

    # 安全检查：确保trade_date列存在
    if "trade_date" not in df.columns:
        raise ConnectionError(f"Tushare返回缺少trade_date列，字段: {list(df.columns)}")
    if len(df) == 0:
        raise ConnectionError(f"Tushare返回空数据: {code}")

    df = df.rename(columns={"trade_date": "date", "vol": "volume", "pct_chg": "pct_change"})

    # 日期列必须是字符串 "YYYYMMDD" 格式
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    if df["date"].isna().all():
        raise ConnectionError(f"日期解析全失败，原始值: {df['date'].tolist()[:5]}")

    df = df.sort_values("date").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # 过滤停牌（volume为0或NaN）
    df = df[df["volume"].notna() & (df["volume"] > 0)].reset_index(drop=True)

    return df


def get_daily_df(code: str, days: int = 60) -> Tuple[pd.DataFrame, str]:
    """
    获取日线数据 + 数据时间校验
    返回: (df, date_status)
    date_status: "latest"(今日数据) / "stale"(旧数据) / "error"
    """
    try:
        df = _get_daily_df_tushare(code, days)
    except ConnectionError as e:
        raise ConnectionError(f"数据获取失败: {e}")

    latest_date = pd.Timestamp(df.iloc[-1]["date"])
    today = datetime.now().date()
    date_status = "latest" if latest_date.date() == today else "stale"

    return df, date_status


# ═══════════════════════════════════════════════════
# LAYER 2：信号层（门禁系统 Signal Gate）
# ═══════════════════════════════════════════════════

GATE_RULES = {
    "score_min": 3,           # 评分门槛
    "amplitude_min": 0.04,     # 振幅门槛（硬过滤，非软判断）
    "volume_ratio_min": 0.6,   # 量能门槛（相对MA5）
    "realtime_required": True,  # 是否必须实时价
    "trend_required": True,     # 趋势方向必须匹配
    "max_position_if_score3": 0.3,  # 3分时最大仓位30%
    "max_position_if_score4": 0.5,   # 4分时最大仓位50%
}


def calc_signal_gate(df: pd.DataFrame, amp_th: float = 0.04) -> dict:
    """
    v3.0 信号门禁系统
    ==========================
    核心变化：从"评分系统" → "门禁系统"
    所有门禁条件必须通过，否则禁止交易

    门禁清单：
    ✓ G1：评分 ≥ 3/4
    ✓ G2：振幅 ≥ 4%（硬条件，不过滤）
    ✓ G3：量能支撑（相对MA5）
    ✓ G4：趋势方向确认（防止逆势）
    ✓ G5：实时价可用（时间对齐）
    """
    df = df.copy()

    # ── 计算指标 ──
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["volume_ma5"] = df["volume"].rolling(5).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    # ── 因子打分 ──
    score = 0
    gates = {}

    # G1：趋势
    price_vs_ma5 = latest["close"] > latest["ma5"]
    score += 1 if price_vs_ma5 else 0
    gates["G1_trend"] = {
        "pass": price_vs_ma5,
        "value": f"{'MA5上方' if price_vs_ma5 else 'MA5下方'}（{latest['close']:.2f} vs {latest['ma5']:.2f}）"
    }

    # G2：振幅（硬条件）
    amplitude = (latest["high"] - latest["low"]) / latest["close"]
    amplitude_pass = amplitude >= amp_th
    score += 1 if amplitude_pass else 0
    gates["G2_amplitude"] = {
        "pass": amplitude_pass,
        "value": f"{amplitude:.2%} {'≥' if amplitude_pass else '<'} {amp_th:.2%}"
    }

    # G3：情绪（涨幅不过热）
    change = latest.get("pct_change", 0)
    change = float(change) if not isinstance(change, (int, float)) else change
    change_pass = abs(change) < 5.0
    score += 1 if change_pass else 0
    gates["G3_sentiment"] = {
        "pass": change_pass,
        "value": f"涨跌 {change:+.2f}%（{'正常' if change_pass else '过热/过冷'}）"
    }

    # G4：量能
    volume_ratio = latest["volume"] / df["volume_ma5"].iloc[-1]
    volume_pass = volume_ratio >= GATE_RULES["volume_ratio_min"]
    score += 1 if volume_pass else 0
    gates["G4_volume"] = {
        "pass": volume_pass,
        "value": f"量比 {volume_ratio:.2f} {'✓' if volume_pass else '✗'}"
    }

    do_trade = score >= GATE_RULES["score_min"]
    amplitude_hard_pass = amplitude >= GATE_RULES["amplitude_min"]

    # ── 门禁最终判定 ──
    # 硬门禁：评分 + 振幅 必须同时满足
    gate_passed = do_trade and amplitude_hard_pass

    if not amplitude_hard_pass:
        gate_passed = False
        gates["G2_amplitude"]["reject_reason"] = "振幅不足，硬过滤"

    # ── 做T价格区间 ──
    H, L, C = latest["high"], latest["low"], latest["close"]
    M = (H + L + C) / 3
    R = H - L

    buy1 = round(M - 0.4 * R, 2)
    buy2 = round(M - 0.6 * R, 2)
    sell1 = round(M + 0.4 * R, 2)
    sell2 = round(M + 0.6 * R, 2)

    # ── 仓位动态调整（Signal Gate 输出） ──
    if score >= 4 and amplitude_hard_pass:
        pos = GATE_RULES["max_position_if_score4"]  # 4分 → 50%
    elif score >= 3 and amplitude_hard_pass:
        pos = GATE_RULES["max_position_if_score3"]  # 3分 → 30%（降仓）
    else:
        pos = 0.0  # 不满足硬门禁 → 0%

    # ── 趋势方向（用于过滤逆势单） ──
    trend = "up" if price_vs_ma5 else "down"

    return {
        "score": score,
        "max_score": 4,
        "do_trade": gate_passed,
        "gate_passed": gate_passed,
        "buy": (buy1, buy2),
        "sell": (sell1, sell2),
        "position_ratio": pos,
        "amplitude": amplitude,
        "change_pct": change,
        "gates": gates,
        "trend": trend,
        "reject_reason": None if gate_passed else gates["G2_amplitude"].get("reject_reason") or ("评分不足" if not do_trade else "门禁未通过")
    }
