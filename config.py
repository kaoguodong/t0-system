# ===== 标的池 =====
STOCKS = {
    "工业富联": "601138",
    "中科曙光": "603019",
}

# ===== 做T参数 =====
T_CONFIG = {
    "buy_ratio": [0.4, 0.6],   # 低吸系数
    "sell_ratio": [0.4, 0.6],  # 高抛系数
    "amplitude_threshold": 0.04,  # 振幅阈值
}

# ===== 推送（飞书/钉钉 webhook）=====
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/4aa6d736-3520-4c4f-8b48-b5117b8abac7"

# ===== 资金参数（用于仓位建议）=====
CAPITAL = 190000
T_POSITION_RATIO = 0.4  # 做T仓占比

# ===== 回测参数 =====
BACKTEST_INITIAL_CASH = 100000
BACKTEST_START_DAYS_AGO = 60  # 回测窗口（交易日数量）

# ===== 调度时间（北京时间）=====
SCHEDULE_TIME = "15:10"
