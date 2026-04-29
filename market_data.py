"""
v3.1 统一行情层（Single Source of Truth）
==========================================
核心理念：整个系统中，所有模块必须使用同一价格基准
架构：
  MarketSnapshot → 唯一真实数据源
               → 门禁系统
               → 信号计算
               → 执行层
               → 推送展示
所有价格：同源、同时间、同精度
"""

import time
import requests
import pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Tuple

# ─── Tushare Token ───
TUSHARE_TOKEN = "d756d9218d4665c7c38f1b539446b05d1504c7df637e8e90e30bc6ea"
TUSHARE_BASE = "http://api.tushare.pro"


# ═══════════════════════════════════════════════════════
# 统一行情快照（Single Source of Truth）
# ═══════════════════════════════════════════════════════

@dataclass
class MarketSnapshot:
    """
    行情快照：系统中所有模块的唯一价格来源
    ==========================================
    所有价格字段来自同一时刻、同一接口
    时间戳锁：超过 SIGNAL_VALIDITY_MINUTES 分钟 → 自动失效
    """
    # 标的
    code: str
    name: str

    # 时间戳（数据获取时刻）
    fetched_at: float = field(default_factory=time.time)  # Unix时间戳

    # 日线数据（昨收用）
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0          # 昨收价（日线收盘价）
    volume: float = 0.0
    amplitude: float = 0.0      # 振幅 (high-low)/close
    pct_change: float = 0.0     # 涨跌幅

    # 实时价（当前价）
    current_price: float = 0.0
    price_source: str = ""       # 数据来源描述
    is_realtime: bool = False

    # 数据新鲜度
    data_date: str = ""          # K线日期 "YYYY-MM-DD"
    data_age_minutes: float = 0.0  # 数据年龄（分钟）

    # 元数据
    fetch_success: bool = False
    error_msg: str = ""

    # ─── 时间锁控制 ───
    SIGNAL_VALIDITY_MINUTES: int = 5  # 信号有效期（分钟）

    @property
    def is_expired(self) -> bool:
        """检查信号是否已过期（超过5分钟）"""
        age = (time.time() - self.fetched_at) / 60
        return age > self.SIGNAL_VALIDITY_MINUTES

    @property
    def age_display(self) -> str:
        """数据年龄展示"""
        age = (time.time() - self.fetched_at) / 60
        if age < 1:
            return f"{int(age*60)}秒前"
        return f"{int(age)}分钟前"

    @property
    def reference_price(self) -> float:
        """
        参考价：用于所有决策的唯一价格
        优先级：实时价 > 今开 > 昨收
        """
        if self.is_realtime and self.current_price > 0:
            return self.current_price
        if self.open > 0:
            return self.open
        return self.close

    @property
    def price_for_gate(self) -> float:
        """门禁系统用的价格（昨收/实时价的统一口径）"""
        return self.close  # 门禁用昨收（基准）

    @property
    def price_for_signal(self) -> float:
        """信号计算用的价格（用昨收，口径统一）"""
        return self.close

    def to_dict(self) -> dict:
        """序列化（用于调试/推送）"""
        return {
            "code": self.code,
            "name": self.name,
            "data_date": self.data_date,
            "data_age": self.age_display,
            "is_expired": self.is_expired,
            "prev_close": self.prev_close,
            "close": self.close,
            "current_price": self.current_price,
            "price_source": self.price_source,
            "is_realtime": self.is_realtime,
            "reference_price": self.reference_price,
            "amplitude": f"{self.amplitude:.2%}",
            "pct_change": f"{self.pct_change:+.2f}%",
            "fetch_success": self.fetch_success,
        }


# ═══════════════════════════════════════════════════════
# Tushare API（内部用）
# ═══════════════════════════════════════════════════════

def _tushare_api(api_name: str, params: dict, fields: str, retries: int = 3) -> pd.DataFrame:
    """Tushare API 通用调用"""
    for attempt in range(retries):
        try:
            payload = {
                "api_name": api_name,
                "token": TUSHARE_TOKEN,
                "params": params,
                "fields": fields
            }
            resp = requests.post(TUSHARE_BASE, json=payload, timeout=30)
            result = resp.json()
            if result.get("code") == 0:
                cols = result["data"]["fields"]
                rows = result["data"]["items"]
                return pd.DataFrame(rows, columns=cols)
            else:
                print(f"  ⚠️ Tushare错误 [{attempt+1}]: {result.get('msg')}")
        except Exception as e:
            print(f"  ⚠️ 网络异常 [{attempt+1}]: {e}")

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    raise ConnectionError(f"Tushare {api_name} 连续{retries}次失败")


