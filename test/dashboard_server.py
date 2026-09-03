"""ETH v12 实时行情监控后端

架构:
  Binance WS (data-stream.binance.vision)
    └─> K线流 → 指标计算(ROC/VolMA) → v12信号判断
              └─> WebSocket 推送给浏览器前端

启动:
  python dashboard_server.py
  浏览器打开: http://127.0.0.1:8080
"""
import asyncio
import json
import os
import threading
import time
import urllib.request
from contextlib import asynccontextmanager

import numpy as np
import websocket

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ==================== 配置 ====================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "dashboard_static")

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

SYMBOL = "ethusdt"
INTERVAL = "1h"
WS_URL = "wss://data-stream.binance.vision/ws/ethusdt@kline_1h"
REST_KLINE_URL = "https://data-api.binance.vision/api/v3/klines"

HISTORY_BARS = 300          # 历史回补根数
RECONNECT_DELAY = 5
PUSH_INTERVAL = 3           # 向前端推送间隔(秒, 心跳/最新价刷新)

# ---- v12 策略参数 (与 paper_trading.py 一致) ----
CAPITAL = 150.0
LEVERAGE = 3
FRACTION_BASE = 0.3
FEE_RATE = 0.0004
SL_USDT = 5.0
MAX_HOLD_BARS = 72
ROC_SHORT = 8
ROC_MEDIUM = 20
VOL_MA_PERIOD = 20

DRAWDOWN_THRESHOLDS = [
    (0.10, 1.0),
    (0.20, 0.7),
    (0.30, 0.5),
    (1.00, 0.3),
]


# ==================== 指标计算 ====================

def calc_roc(closes, period):
    n = len(closes)
    roc = np.full(n, np.nan)
    for i in range(period, n):
        if closes[i - period] > 0:
            roc[i] = (closes[i] - closes[i - period]) / closes[i - period] * 100
    return roc


def calc_ma(values, period):
    ma = np.full(len(values), np.nan)
    if len(values) < period:
        return ma
    cumsum = np.cumsum(values)
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


def get_proxy_opener():
    proxy = urllib.request.ProxyHandler({
        "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
    })
    return urllib.request.build_opener(proxy)


def fetch_history(limit=HISTORY_BARS, retries=5):
    url = f"{REST_KLINE_URL}?symbol=ETHUSDT&interval={INTERVAL}&limit={limit}"
    last_err = None
    for attempt in range(retries):
        try:
            resp = get_proxy_opener().open(url, timeout=15)
            data = json.loads(resp.read().decode())
            bars = []
            for k in data:
                bars.append({
                    "t": int(k[0]),          # 开盘时间
                    "o": float(k[1]),
                    "h": float(k[2]),
                    "l": float(k[3]),
                    "c": float(k[4]),
                    "v": float(k[5]),
                })
            return bars
        except Exception as e:
            last_err = e
            print(f"  ⚠️ 历史回补重试 {attempt+1}/{retries}: {str(e)[:60]}")
            time.sleep(2)
    raise last_err


# ==================== 市场数据服务 ====================

