"""ETH v12 双ROC动量策略 — 合约(USDⓈ-M Futures)实时模拟交易 + 信号推送

与现货 live_trader.py 独立, 数据源来自 datasource.FUTURES (fapi.binance.com)。
换数据源只需修改 datasource.py, 无需改动本文件。

架构:
  Binance Futures WS (fstream.binance.com)
    ├─ 1h K线 → 指标计算(ROC/VolMA) → v12信号 → 终端输出 + 浏览器推送
    └─ Ticker  → 实时价格

前端:
  http://127.0.0.1:8081 — 合约实时K线图 + 信号面板 + 指标状态

启动:
  python live_trader_contract.py
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

from datasource import FUTURES  # 合约数据源 (fapi.binance.com / fstream.binance.com)

# ==================== 配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "dashboard_static")

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

SYMBOL = "ethusdt"
INTERVAL = "1h"
MARKET_NAME = FUTURES["name"]   # "合约"

# 数据源 — 来自 datasource.FUTURES, 换数据源只改 datasource.py
WS_KLINE_URL = FUTURES["ws_kline"]
WS_TICKER_URL = FUTURES["ws_ticker"]
REST_KLINE_URL = FUTURES["rest_kline"]
REST_TICKER_URL = FUTURES["rest_ticker"]

# 服务端口 (与现货 8080 区分, 避免冲突)
SERVER_PORT = 8081

HISTORY_BARS = 300
RECONNECT_DELAY = 5
PUSH_INTERVAL = 3           # 定时广播间隔(秒), 不依赖 K线收盘

# ---- 合约策略参数 (经验值, 跳过优化; 与 strategies/eth_roc_momentum_contract.py 一致) ----
CAPITAL = 150.0
LEVERAGE = 8
FRACTION_BASE = 0.25
FEE_RATE = 0.0004
MAX_HOLD_BARS = 72
ROC_SHORT = 8
ROC_MEDIUM = 20
VOL_MA_PERIOD = 20

# 动量衰竭阈值: 0=穿零即出 (原0.8经验值导致利润回吐太重, 改回v12逻辑)
MOMENTUM_DEATH_THRESH = 0

# ATR 自适应止盈止损
ATR_PERIOD = 14
SL_ATR_MULT = 1.5         # 止损 = 1.5 × ATR
TP_ATR_MULT = 999         # 关闭止盈 (设极大值=关闭; 让趋势跑完, 靠动量出场更优)

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


def calc_atr(highs, lows, closes, period):
    """计算 ATR (Average True Range) — 用于动态止盈止损

    TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR = TR 的简单移动平均
    """
    n = len(closes)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    atr = np.full(n, np.nan)
    if n >= period + 1:
        valid_tr = tr[1:]
        if len(valid_tr) >= period:
            cumsum = np.nancumsum(valid_tr)
            atr_sma = np.full(len(valid_tr), np.nan)
            atr_sma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
            atr[1:] = atr_sma
    return atr


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

        # 持仓管理 (实盘模拟)
        self.position = None          # 当前持仓: None 或 dict
        self.balance = CAPITAL        # 账户余额
        self.peak_balance = CAPITAL   # 资金峰值 (用于回撤减仓)
        self.trade_history = []       # 已平仓交易历史
        self.bar_counter = 0          # K线计数 (用于 entry_bar 计算)

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
        highs = np.array([b["h"] for b in bars])
        lows = np.array([b["l"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        atr = calc_atr(highs, lows, closes, ATR_PERIOD)
        return closes, vols, roc5, roc20, vol_ma, atr

    # ---- 仓位管理辅助 ----
    def _get_position_size(self, drawdown):
        """根据回撤动态调整仓位倍数"""
        for threshold, multiplier in DRAWDOWN_THRESHOLDS:
            if drawdown <= threshold:
                return multiplier
        return 0.3

    def _open_position(self, direction, price, ts, cur_roc5, cur_roc20, cur_atr):
        """开仓: 计算 ATR 动态 SL/TP 价格, 记录持仓"""
        drawdown = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0
        multiplier = self._get_position_size(drawdown)
        fraction = FRACTION_BASE * multiplier
        notional = self.balance * fraction * LEVERAGE

        if direction == "long":
            sl_price = price - SL_ATR_MULT * cur_atr
            tp_price = price + TP_ATR_MULT * cur_atr
        else:
            sl_price = price + SL_ATR_MULT * cur_atr
            tp_price = price - TP_ATR_MULT * cur_atr

        self.position = {
            "direction": direction,
            "entry_price": round(float(price), 2),
            "size_usdt": round(notional, 2),
            "fraction": round(fraction, 4),
            "entry_time": int(ts),
            "entry_bar": self.bar_counter,
            "entry_roc5": round(float(cur_roc5), 2),
            "entry_roc20": round(float(cur_roc20), 2),
            "entry_atr": round(float(cur_atr), 2),
            "sl_price": round(float(sl_price), 2),
            "tp_price": round(float(tp_price), 2),
        }
        return self.position

    def _close_position(self, exit_price, reason, ts):
        """平仓: 计算盈亏, 记录交易, 更新余额"""
        pos = self.position
        if pos is None:
            return None
        if pos["direction"] == "long":
            price_diff = exit_price - pos["entry_price"]
        else:
            price_diff = pos["entry_price"] - exit_price
        pnl_pct = price_diff / pos["entry_price"]
        raw_pnl = pnl_pct * pos["size_usdt"]
        close_fee = pos["size_usdt"] * FEE_RATE / 2
        net_pnl = raw_pnl - close_fee

        trade = {
            **pos,
            "exit_price": round(float(exit_price), 2),
            "pnl": round(net_pnl, 4),
            "exit_time": int(ts),
            "held_bars": self.bar_counter - pos["entry_bar"],
            "reason": reason,
        }
        self.trade_history.append(trade)
        self.trade_history = self.trade_history[-50:]
        self.balance += net_pnl
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        self.position = None
        return trade

    def _check_exit_conditions(self, price, cur_roc5):
        """检查当前持仓是否触发出场, 返回 (should_close, reason, exit_price)"""
        if self.position is None:
            return False, "", 0
        pos = self.position
        direction = pos["direction"]
        held_bars = self.bar_counter - pos["entry_bar"]

        # 动量衰竭 (带阈值)
        if direction == "long" and cur_roc5 < -MOMENTUM_DEATH_THRESH:
            return True, "momentum_death", price
        if direction == "short" and cur_roc5 > MOMENTUM_DEATH_THRESH:
            return True, "momentum_death", price

        # ATR 止损
        if direction == "long" and price <= pos["sl_price"]:
            return True, "SL", pos["sl_price"]
        if direction == "short" and price >= pos["sl_price"]:
            return True, "SL", pos["sl_price"]

        # ATR 止盈
        if direction == "long" and price >= pos["tp_price"]:
            return True, "TP", pos["tp_price"]
        if direction == "short" and price <= pos["tp_price"]:
            return True, "TP", pos["tp_price"]

        # 超时
        if held_bars >= MAX_HOLD_BARS:
            return True, "timeout", price

        return False, "", 0

    def _get_position_view(self):
        """返回前端可展示的持仓视图 (含实时未实现盈亏)"""
        if self.position is None:
            return None
        pos = self.position
        price = self.last_price if self.last_price > 0 else pos["entry_price"]
        if pos["direction"] == "long":
            price_diff = price - pos["entry_price"]
        else:
            price_diff = pos["entry_price"] - price
        pnl_pct = price_diff / pos["entry_price"]
        unrealized = pnl_pct * pos["size_usdt"]
        held_bars = self.bar_counter - pos["entry_bar"]
        return {
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "current_price": round(float(price), 2),
            "size_usdt": pos["size_usdt"],
            "sl_price": pos["sl_price"],
            "tp_price": pos["tp_price"],
            "entry_atr": pos["entry_atr"],
            "unrealized_pnl": round(float(unrealized), 4),
            "unrealized_pct": round(float(pnl_pct * 100), 2),
            "held_bars": held_bars,
            "entry_time": pos["entry_time"],
        }

    # ---- 信号检测 + 持仓管理 ----
    def check_signal(self, dry_run=False):
        """基于最新收盘K线: 先检查出场, 再检查入场。返回事件列表 (入场/出场)

        dry_run=True 时仅更新指标/信号状态, 不开仓/平仓 (用于启动时)
        """
        closes, vols, roc5, roc20, vol_ma, atr = self.compute_indicators()
        i = len(closes) - 1
        if i < max(ROC_MEDIUM, VOL_MA_PERIOD, ATR_PERIOD) + 2:
            return []

        cur_roc5 = roc5[i]
        cur_roc20 = roc20[i]
        cur_vol = vols[i]
        cur_vol_ma = vol_ma[i]
        cur_atr = atr[i]
        price = closes[i]
        ts = self.bars[i]["t"]

        if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma) or np.isnan(cur_atr):
            return []

        # 更新指标状态
        self.indicator_state = {
            "price": round(float(price), 2),
            "roc5": round(float(cur_roc5), 4),
            "roc20": round(float(cur_roc20), 4),
            "volume": round(float(cur_vol), 2),
            "vol_ma": round(float(cur_vol_ma), 2),
            "vol_ratio": round(float(cur_vol / cur_vol_ma), 2) if cur_vol_ma > 0 else 0,
            "atr": round(float(cur_atr), 2),
            "ts": int(ts),
        }

        # 入场条件检查 (用于前端条件面板)
        vol_ok = cur_vol > cur_vol_ma
        long_roc = cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20
        short_roc = cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20

        self.signal_readiness = {
            "vol_confirmed": bool(vol_ok),
            "long_ready": bool(vol_ok and long_roc),
            "short_ready": bool(vol_ok and short_roc),
            "neutral": bool(not long_roc and not short_roc),
        }

        if dry_run:
            return []

        events = []

        # ---- 1. 有持仓 → 先检查出场 ----
        if self.position is not None:
            should_close, reason, exit_price = self._check_exit_conditions(price, cur_roc5)
            if should_close:
                trade = self._close_position(exit_price, reason, ts)
                if trade:
                    events.append({"type": "CLOSE", "trade": trade})
                    self.current_signal = None

        # ---- 2. 无持仓 → 检查入场信号 ----
        if self.position is None and vol_ok:
            signal = None
            if long_roc:
                signal = {"type": "BUY", "direction": "long", "price": round(float(price), 2),
                          "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                          "roc20": round(float(cur_roc20), 2), "atr": round(float(cur_atr), 2)}
                self.current_signal = "long"
            elif short_roc:
                signal = {"type": "SELL", "direction": "short", "price": round(float(price), 2),
                          "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                          "roc20": round(float(cur_roc20), 2), "atr": round(float(cur_atr), 2)}
                self.current_signal = "short"

            if signal:
                self._open_position(signal["direction"], price, ts,
                                    cur_roc5, cur_roc20, cur_atr)
                signal["sl_price"] = self.position["sl_price"]
                signal["tp_price"] = self.position["tp_price"]
                signal["size_usdt"] = self.position["size_usdt"]
                self.signal_log.append(signal)
                self.signal_log = self.signal_log[-50:]
                events.append({"type": "OPEN", "signal": signal})

        return events

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
                    self.bar_counter += 1

                    # 收盘K线 → 完整信号检测 + 持仓出场检查
                    events = self.check_signal()
                    self._print_status(bar)
                    for ev in events:
                        if ev["type"] == "OPEN":
                            self._print_signal(ev["signal"])
                        elif ev["type"] == "CLOSE":
                            self._print_close(ev["trade"])
                else:
                    # 未收盘K线 → 更新最新一根 + 实时 SL/TP 检查
                    if self.bars and self.bars[-1]["t"] == bar["t"]:
                        self.bars[-1] = bar
                    else:
                        self.bars.append(bar)
                    # 实时价格触及 SL/TP → 立即平仓 (不等收盘)
                    self._check_realtime_sl_tp(bar["c"], bar["t"])
        except Exception as e:
            print(f"  ⚠️ [{now()}] K线解析错误: {e}")

    def _check_realtime_sl_tp(self, price, ts):
        """未收盘K线的实时 SL/TP 检查 (价格触及立即平仓)"""
        if self.position is None:
            return
        pos = self.position
        direction = pos["direction"]
        should_close = False
        reason = ""
        exit_price = price

        if direction == "long" and price <= pos["sl_price"]:
            should_close, reason, exit_price = True, "SL", pos["sl_price"]
        elif direction == "short" and price >= pos["sl_price"]:
            should_close, reason, exit_price = True, "SL", pos["sl_price"]
        elif direction == "long" and price >= pos["tp_price"]:
            should_close, reason, exit_price = True, "TP", pos["tp_price"]
        elif direction == "short" and price <= pos["tp_price"]:
            should_close, reason, exit_price = True, "TP", pos["tp_price"]

        if should_close:
            trade = self._close_position(exit_price, reason, ts)
            if trade:
                self._print_close(trade)

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

        # 持仓状态
        pos_tag = "💤 空仓"
        if self.position is not None:
            pos = self._get_position_view()
            dir_emoji = "🟢多" if pos["direction"] == "long" else "🔴空"
            pnl_str = f"{pos['unrealized_pnl']:+.2f}U ({pos['unrealized_pct']:+.2f}%)"
            pos_tag = f"📂 {dir_emoji} 开{pos['entry_price']} 现{pos['current_price']} | 浮盈{pnl_str} | 持{pos['held_bars']}根"

        print(f"\n{'─'*65}")
        print(f"  📊 [{now()}] K线收盘 {t} | close={bar['c']:.2f} | 余额={self.balance:.2f}U")
        print(f"  📈 ROC({ROC_SHORT})={st.get('roc5', '?')}  ROC({ROC_MEDIUM})={st.get('roc20', '?')}  |  "
              f"Vol={st.get('volume', '?')} / MA={st.get('vol_ma', '?')} ({vol_tag})  |  ATR={st.get('atr', '?')}")
        print(f"  🎯 {signal_tag}  |  {pos_tag}")
        print(f"{'─'*65}")

    def _print_signal(self, signal):
        """开仓信号触发时醒目输出"""
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
        print(f"  {emoji} {emoji} {emoji}  *** {sig_type} 开仓 — {label} ***  {emoji} {emoji} {emoji}")
        print(f"  ═══════════════════════════════════════════════════════")
        print(f"  价格: {signal['price']} USDT")
        print(f"  ROC({ROC_SHORT}): {signal['roc5']}%  |  ROC({ROC_MEDIUM}): {signal['roc20']}%  |  ATR: {signal.get('atr', '?')}")
        print(f"  时间: {ts_to_str(signal['ts'])}")
        print(f"  仓位: {signal.get('size_usdt', '?')} USDT ({FRACTION_BASE*100:.0f}% × {LEVERAGE}x)")
        print(f"  止损: {signal.get('sl_price', '?')} USDT ({SL_ATR_MULT}×ATR)")
        print(f"  止盈: {signal.get('tp_price', '?')} USDT ({TP_ATR_MULT}×ATR)")
        print(f"  最大持仓: {MAX_HOLD_BARS}根K线")
        print(f"  ═══════════════════════════════════════════════════════")
        print(f"{border}\n")

    def _print_close(self, trade):
        """平仓时醒目输出"""
        direction = trade["direction"]
        reason_labels = {"momentum_death": "动量衰竭", "SL": "止损", "TP": "止盈",
                         "timeout": "超时", "force_close": "强平"}
        reason_label = reason_labels.get(trade["reason"], trade["reason"])
        pnl = trade["pnl"]
        is_win = pnl >= 0
        border = "🟢" * 20 if is_win else "🔴" * 20
        emoji = "💰" if is_win else "💔"

        print(f"\n{border}")
        print(f"  {emoji} {emoji} {emoji}  *** 平仓 — {reason_label} ***  {emoji} {emoji} {emoji}")
        print(f"  ═══════════════════════════════════════════════════════")
        dir_label = "做多" if direction == "long" else "做空"
        print(f"  方向: {dir_label}  |  持仓: {trade['held_bars']}根K线")
        print(f"  开仓: {trade['entry_price']}  →  平仓: {trade['exit_price']} USDT")
        print(f"  盈亏: {pnl:+.4f} USDT  |  时间: {ts_to_str(trade['exit_time'])}")
        print(f"  余额: {self.balance:.2f} USDT")
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
        print(f"  🎯 ETH 双ROC动量策略 — 合约({MARKET_NAME})实时模拟交易")
        print(f"  策略: ROC({ROC_SHORT})/ROC({ROC_MEDIUM}) + VolMA({VOL_MA_PERIOD}) + ATR({ATR_PERIOD})")
        print(f"  做多: ROC{ROC_SHORT}>0 & ROC{ROC_MEDIUM}>0 & ROC{ROC_SHORT}>ROC{ROC_MEDIUM} & 放量")
        print(f"  做空: ROC{ROC_SHORT}<0 & ROC{ROC_MEDIUM}<0 & ROC{ROC_SHORT}<ROC{ROC_MEDIUM} & 放量")
        print(f"  本金: {CAPITAL}U | 杠杆: {LEVERAGE}x | 仓位: {FRACTION_BASE*100:.0f}%")
        print(f"  止损: {SL_ATR_MULT}×ATR | 止盈: {TP_ATR_MULT}×ATR | 动量阈值: ±{MOMENTUM_DEATH_THRESH}")
        print(f"  最大持仓: {MAX_HOLD_BARS}根K线")
        print(f"  数据源: {MARKET_NAME} | {WS_KLINE_URL}")
        print(f"  面板  : http://127.0.0.1:{SERVER_PORT} (合约)")
        print(f"{'='*65}")

        # 历史回补
        self.fetch_history()
        # 初始信号检测 (启动时 dry_run, 仅更新指标状态, 不开仓)
        self.check_signal(dry_run=True)
        if self.bars:
            self._print_status(self.bars[-1])

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
        highs = np.array([b["h"] for b in bars])
        lows = np.array([b["l"] for b in bars])
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        atr_arr = calc_atr(highs, lows, closes, ATR_PERIOD)

        klines = [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]

        def series(arr):
            out = []
            for i, v in enumerate(arr):
                if not np.isnan(v):
                    out.append([int(bars[i]["t"]), round(float(v), 4)])
            return out

        # 实时指标: 用最新 bar 重新计算, 不依赖 K线收盘时的缓存
        if len(closes) and not np.isnan(roc5[-1]) and not np.isnan(vol_ma[-1]):
            _price = round(float(closes[-1]), 2)
            _vol, _volma = float(vols[-1]), float(vol_ma[-1])
            _roc5, _roc20 = float(roc5[-1]), float(roc20[-1])
            _atr = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0
            indicator_state = {
                "price": _price,
                "roc5": round(_roc5, 4),
                "roc20": round(_roc20, 4),
                "volume": round(_vol, 2),
                "vol_ma": round(_volma, 2),
                "vol_ratio": round(_vol / _volma, 2) if _volma > 0 else 0,
                "atr": round(_atr, 2),
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

        # 持仓视图 + 账户状态
        position_view = self._get_position_view()
        drawdown = (self.peak_balance - self.balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0
        return_pct = (self.balance - CAPITAL) / CAPITAL * 100

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
            "position": position_view,
            "balance": round(self.balance, 2),
            "peak_balance": round(self.peak_balance, 2),
            "initial_capital": CAPITAL,
            "drawdown_pct": round(drawdown, 2),
            "return_pct": round(return_pct, 2),
            "trade_history": list(self.trade_history[-10:]),
            "params": {
                "roc_short": ROC_SHORT, "roc_medium": ROC_MEDIUM,
                "vol_ma_period": VOL_MA_PERIOD, "leverage": LEVERAGE,
                "capital": CAPITAL, "fraction_base": FRACTION_BASE,
                "momentum_death_thresh": MOMENTUM_DEATH_THRESH,
                "atr_period": ATR_PERIOD, "sl_atr_mult": SL_ATR_MULT,
                "tp_atr_mult": TP_ATR_MULT, "max_hold_bars": MAX_HOLD_BARS,
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
<title>ETH v12 合约实时交易信号面板</title>
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
.account-card{background:#21262d;border-radius:6px;padding:10px;margin:4px 0}
.account-row{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.account-row .label{color:#8b949e}
.account-row .value{font-weight:bold}
.balance-big{font-size:22px;font-weight:bold;color:#f7931a;text-align:center;padding:6px 0}
.position-card{border-radius:6px;padding:10px;margin:4px 0;font-size:12px}
.position-card.long{background:#0d2818;border:2px solid #3fb950}
.position-card.short{background:#2a0d0d;border:2px solid #f85149}
.position-card.empty{background:#1a1a2e;border:2px solid #30363d;color:#8b949e;text-align:center;padding:18px}
.pos-header{display:flex;justify-content:space-between;font-weight:bold;margin-bottom:8px;font-size:13px}
.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.pos-grid .p-label{color:#8b949e;font-size:11px}
.pos-grid .p-val{font-weight:bold}
.pnl-positive{color:#3fb950}
.pnl-negative{color:#f85149}
.trade-list{max-height:200px;overflow-y:auto}
.trade-item{display:flex;align-items:center;padding:5px 8px;margin:2px 0;border-radius:3px;font-size:11px;background:#21262d}
.trade-item.win{border-left:3px solid #3fb950}
.trade-item.lose{border-left:3px solid #f85149}
.trade-item .t-dir{font-weight:bold;width:28px}
.trade-item .t-prices{flex:1;color:#8b949e}
.trade-item .t-pnl{font-weight:bold;width:60px;text-align:right}
.trade-item .t-reason{color:#8b949e;width:50px;text-align:right;font-size:10px}
</style>
</head>
<body>

<div class="header">
  <h1><span class="live-dot"></span>ETH v12 双ROC动量策略 — 合约实时信号</h1>
  <div class="nav">
    <a href="http://127.0.0.1:8080">现货</a>
    <a href="http://127.0.0.1:8081" class="active">合约</a>
  </div>
  <div class="price-display" id="price-display">--</div>
</div>

<div class="layout">
  <div class="chart-area">
    <div id="kline-chart"></div>
    <div id="roc-chart"></div>
  </div>
  <div class="sidebar">
    <!-- 账户概览 -->
    <div class="section">
      <h3>💰 账户概览</h3>
      <div class="account-card">
        <div class="balance-big" id="acc-balance">--</div>
        <div class="account-row"><span class="label">本金</span><span class="value" id="acc-initial">--</span></div>
        <div class="account-row"><span class="label">收益率</span><span class="value" id="acc-return">--</span></div>
        <div class="account-row"><span class="label">峰值</span><span class="value" id="acc-peak">--</span></div>
        <div class="account-row"><span class="label">回撤</span><span class="value" id="acc-drawdown">--</span></div>
      </div>
    </div>

    <!-- 当前持仓 -->
    <div class="section">
      <h3>📂 当前持仓</h3>
      <div id="position-panel">
        <div class="position-card empty">💤 空仓等待</div>
      </div>
    </div>

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
      <div class="indicator-row"><span class="label">ATR(14)</span><span class="value" id="ind-atr">--</span></div>
    </div>

    <!-- 策略参数 -->
    <div class="section">
      <h3>⚙️ 策略参数</h3>
      <div class="param-grid">
        <div class="param-item"><span class="p-label">本金</span><br><span class="p-val" id="p-capital">150U</span></div>
        <div class="param-item"><span class="p-label">杠杆</span><br><span class="p-val" id="p-leverage">8x</span></div>
        <div class="param-item"><span class="p-label">仓位</span><br><span class="p-val" id="p-fraction">25%</span></div>
        <div class="param-item"><span class="p-label">ROC短/中</span><br><span class="p-val" id="p-roc">8/20</span></div>
        <div class="param-item"><span class="p-label">止损</span><br><span class="p-val" id="p-sl">1.5×ATR</span></div>
        <div class="param-item"><span class="p-label">止盈</span><br><span class="p-val" id="p-tp">关闭</span></div>
        <div class="param-item"><span class="p-label">动量阈值</span><br><span class="p-val" id="p-md">穿零</span></div>
        <div class="param-item"><span class="p-label">最大持仓</span><br><span class="p-val" id="p-hold">72根</span></div>
      </div>
    </div>

    <!-- 最近交易 -->
    <div class="section">
      <h3>📋 最近交易</h3>
      <div class="trade-list" id="trade-list"></div>
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
  const volMaData = data.volma20.map(v => v[1]);

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
  const roc5Data = data.roc5.map(v => v[1]);
  const roc20Data = data.roc20.map(v => v[1]);
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

  // 指标 (含 ATR)
  const s = data.indicator_state || {};
  document.getElementById('ind-roc5').textContent = (s.roc5||0).toFixed(4) + '%';
  document.getElementById('ind-roc20').textContent = (s.roc20||0).toFixed(4) + '%';
  document.getElementById('ind-vol').textContent = (s.volume||0).toFixed(1);
  document.getElementById('ind-volma').textContent = (s.vol_ma||0).toFixed(1);
  document.getElementById('ind-volratio').textContent = (s.vol_ratio||0).toFixed(2) + 'x';
  document.getElementById('ind-atr').textContent = (s.atr||0).toFixed(2);

  // 账户概览
  document.getElementById('acc-balance').textContent = (data.balance||0).toFixed(2) + ' U';
  document.getElementById('acc-initial').textContent = (data.initial_capital||0).toFixed(0) + ' U';
  const retPct = data.return_pct || 0;
  const retEl = document.getElementById('acc-return');
  retEl.textContent = (retPct>=0?'+':'') + retPct.toFixed(2) + '%';
  retEl.className = 'value ' + (retPct>=0?'pnl-positive':'pnl-negative');
  document.getElementById('acc-peak').textContent = (data.peak_balance||0).toFixed(2) + ' U';
  document.getElementById('acc-drawdown').textContent = (data.drawdown_pct||0).toFixed(2) + '%';

  // 当前持仓
  const posDiv = document.getElementById('position-panel');
  const pos = data.position;
  if (pos) {
    const isLong = pos.direction === 'long';
    const cls = isLong ? 'long' : 'short';
    const dirLabel = isLong ? '🟢 做多' : '🔴 做空';
    const pnlClass = pos.unrealized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
    const pnlStr = (pos.unrealized_pnl>=0?'+':'') + pos.unrealized_pnl.toFixed(2) + ' U (' + (pos.unrealized_pct>=0?'+':'') + pos.unrealized_pct.toFixed(2) + '%)';
    posDiv.innerHTML = `<div class="position-card ${cls}">
      <div class="pos-header"><span>${dirLabel}</span><span>持 ${pos.held_bars}根</span></div>
      <div class="pos-grid">
        <div><span class="p-label">开仓价</span><br><span class="p-val">${pos.entry_price}</span></div>
        <div><span class="p-label">当前价</span><br><span class="p-val">${pos.current_price}</span></div>
        <div><span class="p-label">止损</span><br><span class="p-val pnl-negative">${pos.sl_price}</span></div>
        <div><span class="p-label">止盈</span><br><span class="p-val pnl-positive">${pos.tp_price}</span></div>
        <div><span class="p-label">仓位</span><br><span class="p-val">${pos.size_usdt.toFixed(1)}U</span></div>
        <div><span class="p-label">ATR</span><br><span class="p-val">${pos.entry_atr}</span></div>
      </div>
      <div style="margin-top:6px;text-align:center;font-size:14px;font-weight:bold" class="${pnlClass}">浮盈 ${pnlStr}</div>
    </div>`;
  } else {
    posDiv.innerHTML = '<div class="position-card empty">💤 空仓等待</div>';
  }

  // 信号警报
  const rdy = data.signal_readiness || {};
  const alertDiv = document.getElementById('signal-alert');
  alertDiv.className = 'signal-alert';
  if (pos) {
    // 有持仓时显示持仓方向
    if (pos.direction === 'long') {
      alertDiv.className += ' buy';
      alertDiv.innerHTML = '📈 <b>持仓做多</b><br>等待出场信号';
    } else {
      alertDiv.className += ' sell';
      alertDiv.innerHTML = '📉 <b>持仓做空</b><br>等待出场信号';
    }
  } else if (rdy.long_ready) {
    alertDiv.className += ' buy';
    alertDiv.innerHTML = '📈 <b>做多信号就绪！</b><br>ROC(10)>0 & ROC(25)>0 & 放量';
  } else if (rdy.short_ready) {
    alertDiv.className += ' sell';
    alertDiv.innerHTML = '📉 <b>做空信号就绪！</b><br>ROC(10)<0 & ROC(25)<0 & 放量';
  } else {
    alertDiv.className += ' neutral';
    alertDiv.innerHTML = '⚪ 等待入场信号<br><small>条件未全部满足</small>';
  }

  // 条件检查
  updateCondition('cond-vol', rdy.vol_confirmed, '成交量 > VolMA(30)');
  updateCondition('cond-long', rdy.long_ready, 'ROC(10)>0 & ROC(25)>0 & ROC(10)>ROC(25)');
  updateCondition('cond-short', rdy.short_ready, 'ROC(10)<0 & ROC(25)<0 & ROC(10)<ROC(25)');

  // 最近交易
  const tradeListDiv = document.getElementById('trade-list');
  const trades = data.trade_history || [];
  tradeListDiv.innerHTML = trades.slice().reverse().map(t => {
    const isWin = t.pnl >= 0;
    const cls = isWin ? 'win' : 'lose';
    const dirLabel = t.direction === 'long' ? '多' : '空';
    const reasonMap = {momentum_death:'动量', SL:'止损', TP:'止盈', timeout:'超时', force_close:'强平'};
    const reason = reasonMap[t.reason] || t.reason;
    return `<div class="trade-item ${cls}">
      <span class="t-dir">${dirLabel}</span>
      <span class="t-prices">${t.entry_price}→${t.exit_price}</span>
      <span class="t-pnl ${isWin?'pnl-positive':'pnl-negative'}">${isWin?'+':''}${t.pnl.toFixed(2)}</span>
      <span class="t-reason">${reason}</span>
    </div>`;
  }).join('') || '<div style="color:#8b949e;font-size:12px;text-align:center;padding:10px">暂无交易</div>';

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
    print(f"  🌐 启动 Web 面板(合约): http://127.0.0.1:{SERVER_PORT}")
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


app = FastAPI(title="ETH v12 合约实时交易信号", lifespan=lifespan)
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


# 猴子补丁: trader 的 _print_status/_print_signal/_print_close 中触发 broadcast + 微信通知
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
    title = f"[合约] {label}开仓 | ETH={signal['price']} USDT"
    content = (
        f"## [合约] {sig_type} 开仓 — {label}\n\n"
        f"- **市场**: {MARKET_NAME} (USDⓈ-M Futures)\n"
        f"- **价格**: {signal['price']} USDT\n"
        f"- **ROC({ROC_SHORT})**: {signal['roc5']}%\n"
        f"- **ROC({ROC_MEDIUM})**: {signal['roc20']}%\n"
        f"- **ATR**: {signal.get('atr', '?')}\n"
        f"- **时间**: {ts_to_str(signal['ts'])}\n"
        f"- **仓位**: {signal.get('size_usdt', '?')} USDT ({FRACTION_BASE*100:.0f}% × {LEVERAGE}x)\n"
        f"- **止损**: {signal.get('sl_price', '?')} USDT ({SL_ATR_MULT}×ATR)\n"
        f"- **止盈**: {signal.get('tp_price', '?')} USDT ({TP_ATR_MULT}×ATR)\n"
        f"\n> ETH 双ROC动量策略 · 合约实时交易"
    )
    threading.Thread(target=wx_notify, args=(title, content), daemon=True).start()
trader._print_signal = _print_signal_with_broadcast

_orig_print_close = trader._print_close
def _print_close_with_broadcast(trade):
    _orig_print_close(trade)
    broadcast()
    # 平仓微信通知
    reason_labels = {"momentum_death": "动量衰竭", "SL": "止损", "TP": "止盈",
                     "timeout": "超时", "force_close": "强平"}
    reason_label = reason_labels.get(trade["reason"], trade["reason"])
    direction = trade["direction"]
    dir_label = "做多" if direction == "long" else "做空"
    pnl = trade["pnl"]
    is_win = pnl >= 0
    emoji = "💰" if is_win else "💔"
    title = f"[合约] {emoji} 平仓 {reason_label} | 盈亏={pnl:+.2f}U"
    content = (
        f"## [合约] 平仓 — {reason_label}\n\n"
        f"- **方向**: {dir_label}\n"
        f"- **开仓价**: {trade['entry_price']} USDT\n"
        f"- **平仓价**: {trade['exit_price']} USDT\n"
        f"- **盈亏**: {pnl:+.4f} USDT\n"
        f"- **持仓**: {trade['held_bars']}根K线\n"
        f"- **时间**: {ts_to_str(trade['exit_time'])}\n"
        f"- **余额**: {trader.balance:.2f} USDT\n"
        f"\n> ETH 双ROC动量策略 · 合约实时交易"
    )
    threading.Thread(target=wx_notify, args=(title, content), daemon=True).start()
trader._print_close = _print_close_with_broadcast


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")