# ═══════════════════════════════════════════════════════
# 统一数据获取（Single Source of Truth）
# ═══════════════════════════════════════════════════════

def fetch_market_snapshot(code: str, name: str) -> MarketSnapshot:
    """
    获取统一行情快照
    =====================
    整个系统只调用这一个函数获取数据
    所有价格字段来自同一批次请求
    """
    snap = MarketSnapshot(code=code, name=name, fetched_at=time.time())

    # ── Step 1: 获取日线数据（Tushare，唯一来源） ──
    try:
        end_date = datetime.today().strftime("%Y%m%d")
        start_date = (datetime.today() - pd.Timedelta(days=90)).strftime("%Y%m%d")
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        df = _tushare_api(
            "daily",
            params={"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
            fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
        )

        if len(df) == 0 or "trade_date" not in df.columns:
            raise ConnectionError(f"Tushare返回空或缺少字段: {list(df.columns)}")

        # 只保留有成交量的（过滤停牌）
        df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
        df = df[df["vol"].notna() & (df["vol"] > 0)].reset_index(drop=True)

        for col in ["open", "high", "low", "close", "pct_chg", "vol"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.sort_values("trade_date").reset_index(drop=True)
        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) >= 2 else latest_row

        snap.prev_close = float(prev_row["close"])
        snap.open = float(latest_row["open"])
        snap.high = float(latest_row["high"])
        snap.low = float(latest_row["low"])
        snap.close = float(latest_row["close"])
        snap.volume = float(latest_row["vol"])
        snap.pct_change = float(latest_row.get("pct_chg", 0))
        snap.amplitude = (snap.high - snap.low) / snap.close if snap.close > 0 else 0

        # 数据日期
        trade_date_str = str(latest_row["trade_date"])
        snap.data_date = f"{trade_date_str[:4]}-{trade_date_str[4:6]}-{trade_date_str[6:8]}"

        # 判断数据新鲜度（是否今日）
        today_str = datetime.today().strftime("%Y%m%d")
        snap.data_age_minutes = 0 if trade_date_str == today_str else (24 * 60)

    except Exception as e:
        snap.fetch_success = False
        snap.error_msg = f"日线数据失败: {e}"
        return snap

    # ── Step 2: 获取实时价（新浪，唯一来源） ──
    # 注意：只补充实时价，不重新获取日线
    try:
        suffix = "sh" if code.startswith("6") else "sz"
        url = f"http://hq.sinajs.cn/list={suffix}{code}"
        headers = {
            "Referer": "http://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        raw = resp.text
        if '"' in raw:
            parts = raw.split('"')[1].split(',')
            if len(parts) > 3:
                snap.current_price = float(parts[3])
                snap.price_source = f"新浪 {parts[4] if len(parts)>4 else ''}"
                snap.is_realtime = True
    except Exception as e:
        # 实时价获取失败，使用昨收
        snap.current_price = snap.close
        snap.price_source = "新浪(失败)→Tushare昨收"
        snap.is_realtime = False

    snap.fetch_success = True
    return snap


# ═══════════════════════════════════════════════════════
# 导出别名（兼容旧接口）
# ═══════════════════════════════════════════════════════

def get_daily_df(code: str, days: int = 60):
    """兼容旧接口，建议统一用 fetch_market_snapshot"""
    end_date = datetime.today().strftime("%Y%m%d")
    start_date = (datetime.today() - pd.Timedelta(days=days+30)).strftime("%Y%m%d")
    ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    df = _tushare_api(
        "daily",
        params={"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
    )
    df = df.rename(columns={"trade_date": "date", "vol": "volume", "pct_chg": "pct_change"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["volume"].notna() & (df["volume"] > 0)].reset_index(drop=True)
    return df


def get_realtime_price(code: str) -> Tuple[float, str, bool]:
    """兼容旧接口，建议统一用 fetch_market_snapshot"""
    snap = fetch_market_snapshot(code, "")
    return snap.current_price, snap.price_source, snap.is_realtime
