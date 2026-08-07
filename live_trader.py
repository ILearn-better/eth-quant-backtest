"""ETH v12 双ROC动量策略 — 实时模拟交易 + 信号推送

架构:
  Binance WS (stream.binance.com:9443)
    ├─ 1h K线 → 指标计算(ROC/VolMA) → v12信号 → 终端输出 + 浏览器推送
    └─ Ticker  → 实时价格

前端:
  http://127.0.0.1:8080 — 实时K线图 + 信号面板 + 指标状态

启动:
  python live_trader.py
"""
import asyncio
import json
import os
import sys
import threading
import time
import datetime
import urllib.request
import urllib.error
import urllib.parse
import ssl

# Windows 终端 UTF-8 兼容
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from contextlib import asynccontextmanager

import numpy as np
import websocket

# ==================== 配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "dashboard_static")

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

SYMBOL = "ethusdt"
INTERVAL = "1h"

# K线 WS (通过代理)
WS_KLINE_URL = "wss://stream.binance.com:9443/ws/ethusdt@kline_1h"
# 实时价格 WS (1s 更新)
WS_TICKER_URL = "wss://stream.binance.com:9443/ws/ethusdt@ticker"
# REST API (直连)
REST_KLINE_URL = "https://api.binance.com/api/v3/klines"
REST_TICKER_URL = "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT"

HISTORY_BARS = 300
RECONNECT_DELAY = 5
PUSH_INTERVAL = 3           # 定时广播间隔(秒), 不依赖 K线收盘

# ---- v12 策略参数 ----
CAPITAL = 150.0
LEVERAGE = 3
FRACTION_BASE = 0.3
FEE_RATE = 0.0004
SL_USDT = 5.0
MAX_HOLD_BARS = 72
ROC_SHORT = 8
ROC_MEDIUM = 20
VOL_MA_PERIOD = 20

# ---- 微信通知 (Server酱) ----
# 获取 SendKey: https://sct.ftqq.com/ 登录后可见
# 设置环境变量: set WX_SENDKEY=SCTxxxxx  或直接在下面填入
WX_SENDKEY = os.environ.get("WX_SENDKEY", "SCT391359TGd8xzPIRZUFTAfvUQOr4OH9D")

def wx_notify(title, content):
    """通过 Server酱 发送微信消息"""
    if not WX_SENDKEY:
        return
    try:
        url = f"https://sctapi.ftqq.com/{WX_SENDKEY}.send"
        data = urllib.parse.urlencode({
            "title": title,
            "desp": content,
        }).encode("utf-8")
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=10, context=ctx)
    except Exception as e:
        print(f"  ⚠️ 微信通知发送失败: {e}")

DRAWDOWN_THRESHOLDS = [
    (0.10, 1.0), (0.20, 0.7), (0.30, 0.5), (1.00, 0.3),
]


# ==================== 工具函数 ====================
def get_proxy_opener():
    proxy = urllib.request.ProxyHandler({
        "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
    })
    return urllib.request.build_opener(proxy)


