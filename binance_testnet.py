"""Binance Testnet 下单客户端 — 现货(testnet.binance.vision) + 合约(fapi.testnet.binancefuture.com)

独立模块, 不修改现有模拟交易逻辑. 真实下单走 Testnet, 账户资金为币安免费测试资金.

配置: config_testnet.json (与脚本同目录)
{
  "spot":    {"api_key": "...", "api_secret": "..."},
  "futures": {"api_key": "...", "api_secret": "..."}
}
Testnet Key 需在 https://testnet.binance.vision (现货) /
https://testnet.binancefuture.com (合约) 分别注册创建, 与主网 Key 不通用.

用法:
    from binance_testnet import TestnetClient
    c = TestnetClient("futures")          # 或 "spot"
    print(c.get_balance(), c.get_price())
    c.place_order("buy", "market", 0.01)
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_testnet.json")

BASE_URLS = {
    "spot": "https://testnet.binance.vision",
    "futures": "https://testnet.binancefuture.com",   # 官方文档: USDT-M testnet REST base
}

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897


def load_keys(market):
    """从 config_testnet.json 读取指定市场的 api_key/api_secret, 缺失返回空串"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        k = cfg.get(market, {})
        return str(k.get("api_key", "")).strip(), str(k.get("api_secret", "")).strip()
    except Exception:
        return "", ""


class TestnetClient:
    """轻量 Binance Testnet 客户端: 签名请求 / 下单 / 查余额 / 查持仓 / 查价格"""

    def __init__(self, market, proxy_host=PROXY_HOST, proxy_port=PROXY_PORT):
        if market not in BASE_URLS:
            raise ValueError(f"未知市场: {market}, 可选 {list(BASE_URLS)}")
        self.market = market
        self.base = BASE_URLS[market]
        self.api_key, self.api_secret = load_keys(market)
        self.proxy = f"http://{proxy_host}:{proxy_port}"
        self.ready = bool(self.api_key and self.api_secret)
        self._offset = None   # 服务器-本地时间偏移(ms), 首次签名时获取并缓存

    # ---- 时间同步 ----
    def _server_offset(self):
        """获取币安服务器与本地的时间偏移(ms), 用于签名防 -1021"""
        if self._offset is None:
            path = "/fapi/v1/time" if self.market == "futures" else "/api/v3/time"
            d = self._request("GET", path)
            self._offset = int(d["serverTime"]) - int(time.time() * 1000)
        return self._offset

    # ---- 基础请求 ----
    def _request(self, method, path, params=None, signed=False):
        """发请求; 直连失败自动走代理重试一次. 返回解析后的 JSON dict/list."""
        if signed and not self.ready:
            raise RuntimeError("未配置 Testnet API Key/Secret (config_testnet.json)")
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._server_offset()
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = self.base + path
        headers = {"User-Agent": "Mozilla/5.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers=headers)
        else:
            body = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(
                url, data=body, method="POST", headers={
                    **headers, "Content-Type": "application/x-www-form-urlencoded"})

        def _do(proxies):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
            with opener.open(req, timeout=12) as resp:
                return json.loads(resp.read().decode())

        last_err = None
        for proxies in ({}, {"http": self.proxy, "https": self.proxy}):
            try:
                return _do(proxies)
            except urllib.error.HTTPError as e:
                # API 层错误(签名/参数/权限)直接透出, 不重试
                raise RuntimeError(f"Testnet {self.market} HTTP {e.code}: "
                                   f"{e.read().decode(errors='replace')[:200]}")
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Testnet {self.market} 请求失败: {last_err}")

    # ---- 行情 ----
    def get_price(self):
        path = "/fapi/v1/ticker/price" if self.market == "futures" else "/api/v3/ticker/price"
        d = self._request("GET", path, {"symbol": "ETHUSDT"})
        return float(d["price"])

    # ---- 账户 ----
    def get_balance(self):
        """返回非零资产: [{asset, balance, available}]"""
        if self.market == "futures":
            d = self._request("GET", "/fapi/v2/balance", signed=True)
            return [{"asset": x["asset"], "balance": float(x["balance"]),
                     "available": float(x["availableBalance"])}
                    for x in d if float(x.get("balance", 0)) != 0
                    or float(x.get("availableBalance", 0)) != 0]
        d = self._request("GET", "/api/v3/account", signed=True)
        return [{"asset": x["asset"], "balance": float(x["free"]) + float(x["locked"]),
                 "available": float(x["free"])}
                for x in d.get("balances", [])
                if float(x["free"]) != 0 or float(x["locked"]) != 0]

    def get_position(self):
        """合约: 返回非零持仓列表; 现货: 返回账户中 ETH 数量"""
        if self.market == "futures":
            d = self._request("GET", "/fapi/v2/positionRisk", signed=True)
            return [{"symbol": x["symbol"], "positionAmt": float(x["positionAmt"]),
                     "entryPrice": float(x["entryPrice"]), "markPrice": float(x["markPrice"]),
                     "unRealizedProfit": float(x["unRealizedProfit"]),
                     "leverage": int(x["leverage"]),
                     "liquidationPrice": float(x["liquidationPrice"])}
                    for x in d if float(x.get("positionAmt", 0)) != 0]
        bal = self.get_balance()
        return [b for b in bal if b["asset"] == "ETH"]

    def set_leverage(self, leverage):
        """合约: 设置 ETHUSDT 杠杆 (1~125)"""
        if self.market != "futures":
            return None
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": "ETHUSDT", "leverage": int(leverage)}, signed=True)

    # ---- 下单 ----
    def place_order(self, side, order_type, qty, price=None, reduce_only=False):
        """下单. side: BUY/SELL; order_type: MARKET/LIMIT; qty: ETH 数量.

        精度: 现货 qty 6 位小数, 合约 3 位小数 (ETHUSDT 合约 stepSize=0.001).
        返回 Binance 订单响应 (含 orderId/status/filled 等).
        """
        side = str(side).upper()
        order_type = str(order_type).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side 必须为 BUY/SELL")
        if order_type not in ("MARKET", "LIMIT"):
            raise ValueError("order_type 必须为 MARKET/LIMIT")
        params = {"symbol": "ETHUSDT", "side": side, "type": order_type}
        if order_type == "LIMIT":
            if not price or float(price) <= 0:
                raise ValueError("限价单必须填写有效价格")
            params["price"] = f"{float(price):.2f}"
            params["timeInForce"] = "GTC"
        if not qty or float(qty) <= 0:
            raise ValueError("数量必须大于 0")
        if self.market == "futures":
            params["quantity"] = f"{float(qty):.3f}"
        else:
            params["quantity"] = f"{float(qty):.6f}"
        if reduce_only:
            params["reduceOnly"] = "true"
        path = "/fapi/v1/order" if self.market == "futures" else "/api/v3/order"
        return self._request("POST", path, params, signed=True)


if __name__ == "__main__":
    # 自检: python binance_testnet.py
    for m in ("spot", "futures"):
        c = TestnetClient(m)
        print(f"=== {m} ready={c.ready} ===")
        if not c.ready:
            print("  [SKIP] 未配置 Key, 跳过")
            continue
        try:
            print("  price =", c.get_price())
            print("  balance =", c.get_balance())
            print("  position =", c.get_position())
        except Exception as e:
            print("  [ERR]", e)
