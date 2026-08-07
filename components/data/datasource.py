"""数据源配置 (现货/合约 URL 集中管理)

归纳自根目录 datasource.py. 现货/合约的 REST/WS 消息格式一致, 解析代码可复用,
仅 URL 不同. 换数据源只改本文件.

封装为 DataSource 类 + 保留 SPOT/FUTURES 字典常量 (兼容旧用法).

用法:
    from components.data.datasource import DataSource, SPOT, FUTURES
    url = FUTURES["rest_kline"]                 # 旧式字典访问
    ws  = DataSource.get("futures", "ws_kline") # 类接口
"""
from enum import Enum


class Market(str, Enum):
        """市场类型枚举"""
        SPOT = "spot"      # 现货
        FUTURES = "futures"  # USDⓈ-M 永续合约


# ===== 现货 / 合约 URL 配置 (与根目录 datasource.py 保持一致) =====
SPOT = {
        "name": "现货",
        "ws_kline": "wss://stream.binance.com:9443/ws/ethusdt@kline_1h",
        "ws_ticker": "wss://stream.binance.com:9443/ws/ethusdt@ticker",
        "rest_kline": "https://api.binance.com/api/v3/klines",
        "rest_ticker": "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
}

FUTURES = {
        "name": "合约",
        "ws_kline": "wss://fstream.binance.com/ws/ethusdt@kline_1h",
        "ws_ticker": "wss://fstream.binance.com/ws/ethusdt@ticker",
        "rest_kline": "https://fapi.binance.com/fapi/v1/klines",
        "rest_ticker": "https://fapi.binance.com/fapi/v1/ticker/price?symbol=ETHUSDT",
}


class DataSource:
        """数据源统一访问接口

        将现货/合约的 URL 配置集中管理, 切换数据源只需改 SPOT/FUTURES 字典.
        合约 fapi/fstream 与现货 api/stream 消息格式一致, 解析逻辑可复用.
        """

        _REGISTRY = {
                Market.SPOT: SPOT,
                Market.FUTURES: FUTURES,
                "spot": SPOT,
                "futures": FUTURES,
        }

        @classmethod
        def get(cls, market, key=None):
                """获取指定市场的数据源配置

                Args:
                    market: Market.SPOT / Market.FUTURES / "spot" / "futures"
                    key:    可选, 配置键 (如 "rest_kline"/"ws_kline"/"ws_ticker"/"rest_ticker"/"name")
                            不传则返回整个配置 dict
                """
                cfg = cls._REGISTRY.get(market)
                if cfg is None:
                        raise KeyError(f"未知市场: {market}, 可选: spot / futures")
                return cfg if key is None else cfg[key]

        @classmethod
        def spot(cls, key=None):
                """便捷: 现货配置"""
                return cls.get(Market.SPOT, key)

        @classmethod
        def futures(cls, key=None):
                """便捷: 合约配置"""
                return cls.get(Market.FUTURES, key)
