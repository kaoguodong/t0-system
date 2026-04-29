import pandas as pd
import requests
from datetime import datetime, timedelta

# Tushare Token（请替换为您的token，或使用下面的demo token）
TUSHARE_TOKEN = "2a6af9c37d8f2c2f5d54e7c9a3b7e6f1d4c8a2b5e6f7g3h9i0j1k2l3m4n5o6p"

TUSHARE_BASE = "http://api.tushare.pro"


def _tushare_api(api_name: str, params: dict, fields: str) -> pd.DataFrame:
    """Tushare Pro API 通用调用"""
    import os
    token = os.getenv("TUSHARE_TOKEN") or TUSHARE_TOKEN

    payload = {
        "api_name": api_name,
        "token": token,
        "params": params,
        "fields": fields
    }

    resp = requests.post(TUSHARE_BASE, json=payload, timeout=30)
    result = resp.json()

    if result.get("code") != 0:
        raise ConnectionError(f"Tushare错误 {result.get('code')}: {result.get('msg')}")

    cols = result["data"]["fields"]
    rows = result["data"]["items"]
    return pd.DataFrame(rows, columns=cols)


def get_daily_df(code: str, days: int = 60) -> pd.DataFrame:
    """获取A股日线行情（使用tushare pro）"""
    end_date = datetime.today().strftime("%Y%m%d")
    start_date = (datetime.today() - timedelta(days=days + 30)).strftime("%Y%m%d")

    # tushare需要带交易所后缀：沪市用.SH，深市用.SZ
    if code.startswith("6"):
        ts_code = f"{code}.SH"
    elif code.startswith("0") or code.startswith("3"):
        ts_code = f"{code}.SZ"
    else:
        ts_code = f"{code}.SH"

    df = _tushare_api(
        "daily",
        params={"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"
    )

    df = df.rename(columns={
        "trade_date": "date",
        "vol": "volume",
        "pct_chg": "pct_change"
    })

    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)

    # 过滤停牌
    df = df[df["volume"] > 0].reset_index(drop=True)
    return df


def get_realtime_price(code: str) -> float:
    """获取实时最新价（来自新浪）"""
    try:
        suffix = "sh" if code.startswith("6") else "sz"
        url = f"http://hq.sinajs.cn/list={suffix}{code}"
        headers = {"Referer": "http://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.text.split('"')[1].split(',')
        return float(data[3])
    except Exception:
        df = get_daily_df(code, days=3)
        return float(df.iloc[-1]["close"])
