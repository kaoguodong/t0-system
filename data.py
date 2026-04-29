import baostock as bs
import pandas as pd
from datetime import datetime, timedelta


def get_daily_df(code: str, days: int = 60) -> pd.DataFrame:
    """获取A股日线行情（baostock接口，稳定兼容国内网络）"""
    # 登录baostock
    lg = bs.login()
    if lg.error_code != '0':
        raise ConnectionError(f"baostock登录失败: {lg.error_msg}")

    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    # baostock需要sh/sz前缀
    bs_code = f"sh.{code}" if code.startswith("6") else f"sz.{code}"

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,pct_change",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2"  # 前复权
    )

    data_list = []
    while rs.error_code == '0' and rs.next():
        data_list.append(rs.get_row_data())

    bs.logout()

    if not data_list:
        raise ConnectionError(f"无数据返回：{code}")

    df = pd.DataFrame(data_list, columns=[
        "date", "open", "high", "low", "close", "volume", "amount", "pct_change"
    })

    # 类型转换
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_change"]:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["volume"] = df["volume"] * 100  # baostock成交量单位是手，转为股
    df = df[df["volume"] > 0].reset_index(drop=True)  # 过滤停牌

    return df


def get_realtime_price(code: str) -> float:
    """获取实时最新价"""
    try:
        import requests
        url = f"http://hq.sinajs.cn/list=sh{code}" if code.startswith("6") else f"http://hq.sinajs.cn/list=sz{code}"
        headers = {"Referer": "http://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.text.split('"')[1].split(',')
        return float(data[3])
    except Exception:
        pass
    # 兜底：取日线最后一根收盘价
    df = get_daily_df(code, days=3)
    return float(df.iloc[-1]["close"])
