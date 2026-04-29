import akshare as ak
import pandas as pd
from datetime import datetime, timedelta


def get_daily_df(code: str, days: int = 60) -> pd.DataFrame:
    """获取A股日线行情（akshare接口）"""
    end_date = datetime.today().strftime("%Y%m%d")
    start_date = (datetime.today() - timedelta(days=days + 30)).strftime("%Y%m%d")

    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq"
    )

    df = df.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_change"
    })

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = df[["date", "open", "high", "low", "close", "volume", "amount", "pct_change"]]

    # 过滤停牌
    df = df[df["volume"] > 0].reset_index(drop=True)
    return df


def get_realtime_price(code: str) -> float:
    """获取实时最新价（快照用）"""
    try:
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == code]
        if not row.empty:
            return float(row.iloc[0]["最新价"])
    except Exception:
        pass
    # 兜底：取日线最后一根收盘价
    df = get_daily_df(code, days=3)
    return float(df.iloc[-1]["close"])