class MarketService:
    """订阅 Binance WS, 维护K线缓冲, 计算指标和信号"""

    def __init__(self):
        self.bars = []
        self.lock = threading.Lock()
        self.ws = None
        self.running = True
        self.clients = set()          # 连接的浏览器 WebSocket
        self.last_push_time = 0
        self.signal_log = []          # 已触发的买卖信号 (供前端标记)

    # ---- 历史回补 ----
    def init_history(self):
        try:
            bars = fetch_history(HISTORY_BARS)
            with self.lock:
                self.bars = bars
            print(f"  ✅ 历史回补: {len(bars)} 根")
            # 回放历史生成信号 (用于前端初始展示买卖点)
            self._replay_history()
        except Exception as e:
            print(f"  ❌ 历史回补失败: {e}")

    def _replay_history(self):
        """用历史K线回放策略, 生成买卖信号"""
        bars = self.bars
        closes = np.array([b["c"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)

        warmup = max(ROC_MEDIUM, VOL_MA_PERIOD) + 2
        position = None
        signals = []

        for i in range(warmup, len(bars)):
            price = closes[i]
            cur_roc5 = roc5[i]
            cur_roc20 = roc20[i]
            cur_vol = vols[i]
            cur_vol_ma = vol_ma[i]

            if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma):
                continue

            # 出场
            if position is not None:
                pnl_pct = (price - position["entry"]) / position["entry"] if position["dir"] == "long" else (position["entry"] - price) / position["entry"]
                pnl = pnl_pct * position["size"]
                held = i - position["bar"]
                should_close = False
                reason = ""
                if position["dir"] == "long" and cur_roc5 < 0:
                    should_close, reason = True, "momentum_death"
                elif position["dir"] == "short" and cur_roc5 > 0:
                    should_close, reason = True, "momentum_death"
                if pnl <= -SL_USDT:
                    should_close, reason = True, "SL"
                if held >= MAX_HOLD_BARS:
                    should_close, reason = True, "timeout"

                if should_close:
                    signals.append({
                        "t": bars[i]["t"],
                        "price": round(price, 2),
                        "type": "sell",
                        "direction": position["dir"],
                        "reason": reason,
                        "pnl": round(pnl, 2),
                    })
                    position = None

            # 入场
            if position is None and cur_vol > cur_vol_ma:
                if cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20:
                    position = {"dir": "long", "entry": price, "bar": i,
                                "size": CAPITAL * FRACTION_BASE * LEVERAGE}
                    signals.append({
                        "t": bars[i]["t"],
                        "price": round(price, 2),
                        "type": "buy",
                        "direction": "long",
                        "reason": "roc_cross",
                        "pnl": None,
                    })
                elif cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20:
                    position = {"dir": "short", "entry": price, "bar": i,
                                "size": CAPITAL * FRACTION_BASE * LEVERAGE}
                    signals.append({
                        "t": bars[i]["t"],
                        "price": round(price, 2),
                        "type": "buy",
                        "direction": "short",
                        "reason": "roc_cross",
                        "pnl": None,
                    })

        with self.lock:
            self.signal_log = signals
        print(f"  ✅ 历史回放: 生成 {len(signals)} 个信号 (买卖点)")

    # ---- 实时K线 ----
    def on_kline(self, k):
        is_closed = k["x"]
        bar = {
            "t": int(k["t"]),
            "o": float(k["o"]),
            "h": float(k["h"]),
            "l": float(k["l"]),
            "c": float(k["c"]),
            "v": float(k["v"]),
        }
        with self.lock:
            if is_closed:
                if self.bars and self.bars[-1]["t"] == bar["t"]:
                    self.bars[-1] = bar
                else:
                    self.bars.append(bar)
                    if len(self.bars) > HISTORY_BARS:
                        self.bars = self.bars[-HISTORY_BARS:]
                # 检查是否产生新信号
                signals = self._check_signal()
                if signals:
                    self.signal_log.extend(signals)
                    self.signal_log = self.signal_log[-300:]
            else:
                # 未收盘K线: 更新最后一根
                if self.bars and self.bars[-1]["t"] == bar["t"]:
                    self.bars[-1] = bar
                else:
                    # 新K线开始, 但还没收盘 → 追加为进行中K线
                    self.bars.append(bar)

    def _check_signal(self):
        """基于当前缓冲最后一根已收盘K线判断信号"""
        bars = self.bars
        n = len(bars)
        if n < max(ROC_MEDIUM, VOL_MA_PERIOD) + 2:
            return []

        closes = np.array([b["c"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)

        i = n - 1
        price = closes[i]
        cur_roc5 = roc5[i]
        cur_roc20 = roc20[i]
        cur_vol = vols[i]
        cur_vol_ma = vol_ma[i]

        if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma):
            return []

        # 简化: 只检测入场信号 (出场信号由持仓模拟计算, 这里只做展示)
        signals = []
        if cur_vol > cur_vol_ma:
            if cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20:
                signals.append({
                    "t": bars[i]["t"],
                    "price": round(price, 2),
                    "type": "buy",
                    "direction": "long",
                    "reason": "roc_cross",
                    "pnl": None,
                })
            elif cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20:
                signals.append({
                    "t": bars[i]["t"],
                    "price": round(price, 2),
                    "type": "buy",
                    "direction": "short",
                    "reason": "roc_cross",
                    "pnl": None,
                })
        return signals

    # ---- 指标计算 (给前端展示) ----
    def get_chart_data(self):
        """返回前端需要的完整数据: K线 + 指标线 + 信号"""
        with self.lock:
            bars = list(self.bars)
            signals = list(self.signal_log)

        closes = np.array([b["c"] for b in bars])
        vols = np.array([b["v"] for b in bars])

        klines = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]

        # 指标线
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        volma20 = calc_ma(vols, 20)

        # 转换为前端格式: [ts, value], NaN 过滤
        def series(arr):
            out = []
            for i, v in enumerate(arr):
                if not np.isnan(v):
                    out.append([int(bars[i]["t"]), round(float(v), 4)])
            return out

        return {
            "klines": klines,
            "roc5": series(roc5),
            "roc20": series(roc20),
            "vol_ma": series(vol_ma),
            "volma20": series(volma20),
            "signals": signals,
            "last_price": round(float(closes[-1]), 2) if len(closes) else 0,
            "last_ts": int(bars[-1]["t"]) if bars else 0,
            "params": {
                "roc_short": ROC_SHORT,
                "roc_medium": ROC_MEDIUM,
                "vol_ma_period": VOL_MA_PERIOD,
                "sl_usdt": SL_USDT,
                "leverage": LEVERAGE,
                "capital": CAPITAL,
            },
            "server_time": int(time.time() * 1000),
        }

    # ---- WebSocket 事件 (Binance) ----
    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "k" in data:
                self.on_kline(data["k"])
                self._broadcast()
        except Exception as e:
            print(f"  ⚠️ 解析错误: {e}")

    def on_error(self, ws, error):
        print(f"  ❌ WS错误: {error}")

    def on_close(self, ws, code, msg):
        print(f"  🔌 Binance WS关闭 ({code}), {RECONNECT_DELAY}s后重连...")
        if self.running:
            threading.Timer(RECONNECT_DELAY, self.connect_binance).start()

    def connect_binance(self):
        try:
            self.ws = websocket.WebSocketApp(
                WS_URL,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            self.ws.run_forever(
                http_proxy_host=PROXY_HOST,
                http_proxy_port=PROXY_PORT,
                proxy_type="http",
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception as e:
            print(f"  ❌ 连接失败: {e}")
            if self.running:
                threading.Timer(RECONNECT_DELAY, self.connect_binance).start()

    # ---- 推送给浏览器 ----
    def _broadcast(self):
        """K线更新时推送(节流)。从 Binance WS 线程调用, 需通过事件循环发送"""
        now = time.time()
        if now - self.last_push_time < PUSH_INTERVAL:
            return
        self.last_push_time = now
        if not self.clients:
            return
        data = self.get_chart_data()
        msg = json.dumps({"type": "update", "data": data}, ensure_ascii=False)

        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        # 跨线程安全发送: 调度到事件循环执行
        for client in list(self.clients):
            try:
                asyncio.run_coroutine_threadsafe(client.send_text(msg), loop)
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self.connect_binance, daemon=True).start()


# ==================== FastAPI ====================

market = MarketService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 启动 ETH v12 实时行情面板...")
    market._loop = asyncio.get_running_loop()   # 保存事件循环供跨线程推送
    market.init_history()
    market.start()
    print("   🌐 http://127.0.0.1:8080")
    yield
    market.running = False
    if market.ws:
        market.ws.close()


app = FastAPI(title="ETH v12 实时行情面板", lifespan=lifespan)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/data")
async def api_data():
    return JSONResponse(market.get_chart_data())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    market.clients.add(ws)
    print(f"  🔗 浏览器连接 ({len(market.clients)} 个客户端)")
    # 先推送初始数据
    await ws.send_text(json.dumps({
        "type": "init",
        "data": market.get_chart_data(),
    }, ensure_ascii=False))
    try:
        while True:
            # 收到 ping 则回应 (保持连接)
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        market.clients.discard(ws)
        print(f"  🔌 浏览器断开 ({len(market.clients)} 个客户端)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="info")