def ts_to_str(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def ts_to_short(ts_ms):
    return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")


def now():
    return datetime.datetime.now().strftime("%H:%M:%S")


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


# ==================== 数据服务 ====================
class LiveTrader:
    """实时交易引擎: 数据订阅 + 指标计算 + 信号检测"""

    def __init__(self):
        self.bars = []           # 1h K线缓冲
        self.last_price = 0      # 实时价格
        self.lock = threading.Lock()
        self.ws_kline = None
        self.ws_ticker = None
        self.running = True

        # 信号状态
        self.signal_log = []         # 历史信号
        self.current_signal = None   # 当前活跃信号方向
        self.indicator_state = {}    # 当前指标值
        self.signal_readiness = {}   # 入场条件满足情况

    # ---- REST: 历史K线回补 ----
    def _rest_get(self, url, timeout=15):
        """REST请求: 直连优先, 失败走代理, 全失败返回 None"""
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        # 直连
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            pass
        # 走代理
        try:
            opener = get_proxy_opener()
            with opener.open(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"  ⚠️ [{now()}] REST 请求失败(直连+代理均失败): {e}")
            return None

    def fetch_history(self):
        print(f"  📥 [{now()}] 拉取历史 {HISTORY_BARS} 根 1h K线...")
        url = f"{REST_KLINE_URL}?symbol=ETHUSDT&interval=1h&limit={HISTORY_BARS}"
        data = None
        for attempt in range(3):
            data = self._rest_get(url)
            if data:
                break
            print(f"  ⚠️ [{now()}] 历史回补重试 {attempt+1}/3...")
            time.sleep(2)
        if not data:
            print(f"  ❌ [{now()}] 历史回补失败, 以空缓冲启动 (WS 连上后会自动补充)")
            return
        bars = []
        for k in data:
            bars.append({
                "t": int(k[0]),  "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
            })
        with self.lock:
            self.bars = bars
        print(f"  ✅ [{now()}] 回补完成: {len(bars)} 根, "
              f"最新 {ts_to_short(bars[-1]['t'])} close={bars[-1]['c']:.2f}")

    def fetch_current_price(self):
        """获取当前价格"""
        try:
            data = self._rest_get(REST_TICKER_URL, timeout=10)
            return float(data["price"])
        except Exception:
            return 0

    # ---- 指标计算 ----
    def compute_indicators(self):
        with self.lock:
            bars = list(self.bars)
        closes = np.array([b["c"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        return closes, vols, roc5, roc20, vol_ma

    # ---- 信号检测 ----
    def check_signal(self):
        """基于最新收盘K线检测入场信号"""
        closes, vols, roc5, roc20, vol_ma = self.compute_indicators()
        i = len(closes) - 1
        if i < max(ROC_MEDIUM, VOL_MA_PERIOD) + 2:
            return None

        cur_roc5 = roc5[i]
        cur_roc20 = roc20[i]
        cur_vol = vols[i]
        cur_vol_ma = vol_ma[i]
        price = closes[i]
        ts = self.bars[i]["t"]

        if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma):
            return None

        # 更新指标状态
        self.indicator_state = {
            "price": round(float(price), 2),
            "roc5": round(float(cur_roc5), 4),
            "roc20": round(float(cur_roc20), 4),
            "volume": round(float(cur_vol), 2),
            "vol_ma": round(float(cur_vol_ma), 2),
            "vol_ratio": round(float(cur_vol / cur_vol_ma), 2) if cur_vol_ma > 0 else 0,
            "ts": int(ts),
        }

        # 入场条件检查
        vol_ok = cur_vol > cur_vol_ma
        long_roc = cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20
        short_roc = cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20

        self.signal_readiness = {
            "vol_confirmed": bool(vol_ok),
            "long_ready": bool(vol_ok and long_roc),
            "short_ready": bool(vol_ok and short_roc),
            "neutral": bool(not long_roc and not short_roc),
        }

        signal = None
        if vol_ok:
            if long_roc:
                signal = {"type": "BUY", "direction": "long", "price": round(float(price), 2),
                          "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                          "roc20": round(float(cur_roc20), 2)}
                self.current_signal = "long"
            elif short_roc:
                signal = {"type": "SELL", "direction": "short", "price": round(float(price), 2),
                          "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                          "roc20": round(float(cur_roc20), 2)}
                self.current_signal = "short"

        if signal:
            self.signal_log.append(signal)
            self.signal_log = self.signal_log[-50:]

        return signal

    # ---- WebSocket 消息处理 ----
    def on_kline_message(self, ws, message):
        try:
            data = json.loads(message)
            if "k" not in data:
                return
            k = data["k"]
            is_closed = k["x"]
            bar = {
                "t": int(k["t"]),     "o": float(k["o"]), "h": float(k["h"]),
                "l": float(k["l"]),   "c": float(k["c"]), "v": float(k["v"]),
            }

            with self.lock:
                if is_closed:
                    if self.bars and self.bars[-1]["t"] == bar["t"]:
                        self.bars[-1] = bar
                    else:
                        self.bars.append(bar)
                        if len(self.bars) > HISTORY_BARS:
                            self.bars = self.bars[-HISTORY_BARS:]
                else:
                    # 未收盘K线 → 更新最新一根
                    if self.bars and self.bars[-1]["t"] == bar["t"]:
                        self.bars[-1] = bar
                    else:
                        self.bars.append(bar)

            # ⚠️ 信号检测必须在锁外执行: check_signal() → compute_indicators()
            #    内部会再次 with self.lock, 而 threading.Lock 不可重入,
            #    若在锁内调用会导致同线程死锁 (K线收盘即卡死, HTTP/广播全部阻塞)
            if is_closed:
                signal = self.check_signal()
                self._print_status(bar)
                if signal:
                    self._print_signal(signal)
        except Exception as e:
            print(f"  ⚠️ [{now()}] K线解析错误: {e}")

    def on_ticker_message(self, ws, message):
        try:
            data = json.loads(message)
            self.last_price = float(data["c"])
        except Exception:
            pass

    def _print_status(self, bar):
        """每根K线收盘打印状态"""
        t = ts_to_short(bar["t"])
        st = self.indicator_state
        rdy = self.signal_readiness

        vol_tag = "🔥放量" if rdy.get("vol_confirmed") else "📉缩量"
        if rdy.get("long_ready"):
            signal_tag = "🟢 做多信号就绪"
        elif rdy.get("short_ready"):
            signal_tag = "🔴 做空信号就绪"
        else:
            signal_tag = "⚪ 观望"

        print(f"\n{'─'*65}")
        print(f"  📊 [{now()}] K线收盘 {t} | close={bar['c']:.2f}")
        print(f"  📈 ROC({ROC_SHORT})={st.get('roc5', '?')}  ROC({ROC_MEDIUM})={st.get('roc20', '?')}  |  "
              f"Vol={st.get('volume', '?')} / MA={st.get('vol_ma', '?')} ({vol_tag})")
        print(f"  🎯 {signal_tag}")
        print(f"{'─'*65}")

    def _print_signal(self, signal):
        """信号触发时醒目输出"""
        direction = signal["direction"]
        sig_type = signal["type"]
        if direction == "long":
            border = "🟢" * 20
            emoji = "📈"
            label = "做多"
        else:
            border = "🔴" * 20
            emoji = "📉"
            label = "做空"

        print(f"\n{border}")
        print(f"  {emoji} {emoji} {emoji}  *** {sig_type} 信号触发 — {label} ***  {emoji} {emoji} {emoji}")
        print(f"  ═══════════════════════════════════════════════════════")
        print(f"  价格: {signal['price']} USDT")
        print(f"  ROC({ROC_SHORT}): {signal['roc5']}%  |  ROC({ROC_MEDIUM}): {signal['roc20']}%")
        print(f"  时间: {ts_to_str(signal['ts'])}")
        print(f"  建议仓位: {FRACTION_BASE*100:.0f}% 本金 × {LEVERAGE}x 杠杆")
        print(f"  止损: {SL_USDT} USDT  |  最大持仓: {MAX_HOLD_BARS}根K线")
        print(f"  ═══════════════════════════════════════════════════════")
        print(f"{border}\n")

    # ---- WebSocket 事件 ----
    def on_error(self, ws, error, name="K线"):
        print(f"  ❌ [{now()}] {name} WS错误: {error}")

    def on_close(self, ws, code, msg, name="K线"):
        print(f"  🔌 [{now()}] {name} WS断开 ({code}), {RECONNECT_DELAY}s后重连...")

    def _ws_connect(self, url, on_msg, name):
        """带 SSL 绕过 + 代理 + 指数退避的 WebSocket 连接"""
        sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        retries = 0
        while self.running:
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=on_msg,
                    on_error=lambda w, e: self.on_error(w, e, name),
                    on_close=lambda w, c, m: self.on_close(w, c, m, name),
                )
                setattr(self, f"ws_{name.lower()}", ws)
                ws.run_forever(
                    http_proxy_host=PROXY_HOST, http_proxy_port=PROXY_PORT,
                    proxy_type="http", ping_interval=20, ping_timeout=10,
                    sslopt=sslopt,
                )
                retries = 0  # 成功运行后重置
            except Exception as e:
                print(f"  [ERR] [{now()}] {name} connect failed: {e}")
            if self.running:
                retries += 1
                delay = min(RECONNECT_DELAY * (1.5 ** retries), 60)
                time.sleep(delay)

    def connect_kline(self):
        self._ws_connect(WS_KLINE_URL, self.on_kline_message, "Kline")

    def connect_ticker(self):
        self._ws_connect(WS_TICKER_URL, self.on_ticker_message, "Ticker")

    def start(self):
        print(f"{'='*65}")
        print(f"  🎯 ETH v12 双ROC动量策略 — 实时模拟交易")
        print(f"  策略: ROC({ROC_SHORT})/ROC({ROC_MEDIUM}) + VolMA({VOL_MA_PERIOD})")
        print(f"  做多: ROC{ROC_SHORT}>0 & ROC{ROC_MEDIUM}>0 & ROC{ROC_SHORT}>ROC{ROC_MEDIUM} & 放量")
        print(f"  做空: ROC{ROC_SHORT}<0 & ROC{ROC_MEDIUM}<0 & ROC{ROC_SHORT}<ROC{ROC_MEDIUM} & 放量")
        print(f"  本金: {CAPITAL}U | 杠杆: {LEVERAGE}x | 止损: {SL_USDT}U")
        print(f"  数据源: {WS_KLINE_URL}")
        print(f"{'='*65}")

        # 历史回补
        self.fetch_history()
        # 初始信号检测
        signal = self.check_signal()
        if self.bars:
            self._print_status(self.bars[-1])
        if signal:
            self._print_signal(signal)

        # 启动 WS 线程
        threading.Thread(target=self.connect_kline, daemon=True, name="kline-ws").start()
        threading.Thread(target=self.connect_ticker, daemon=True, name="ticker-ws").start()

        # 定时打印实时价格
        threading.Thread(target=self._price_reporter, daemon=True, name="price-reporter").start()

    def _price_reporter(self):
        """每30秒报告实时价格"""
        last_report = 0
        while self.running:
            time.sleep(10)
            now_ts = time.time()
            if now_ts - last_report >= 30 and self.last_price > 0:
                rdy = self.signal_readiness
                long_tag = "✅" if rdy.get("long_ready") else "○"
                short_tag = "✅" if rdy.get("short_ready") else "○"
                print(f"  💰 [{now()}] ETH={self.last_price:.2f}  |  做多 {long_tag}  做空 {short_tag}")
                last_report = now_ts

    # ---- 提供给前端的图表数据 ----
    def get_chart_data(self):
        with self.lock:
            bars = list(self.bars)
            signals = list(self.signal_log)

        closes = np.array([b["c"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)

        klines = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]

        def series(arr):
            # 返回与 bars 等长的数组, NaN 位置用 None(null) 填充,
            # 使 ECharts category 轴按索引对齐 (否则过滤NaN后数组变短,
            # 曲线会整体左移 period 位, 末端缺失 → ROC(20)看似只到前一天的错觉)
            out = []
            for v in arr:
                if np.isnan(v):
                    out.append(None)
                else:
                    out.append(round(float(v), 4))
            return out

        # 实时指标: 用最新 bar 重新计算, 不依赖 K线收盘时的缓存
        # (未收盘 K线的 close 被 on_kline_message 持续更新, 指标应跟随实时变化)
        if len(closes) and not np.isnan(roc5[-1]) and not np.isnan(vol_ma[-1]):
            _price = round(float(closes[-1]), 2)
            _vol, _volma = float(vols[-1]), float(vol_ma[-1])
            _roc5, _roc20 = float(roc5[-1]), float(roc20[-1])
            indicator_state = {
                "price": _price,
                "roc5": round(_roc5, 4),
                "roc20": round(_roc20, 4),
                "volume": round(_vol, 2),
                "vol_ma": round(_volma, 2),
                "vol_ratio": round(_vol / _volma, 2) if _volma > 0 else 0,
                "ts": int(bars[-1]["t"]),
            }
            vol_ok = _vol > _volma
            long_roc = _roc5 > 0 and _roc20 > 0 and _roc5 > _roc20
            short_roc = _roc5 < 0 and _roc20 < 0 and _roc5 < _roc20
            signal_readiness = {
                "vol_confirmed": bool(vol_ok),
                "long_ready": bool(vol_ok and long_roc),
                "short_ready": bool(vol_ok and short_roc),
                "neutral": bool(not long_roc and not short_roc),
            }
        else:
            indicator_state = self.indicator_state
            signal_readiness = self.signal_readiness

        return {
            "klines": klines,
            "roc5": series(roc5),
            "roc20": series(roc20),
            "vol_ma": series(vol_ma),
            "volma20": series(calc_ma(vols, 20)),
            "signals": signals,
            "last_price": self.last_price or (round(float(closes[-1]), 2) if len(closes) else 0),
            "last_ts": int(bars[-1]["t"]) if bars else 0,
            "indicator_state": indicator_state,
            "signal_readiness": signal_readiness,
            "params": {
                "roc_short": ROC_SHORT, "roc_medium": ROC_MEDIUM,
                "vol_ma_period": VOL_MA_PERIOD, "sl_usdt": SL_USDT,
                "leverage": LEVERAGE, "capital": CAPITAL,
            },
            "server_time": int(time.time() * 1000),
        }

    def stop(self):
        self.running = False


# ==================== Web 面板 ====================
trader = LiveTrader()

HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETH v12 实时交易信号面板</title>
<script src="/static/echarts.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;overflow:hidden}
.header{background:#161b22;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #30363d;gap:12px}
.header h1{font-size:18px;color:#58a6ff}
.header .nav{display:flex;gap:8px;align-items:center}
.header .nav a{color:#8b949e;font-size:13px;text-decoration:none;padding:4px 10px;border-radius:4px;border:1px solid #30363d}
.header .nav a.active{color:#f7931a;border-color:#f7931a;background:#f7931a22}
.header .nav a:hover{color:#58a6ff;border-color:#58a6ff}
.price-display{font-size:28px;font-weight:bold;color:#f0f6fc}
.price-display .change{font-size:14px;margin-left:10px}
.layout{display:flex;height:calc(100vh - 50px)}
.chart-area{flex:1;min-width:0}
#kline-chart{width:100%;height:60%}
#roc-chart{width:100%;height:40%}
.sidebar{width:340px;background:#161b22;border-left:2px solid #30363d;padding:15px;overflow-y:auto}
.section{margin-bottom:18px}
.section h3{font-size:13px;color:#8b949e;text-transform:uppercase;margin-bottom:8px;border-bottom:1px solid #30363d;padding-bottom:5px}
.indicator-row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px}
.indicator-row .label{color:#8b949e}
.indicator-row .value{font-weight:bold}
.condition{display:flex;align-items:center;padding:6px 10px;margin:4px 0;border-radius:4px;font-size:12px}
.condition.met{background:#1a3a1a;color:#3fb950}
.condition.not-met{background:#3a1a1a;color:#f85149}
.condition .icon{margin-right:8px;font-size:16px}
.signal-alert{padding:12px;border-radius:6px;margin:8px 0;font-weight:bold;font-size:14px;text-align:center}
.signal-alert.buy{background:#1a3a1a;border:2px solid #3fb950;color:#3fb950}
.signal-alert.sell{background:#3a1a1a;border:2px solid #f85149;color:#f85149}
.signal-alert.neutral{background:#1a1a2e;border:2px solid #30363d;color:#8b949e}
.signal-list{max-height:250px;overflow-y:auto}
.signal-item{display:flex;align-items:center;padding:6px 8px;margin:3px 0;border-radius:3px;font-size:12px;background:#21262d}
.signal-item.buy-signal{border-left:3px solid #3fb950}
.signal-item.sell-signal{border-left:3px solid #f85149}
.signal-item .dir{font-weight:bold;width:35px}
.signal-item .price{flex:1;text-align:right}
.signal-item .time{color:#8b949e;font-size:11px;margin-left:8px}
.param-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px}
.param-item{background:#21262d;padding:5px 8px;border-radius:3px}
.param-item .p-label{color:#8b949e}
.param-item .p-val{font-weight:bold;color:#58a6ff}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
</style>
</head>
<body>

<div class="header">
  <h1><span class="live-dot"></span>ETH v12 双ROC动量策略 — 实时信号</h1>
  <div class="nav">
    <a href="http://127.0.0.1:8080" class="active">现货</a>
    <a href="http://127.0.0.1:8081">合约</a>
  </div>
  <div class="price-display" id="price-display">--</div>
</div>

<div class="layout">
  <div class="chart-area">
    <div id="kline-chart"></div>
    <div id="roc-chart"></div>
  </div>
  <div class="sidebar">
    <!-- 信号警报 -->
    <div class="section">
      <h3>🚨 实时信号</h3>
      <div class="signal-alert neutral" id="signal-alert">⚪ 等待信号...</div>
    </div>

    <!-- 入场条件 -->
    <div class="section">
      <h3>📋 入场条件检查</h3>
      <div class="condition not-met" id="cond-vol">
        <span class="icon">⬜</span> 成交量 > VolMA(20)
      </div>
      <div class="condition not-met" id="cond-long">
        <span class="icon">⬜</span> ROC(8)>0 & ROC(20)>0 & ROC(8)>ROC(20)
      </div>
      <div class="condition not-met" id="cond-short">
        <span class="icon">⬜</span> ROC(8)<0 & ROC(20)<0 & ROC(8)<ROC(20)
      </div>
    </div>

    <!-- 实时指标 -->
    <div class="section">
      <h3>📊 当前指标</h3>
      <div class="indicator-row"><span class="label">ROC(8)</span><span class="value" id="ind-roc5">--</span></div>
      <div class="indicator-row"><span class="label">ROC(20)</span><span class="value" id="ind-roc20">--</span></div>
      <div class="indicator-row"><span class="label">成交量</span><span class="value" id="ind-vol">--</span></div>
      <div class="indicator-row"><span class="label">VolMA(20)</span><span class="value" id="ind-volma">--</span></div>
      <div class="indicator-row"><span class="label">量比</span><span class="value" id="ind-volratio">--</span></div>
    </div>

    <!-- 策略参数 -->
    <div class="section">
      <h3>⚙️ 策略参数</h3>
      <div class="param-grid">
        <div class="param-item"><span class="p-label">本金</span><br><span class="p-val">150 USDT</span></div>
        <div class="param-item"><span class="p-label">杠杆</span><br><span class="p-val">3x</span></div>
        <div class="param-item"><span class="p-label">仓位</span><br><span class="p-val">30%</span></div>
        <div class="param-item"><span class="p-label">止损</span><br><span class="p-val">5 USDT</span></div>
        <div class="param-item"><span class="p-label">ROC短</span><br><span class="p-val">8根</span></div>
        <div class="param-item"><span class="p-label">ROC中</span><br><span class="p-val">20根</span></div>
        <div class="param-item"><span class="p-label">VolMA</span><br><span class="p-val">20根</span></div>
        <div class="param-item"><span class="p-label">最大持仓</span><br><span class="p-val">72根</span></div>
      </div>
    </div>

    <!-- 历史信号 -->
    <div class="section">
      <h3>📜 最近信号</h3>
      <div class="signal-list" id="signal-list"></div>
    </div>
  </div>
</div>

<script>
let klineChart, rocChart;
let currentSignals = [];

function initKlineChart() {
  klineChart = echarts.init(document.getElementById('kline-chart'));
  rocChart = echarts.init(document.getElementById('roc-chart'));
}

function updateCharts(data) {
  // K线图
  const dates = data.klines.map(k => {
    const d = new Date(k[0]);
    return d.toLocaleDateString('zh-CN', {month:'short',day:'numeric'}) + ' ' +
           d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
  });
  const kdata = data.klines.map(k => [k[1], k[4], k[3], k[2]]); // [open, close, low, high] ECharts candlestick 格式
  const volData = data.klines.map((k, i) => [i, k[5], k[1] <= k[4] ? 1 : -1]);

  // 信号标记
  const buyMarks = [], sellMarks = [];
  data.signals.forEach(s => {
    const idx = data.klines.findIndex(k => k[0] === s.ts);
    if (idx >= 0) {
      if (s.type === 'BUY') {
        buyMarks.push({coord: [dates[idx], data.klines[idx][3]], value: s.price});
      } else {
        sellMarks.push({coord: [dates[idx], data.klines[idx][2]], value: s.price});
      }
    }
  });

  // 成交量MA
  const volMaData = data.volma20;

  klineChart.setOption({
    grid: [{left:'8%',right:'3%',top:'5%',height:'55%'},
           {left:'8%',right:'3%',top:'68%',height:'25%'}],
    xAxis: [{type:'category',data:dates,gridIndex:0,axisLabel:{show:false}},
            {type:'category',data:dates,gridIndex:1,axisLabel:{fontSize:10}}],
    yAxis: [{type:'value',gridIndex:0,scale:true,splitArea:{show:true}},
            {type:'value',gridIndex:1}],
    series: [
      {name:'K线',type:'candlestick',data:kdata,xAxisIndex:0,yAxisIndex:0,
       itemStyle:{color:'#3fb950',color0:'#f85149',borderColor:'#3fb950',borderColor0:'#f85149'}},
      {name:'买点',type:'scatter',data:buyMarks,xAxisIndex:0,yAxisIndex:0,
       symbol:'triangle',symbolSize:12,itemStyle:{color:'#3fb950'}},
      {name:'卖点',type:'scatter',data:sellMarks,xAxisIndex:0,yAxisIndex:0,
       symbol:'triangle',symbolSize:12,symbolRotate:180,itemStyle:{color:'#f85149'}},
      {name:'量',type:'bar',data:volData,xAxisIndex:1,yAxisIndex:1,
       itemStyle:{color:params=>params.data[2]>0?'#3fb950':'#f85149'}},
      {name:'VolMA20',type:'line',data:volMaData,xAxisIndex:1,yAxisIndex:1,
       lineStyle:{color:'#f0883e',width:1},symbol:'none'},
    ],
    tooltip:{trigger:'axis'},
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:60,end:100}],
  });

  // ROC图
  const roc5Data = data.roc5;
  const roc20Data = data.roc20;
  rocChart.setOption({
    grid:{left:'8%',right:'3%',top:'5%',bottom:'10%'},
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:10}},
    yAxis:{type:'value',name:'ROC %'},
    series:[
      {name:'ROC(8)',type:'line',data:roc5Data,symbol:'none',lineStyle:{color:'#58a6ff',width:1.5}},
      {name:'ROC(20)',type:'line',data:roc20Data,symbol:'none',lineStyle:{color:'#f0883e',width:1.5}},
    ],
    tooltip:{trigger:'axis'},
    dataZoom:[{type:'inside',start:60,end:100}],
  });
}

function updatePanel(data) {
  // 价格
  const price = data.last_price;
  document.getElementById('price-display').innerHTML = `$${price.toFixed(2)}`;

  // 指标
  const s = data.indicator_state || {};
  document.getElementById('ind-roc5').textContent = (s.roc5||0).toFixed(4) + '%';
  document.getElementById('ind-roc20').textContent = (s.roc20||0).toFixed(4) + '%';
  document.getElementById('ind-vol').textContent = (s.volume||0).toFixed(1);
  document.getElementById('ind-volma').textContent = (s.vol_ma||0).toFixed(1);
  document.getElementById('ind-volratio').textContent = (s.vol_ratio||0).toFixed(2) + 'x';

  // 信号警报
  const rdy = data.signal_readiness || {};
  const alertDiv = document.getElementById('signal-alert');
  alertDiv.className = 'signal-alert';
  if (rdy.long_ready) {
    alertDiv.className += ' buy';
    alertDiv.innerHTML = '📈 <b>做多信号就绪！</b><br>ROC(8)>0 & ROC(20)>0 & 放量';
  } else if (rdy.short_ready) {
    alertDiv.className += ' sell';
    alertDiv.innerHTML = '📉 <b>做空信号就绪！</b><br>ROC(8)<0 & ROC(20)<0 & 放量';
  } else {
    alertDiv.className += ' neutral';
    alertDiv.innerHTML = '⚪ 等待入场信号<br><small>条件未全部满足</small>';
  }

  // 条件检查
  updateCondition('cond-vol', rdy.vol_confirmed, '成交量 > VolMA(20)');
  updateCondition('cond-long', rdy.long_ready, 'ROC(8)>0 & ROC(20)>0 & ROC(8)>ROC(20)');
  updateCondition('cond-short', rdy.short_ready, 'ROC(8)<0 & ROC(20)<0 & ROC(8)<ROC(20)');

  // 历史信号列表
  const listDiv = document.getElementById('signal-list');
  const signals = data.signals || [];
  if (signals.length !== currentSignals.length) {
    currentSignals = signals;
    listDiv.innerHTML = signals.slice().reverse().map(s => {
      const cls = s.type === 'BUY' ? 'buy-signal' : 'sell-signal';
      const dir = s.type === 'BUY' ? '📈多' : '📉空';
      const d = new Date(s.ts);
      const t = d.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      return `<div class="signal-item ${cls}">
        <span class="dir">${dir}</span>
        <span class="price">$${s.price.toFixed(2)}</span>
        <span>ROC(${s.roc5}/${s.roc20})</span>
        <span class="time">${t}</span>
      </div>`;
    }).join('');
  }
}

function updateCondition(id, met, text) {
  const el = document.getElementById(id);
  if (met) {
    el.className = 'condition met';
    el.querySelector('.icon').textContent = '✅';
  } else {
    el.className = 'condition not-met';
    el.querySelector('.icon').textContent = '❌';
  }
}

// WebSocket 连接
let pingTimer = null;
function connectWS() {
  const ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'init' || msg.type === 'update' || msg.type === 'pong') {
      updateCharts(msg.data);
      updatePanel(msg.data);
    }
  };
  ws.onopen = () => {
    // 每 10 秒发 ping 保活, 后端回 pong + 最新数据
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 10000);
  };
  ws.onclose = () => {
    if (pingTimer) clearInterval(pingTimer);
    setTimeout(connectWS, 3000);
  };
}

window.onload = () => {
  initKlineChart();
  connectWS();
  window.onresize = () => { klineChart?.resize(); rocChart?.resize(); };
};
</script>
</body>
</html>'''


# ==================== FastAPI ====================
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

clients = set()
_last_push = 0


def _heartbeat_pusher():
    """定时广播线程: 每 PUSH_INTERVAL 秒推送一次, 不依赖 K线收盘"""
    while trader.running:
        time.sleep(PUSH_INTERVAL)
        broadcast()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "="*65)
    print("  🌐 启动 Web 面板: http://127.0.0.1:8080")
    print("="*65 + "\n")
    trader._loop = asyncio.get_running_loop()
    try:
        trader.start()
    except Exception as e:
        print(f"  ⚠️ [{now()}] trader.start() 异常: {e}")
        print(f"     服务仍启动, 数据源恢复后 WS 会自动重连补数据")
    threading.Thread(target=_heartbeat_pusher, daemon=True, name="heartbeat-push").start()
    yield
    trader.stop()


app = FastAPI(title="ETH v12 实时交易信号", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.get("/api/data")
async def api_data():
    return JSONResponse(trader.get_chart_data())


@app.get("/health")
async def health():
    """健康检查 + 内部状态, 方便排查'访问不到'类问题"""
    with trader.lock:
        bars_n = len(trader.bars)
        last_bar_ts = trader.bars[-1]["t"] if trader.bars else 0
    return JSONResponse({
        "status": "ok",
        "bars": bars_n,
        "last_bar_ts": int(last_bar_ts),
        "last_bar_time": ts_to_str(last_bar_ts) if last_bar_ts else None,
        "last_price": trader.last_price,
        "ws_clients": len(clients),
        "kline_ws_alive": trader.ws_kline is not None,
        "ticker_ws_alive": trader.ws_ticker is not None,
        "signal_log_len": len(trader.signal_log),
        "server_time": now(),
    })


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    print(f"  🔗 浏览器连接 ({len(clients)} 个客户端)")
    await ws.send_text(json.dumps({"type": "init", "data": trader.get_chart_data()}, ensure_ascii=False))
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong", "data": trader.get_chart_data()}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


def broadcast():
    """从 WS 线程推送数据到浏览器"""
    global _last_push
    now = time.time()
    if now - _last_push < 1:
        return
    _last_push = now
    if not clients:
        return
    loop = getattr(trader, "_loop", None)
    if loop is None or loop.is_closed():
        return
    data = trader.get_chart_data()
    msg = json.dumps({"type": "update", "data": data}, ensure_ascii=False)
    for c in list(clients):
        try:
            asyncio.run_coroutine_threadsafe(c.send_text(msg), loop)
        except Exception:
            pass


# 猴子补丁: trader 的 _print_status/_print_signal 中触发 broadcast
_orig_print_status = trader._print_status
def _print_status_with_broadcast(bar):
    _orig_print_status(bar)
    broadcast()
trader._print_status = _print_status_with_broadcast

_orig_print_signal = trader._print_signal
def _print_signal_with_broadcast(signal):
    _orig_print_signal(signal)
    broadcast()
    # 微信通知 — 异步发送, 不阻塞 WS 线程(避免持锁卡住 get_chart_data)
    direction = signal["direction"]
    sig_type = signal["type"]
    label = "🟢 做多" if direction == "long" else "🔴 做空"
    title = f"{label}信号触发 | ETH={signal['price']} USDT"
    content = (
        f"## {sig_type} 信号 — {label}\n\n"
        f"- **价格**: {signal['price']} USDT\n"
        f"- **ROC(8)**: {signal['roc5']}%\n"
        f"- **ROC(20)**: {signal['roc20']}%\n"
        f"- **时间**: {ts_to_str(signal['ts'])}\n"
        f"- **建议仓位**: {FRACTION_BASE*100:.0f}% × {LEVERAGE}x\n"
        f"- **止损**: {SL_USDT} USDT\n"
        f"\n> ETH v12 双ROC动量策略 · 实时信号"
    )
    threading.Thread(target=wx_notify, args=(title, content), daemon=True).start()
trader._print_signal = _print_signal_with_broadcast


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
