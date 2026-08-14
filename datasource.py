"""数据源配置: 现货/合约 URL 集中管理, 换数据源只改此文件。

用法:
    from datasource import SPOT, FUTURES
    url = FUTURES["rest_kline"]

合约(USDⓈ-M Futures) REST/WS 消息格式与现货一致, 解析代码可复用。
"""

SPOT = {
    "name": "现货",
    "ws_kline": "wss://stream.binance.com:9443/ws/ethusdt@kline_1h",
    "ws_ticker": "wss://stream.binance.com:9443/ws/ethusdt@ticker",
    "ws_aggtrade": "wss://stream.binance.com:9443/ws/ethusdt@aggTrade",
    "rest_kline": "https://api.binance.com/api/v3/klines",
    "rest_ticker": "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
}

FUTURES = {
    "name": "合约",
    "ws_kline": "wss://fstream.binance.com/ws/ethusdt@kline_1h",
    "ws_ticker": "wss://fstream.binance.com/ws/ethusdt@ticker",
    "ws_aggtrade": "wss://fstream.binance.com/ws/ethusdt@aggTrade",
    "rest_kline": "https://fapi.binance.com/fapi/v1/klines",
    "rest_ticker": "https://fapi.binance.com/fapi/v1/ticker/price?symbol=ETHUSDT",
    "rest_long_short_ratio_top": "https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=ETHUSDT&period=5m",
    "rest_long_short_ratio_acct": "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=ETHUSDT&period=5m",
}
