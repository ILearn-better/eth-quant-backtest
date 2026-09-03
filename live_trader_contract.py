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

import daily_log  # 按天双写日志: logs/contract/YYYY-MM-DD.log

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
WS_AGGTRADE_URL = FUTURES["ws_aggtrade"]
REST_KLINE_URL = FUTURES["rest_kline"]
REST_TICKER_URL = FUTURES["rest_ticker"]
REST_LSR_TOP_URL = FUTURES["rest_long_short_ratio_top"]
REST_LSR_ACCT_URL = FUTURES["rest_long_short_ratio_acct"]
LSR_POLL_INTERVAL = 300     # 多空比轮询间隔(秒, 5分钟)

# 大资金流向参数
LARGE_ORDER_USDT = 100000   # 单笔成交额 ≥ 10万U 算大单
FLOW_WINDOWS = [5, 15, 60]  # 净买卖统计窗口(分钟)
LARGE_ORDER_KEEP = 50       # 后端保留大单条数(deque maxlen)

# 服务端口 (与现货 8080 区分, 避免冲突)
SERVER_PORT = 8081

HISTORY_BARS = 300
RECONNECT_DELAY = 5
PUSH_INTERVAL = 3           # 定时广播间隔(秒), 不依赖 K线收盘

# ---- 合约策略参数 (五维共振版, 与 strategies/eth_roc_momentum_contract_resonance.py 一致) ----
CAPITAL = 150.0
LEVERAGE = 20              # 高杠杆: 靠五维共振提高胜率支撑
FRACTION_BASE = 0.20       # 基础仓位 (20x杠杆下压缩仓位, 有效杠杆4x)
FEE_RATE = 0.0004
MAX_HOLD_BARS = 72
ROC_SHORT = 8
ROC_MEDIUM = 20
ROC_LONG = 50              # 2天动量确认 (时间框架共振)
VOL_MA_PERIOD = 20
TREND_MA_PERIOD = 50       # 2天趋势线 (时间框架共振)
ATR_STATE_PERIOD = 50      # ATR 状态均线 (波动放大期判断)

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
    # NaN 前缀(如 ATR 的 index 0)不传播: 用 0 填充后 nancumsum, 与回测策略 calc_ma 保持一致
    cumsum = np.nancumsum(np.where(np.isnan(values), 0, values))
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


# ---- 趋势判断 (MA20/MA50 排列 + 间距 + 斜率) ----
def analyze_trend(closes):
    """三分类趋势: 上升/震荡/下降.
    规则: |MA20-MA50|/MA50 < 0.3% → 震荡(均线纠缠);
          MA20>MA50 且价≥MA20 → 上升;  MA20<MA50 且价≤MA20 → 下降; 其余为震荡.
    """
    n = len(closes)
    unknown = {"trend": "unknown", "label": "--", "ma_fast": None,
               "ma_slow": None, "spread_pct": None, "slope_pct": None}
    if n < 60:
        return unknown
    ma_fast = calc_ma(closes, 20)
    ma_slow = calc_ma(closes, 50)
    cf, cs = ma_fast[-1], ma_slow[-1]
    if np.isnan(cf) or np.isnan(cs) or cs == 0:
        return unknown
    price = closes[-1]
    slope = (ma_fast[-1] - ma_fast[-6]) / ma_fast[-6] * 100 if ma_fast[-6] != 0 else 0.0
    spread = (cf - cs) / cs * 100
    if abs(spread) < 0.3:
        trend, label = "sideways", "震荡"
    elif cf > cs and price >= cf:
        trend, label = "up", "上升"
    elif cf < cs and price <= cf:
        trend, label = "down", "下降"
    else:
        trend, label = "sideways", "震荡"
    return {
        "trend": trend, "label": label,
        "ma_fast": round(float(cf), 2), "ma_slow": round(float(cs), 2),
        "spread_pct": round(float(spread), 3), "slope_pct": round(float(slope), 3),
    }


# ---- 支撑/突破位 (swing low/high 聚类) ----
def _find_levels(bars, closes, use_high, lookback=150, k=3, max_levels=2, tol=0.004, below=True):
    """通用: 找最近 lookback 根内的摆动极值(use_high=True→swing high),
    聚类(±0.4%), 按方向筛选(支撑=低于现价 / 突破=高于现价),
    返回最新的最多 max_levels 个. 返回: [{"price": float, "ts": int}] (按时间从新到旧)
    """
    n = len(bars)
    if n < 2 * k + 5:
        return []
    lo = max(0, n - lookback)
    arr = [b["h"] if use_high else b["l"] for b in bars]
    price = closes[-1]
    if use_high:
        pivots = [(bars[i]["t"], arr[i]) for i in range(lo + k, n - k)
                  if arr[i] > max(arr[i - k:i]) and arr[i] > max(arr[i + 1:i + k + 1])]
    else:
        pivots = [(bars[i]["t"], arr[i]) for i in range(lo + k, n - k)
                  if arr[i] < min(arr[i - k:i]) and arr[i] < min(arr[i + 1:i + k + 1])]
    if not pivots:
        return []
    # 按价格升序聚类合并 (支撑取更低, 突破取更高)
    pivots.sort(key=lambda x: x[1])
    levels = []
    for ts, p in pivots:
        hit = None
        for lv in levels:
            if abs(p - lv["price"]) / lv["price"] < tol:
                hit = lv
                break
        if hit is not None:
            hit["price"] = max(hit["price"], p) if use_high else min(hit["price"], p)
            hit["ts"] = max(hit["ts"], ts)
        else:
            levels.append({"price": p, "ts": ts})
    if below:
        sel = [lv for lv in levels if lv["price"] < price]
    else:
        sel = [lv for lv in levels if lv["price"] > price]
    sel.sort(key=lambda x: x["ts"], reverse=True)
    return [{"price": round(float(lv["price"]), 2), "ts": int(lv["ts"])} for lv in sel[:max_levels]]


def find_supports(bars, closes, lookback=150, k=3, max_levels=2, tol=0.004):
    """支撑位: 低于现价的摆动低点聚类"""
    return _find_levels(bars, closes, use_high=False, lookback=lookback, k=k,
                        max_levels=max_levels, tol=tol, below=True)


def find_resistances(bars, closes, lookback=150, k=3, max_levels=2, tol=0.004):
    """突破位(压力): 高于现价的摆动高点聚类"""
    return _find_levels(bars, closes, use_high=True, lookback=lookback, k=k,
                        max_levels=max_levels, tol=tol, below=False)


# ---- 布林带 (BOLL 20, 2σ) ----
def calc_boll(closes, period=20, mult=2.0):
    """布林带: 返回 (upper, mid, lower) 三个 np 数组, 与 calc_ma 同为 NaN 前缀.
    mid = MA(period);  std = sqrt(MA(close^2) - mid^2)
    """
    closes = np.asarray(closes, dtype=float)
    mid = calc_ma(closes, period)
    sq = calc_ma(closes * closes, period)
    var = sq - mid * mid
    std = np.sqrt(np.clip(var, 0.0, None))
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


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

        # 大资金流向 (aggTrade 大单聚合 + 合约多空比)
        from collections import deque
        self.large_orders = deque(maxlen=LARGE_ORDER_KEEP)
        self.flow_window = deque(maxlen=5000)
        self.long_short_ratio = None    # 合约多空比(轮询填充)

        # 启动时恢复历史信号 + 持仓状态(避免重启丢失信号 / 持仓断链)
        self._load_state()

    # ---- 持久化: 信号 JSONL + 持仓状态 JSON ----
    def _signal_file(self):
        return os.path.join(BASE_DIR, "data", "futures", "signals_contract.jsonl")

    def _state_file(self):
        return os.path.join(BASE_DIR, "data", "futures", "state_contract.json")

    def _append_signal_jsonl(self, signal):
        """追加一条信号到 JSONL 文件(失败只警告, 不阻断信号链路)"""
        try:
            with open(self._signal_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(signal, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"  ⚠️ [{now()}] 信号落盘失败: {e}")

    def _save_state(self):
        """原子写入持仓状态(position=None 也会写入, 等于清空持仓记录)"""
        st = {
            "position": self.position,
            "balance": self.balance,
            "peak_balance": self.peak_balance,
            "bar_counter": self.bar_counter,
            "trade_history": self.trade_history,
        }
        tmp = self._state_file() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file())
        except Exception as e:
            print(f"  ⚠️ [{now()}] 持仓状态落盘失败: {e}")

    def _load_state(self):
        """启动时加载历史信号(最近1000条)+ 恢复持仓状态"""
        # 1. 历史信号
        try:
            with open(self._signal_file(), encoding="utf-8") as f:
                lines = f.readlines()
            self.signal_log = [json.loads(l) for l in lines[-1000:] if l.strip()]
            print(f"  📂 已加载 {len(self.signal_log)} 条历史信号 (data/futures/signals_contract.jsonl)")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  ⚠️ 加载历史信号失败: {e}")
        # 2. 持仓状态
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                st = json.load(f)
            self.balance = st.get("balance", CAPITAL)
            self.peak_balance = st.get("peak_balance", CAPITAL)
            self.bar_counter = st.get("bar_counter", 0)
            self.trade_history = st.get("trade_history", [])
            if st.get("position"):
                self.position = st["position"]
                p = self.position
                print(f"  ⚠️ 检测到未平仓位: {p['direction']} @ {p['entry_price']} USDT")
                print(f"  ⚠️ 已自动恢复持仓跟踪。如已手动平仓, 请删除 data/futures/state_contract.json 后重启")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  ⚠️ 加载持仓状态失败: {e}")

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
        roc50 = calc_roc(closes, ROC_LONG)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        trend_ma = calc_ma(closes, TREND_MA_PERIOD)
        atr = calc_atr(highs, lows, closes, ATR_PERIOD)
        atr_state = calc_ma(atr, ATR_STATE_PERIOD)
        return closes, vols, roc5, roc20, roc50, vol_ma, trend_ma, atr, atr_state

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
        self._save_state()                    # 持仓状态落盘
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
        self.trade_history = self.trade_history[-1000:]
        self.balance += net_pnl
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        self.position = None
        # 平仓信号也进 signal_log + 落盘(修复此前合约前端看不到平仓历史的 bug)
        close_signal = {
            "type": "CLOSE",
            "direction": pos["direction"],
            "price": round(float(exit_price), 2),
            "ts": int(ts),
            "roc5": None,
            "roc20": None,
            "reason": reason,
            "pnl": round(net_pnl, 4),
            "entry_price": pos["entry_price"],
            "exit_price": round(float(exit_price), 2),
            "held_bars": self.bar_counter - pos["entry_bar"],
            "received_at": now(),
        }
        self.signal_log.append(close_signal)
        self.signal_log = self.signal_log[-1000:]
        self._append_signal_jsonl(close_signal)
        self._save_state()                    # 持仓状态落盘
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

    def _current_atr(self, period=ATR_PERIOD):
        """计算最近一根K线的 ATR (锁外调用, 内部仅短暂快照 bars)"""
        with self.lock:
            bars = list(self.bars)
        if len(bars) < period + 2:
            return 0.0
        highs = np.array([b["h"] for b in bars])
        lows = np.array([b["l"] for b in bars])
        closes = np.array([b["c"] for b in bars])
        atr = calc_atr(highs, lows, closes, period)
        v = atr[-1]
        return float(v) if not np.isnan(v) else 0.0

    def manual_order(self, side, exec_price, usdt, leverage, ts):
        """手动下单(模拟): 空仓→开仓(自定义杠杆+ATR止损); 反方向→平仓. 返回 {"ok", "message"}"""
        direction = "long" if side == "buy" else "short"
        cn_dir = "多" if direction == "long" else "空"
        cur_atr = self._current_atr()          # 锁外算 ATR, 避免嵌套锁
        with self.lock:
            if self.position is not None:
                pos = self.position
                if pos["direction"] == direction:
                    return {"ok": False, "message": f"已有同方向持仓(做{cn_dir}), 请先平仓或反向下单"}
                trade = self._close_position(exec_price, "manual", int(ts))
                return {"ok": True, "message": f"平仓成功(做{cn_dir}平), 成交 {exec_price:.2f}, 盈亏 {trade['pnl']:+.2f}U"}
            # 空仓 → 开仓
            if direction == "long":
                sl_price = exec_price - SL_ATR_MULT * cur_atr
                tp_price = exec_price + TP_ATR_MULT * cur_atr
            else:
                sl_price = exec_price + SL_ATR_MULT * cur_atr
                tp_price = exec_price - TP_ATR_MULT * cur_atr
            self.position = {
                "direction": direction,
                "entry_price": round(float(exec_price), 2),
                "size_usdt": round(float(usdt), 2),
                "fraction": 0,
                "entry_time": int(ts),
                "entry_bar": self.bar_counter,
                "entry_roc5": 0,
                "entry_roc20": 0,
                "entry_atr": round(float(cur_atr), 2),
                "sl_price": round(float(sl_price), 2),
                "tp_price": round(float(tp_price), 2),
                "leverage": int(leverage),
                "manual": True,
            }
            signal = {
                "type": "BUY" if direction == "long" else "SELL",
                "direction": direction,
                "price": round(float(exec_price), 2),
                "ts": int(ts),
                "roc5": None, "roc20": None,
                "reason": "manual", "reason_desc": f"手动开仓(做{cn_dir}, 杠杆{int(leverage)}x)",
                "manual": True,
                "received_at": now(),
            }
            self.signal_log.append(signal)
            self.signal_log = self.signal_log[-1000:]
            self._append_signal_jsonl(signal)
            self._save_state()
            return {"ok": True, "message": f"开仓成功(做{cn_dir}), 成交 {exec_price:.2f}, 名义 {usdt:.0f}U @ {int(leverage)}x, 止损 {sl_price:.2f}"}

    # ---- 信号检测 + 持仓管理 ----
    def check_signal(self, dry_run=False):
        """基于最新收盘K线: 先检查出场, 再检查入场。返回事件列表 (入场/出场)

        dry_run=True 时仅更新指标/信号状态, 不开仓/平仓 (用于启动时)
        """
        closes, vols, roc5, roc20, roc50, vol_ma, trend_ma, atr, atr_state = self.compute_indicators()
        i = len(closes) - 1
        if i < max(ROC_MEDIUM, VOL_MA_PERIOD, ATR_PERIOD, ROC_LONG, TREND_MA_PERIOD, ATR_STATE_PERIOD) + 2:
            return []

        cur_roc5 = roc5[i]
        cur_roc20 = roc20[i]
        cur_roc50 = roc50[i]
        cur_vol = vols[i]
        cur_vol_ma = vol_ma[i]
        cur_trend_ma = trend_ma[i]
        cur_atr = atr[i]
        cur_atr_state = atr_state[i]
        price = closes[i]
        ts = self.bars[i]["t"]

        if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_roc50) \
           or np.isnan(cur_vol_ma) or np.isnan(cur_trend_ma) \
           or np.isnan(cur_atr) or np.isnan(cur_atr_state):
            return []

        # 更新指标状态
        self.indicator_state = {
            "price": round(float(price), 2),
            "roc5": round(float(cur_roc5), 4),
            "roc20": round(float(cur_roc20), 4),
            "roc50": round(float(cur_roc50), 4),
            "trend_ma": round(float(cur_trend_ma), 2),
            "volume": round(float(cur_vol), 2),
            "vol_ma": round(float(cur_vol_ma), 2),
            "vol_ratio": round(float(cur_vol / cur_vol_ma), 2) if cur_vol_ma > 0 else 0,
            "atr": round(float(cur_atr), 2),
            "atr_state": round(float(cur_atr_state), 2),
            "ts": int(ts),
        }

        # 入场条件检查 (五维共振: 双ROC加速 + 量能 + 2天趋势 + 2天动量 + 波动放大)
        vol_ok = cur_vol > cur_vol_ma
        atr_expanding = cur_atr > cur_atr_state
        long_roc = (cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20
                    and price > cur_trend_ma and cur_roc50 > 0)
        short_roc = (cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20
                     and price < cur_trend_ma and cur_roc50 < 0)

        self.signal_readiness = {
            "vol_confirmed": bool(vol_ok),
            "atr_expanding": bool(atr_expanding),
            "trend_up": bool(price > cur_trend_ma),
            "trend_down": bool(price < cur_trend_ma),
            # 做多逐维度 (供前端逐条打勾)
            "long_roc5": bool(cur_roc5 > 0),
            "long_roc20": bool(cur_roc20 > 0),
            "long_acc": bool(cur_roc5 > cur_roc20),
            "long_trend": bool(price > cur_trend_ma),
            "long_roc50": bool(cur_roc50 > 0),
            # 做空逐维度
            "short_roc5": bool(cur_roc5 < 0),
            "short_roc20": bool(cur_roc20 < 0),
            "short_acc": bool(cur_roc5 < cur_roc20),
            "short_trend": bool(price < cur_trend_ma),
            "short_roc50": bool(cur_roc50 < 0),
            "long_ready": bool(vol_ok and atr_expanding and long_roc),
            "short_ready": bool(vol_ok and atr_expanding and short_roc),
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

        # ---- 2. 无持仓 → 检查入场信号 (五维共振) ----
        if self.position is None and vol_ok and atr_expanding:
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
                signal["received_at"] = now()           # 本地触发时间, 排查延迟用
                self.signal_log.append(signal)
                self.signal_log = self.signal_log[-1000:]   # 50 → 1000
                self._append_signal_jsonl(signal)            # 落盘 JSONL
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

            # Phase 1: 锁内只更新 bars (不调用 check_signal, 避免与 compute_indicators 死锁)
            need_signal_check = False
            closed_bar_copy = None
            with self.lock:
                if is_closed:
                    if self.bars and self.bars[-1]["t"] == bar["t"]:
                        self.bars[-1] = bar
                    else:
                        self.bars.append(bar)
                        if len(self.bars) > HISTORY_BARS:
                            self.bars = self.bars[-HISTORY_BARS:]
                    self.bar_counter += 1
                    closed_bar_copy = dict(bar)
                    need_signal_check = True
                else:
                    # 未收盘K线 → 更新最新一根
                    if self.bars and self.bars[-1]["t"] == bar["t"]:
                        self.bars[-1] = bar
                    else:
                        self.bars.append(bar)

            # Phase 2: 锁外执行信号检测 + 打印 (避免 threading.Lock 不可重入死锁)
            if need_signal_check:
                events = self.check_signal()
                self._print_status(closed_bar_copy)
                for ev in events:
                    if ev["type"] == "OPEN":
                        self._print_signal(ev["signal"])
                    elif ev["type"] == "CLOSE":
                        self._print_close(ev["trade"])
            else:
                # 实时价格触及 SL/TP → 立即平仓 (不等收盘), 锁外执行
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

    def connect_aggtrade(self):
        self._ws_connect(WS_AGGTRADE_URL, self.on_aggtrade_message, "Aggtrade")

    def on_aggtrade_message(self, ws, msg):
        """大单成交回调: 筛选 ≥ LARGE_ORDER_USDT 的大单, 累加到 deque (高频, 轻量, 不持锁)"""
        try:
            d = json.loads(msg)
            if "p" not in d or "q" not in d:
                return
            price = float(d["p"]); qty = float(d["q"])
            usdt = price * qty
            if usdt >= LARGE_ORDER_USDT:
                side = "sell" if d.get("m") else "buy"   # m=true=买方是maker→卖方主动=sell
                order = {"ts": int(d.get("T", time.time()*1000)),
                         "price": round(price, 2),
                         "usdt": round(usdt, 0),
                         "side": side}
                self.large_orders.append(order)
                self.flow_window.append(order)
        except Exception:
            pass

    def _calc_flow_stats(self):
        """计算各窗口大单净买卖额 + 买卖比 (broadcast 时调用)"""
        now_ms = time.time() * 1000
        stats = []
        for mins in FLOW_WINDOWS:
            cutoff = now_ms - mins * 60 * 1000
            buys = sum(o["usdt"] for o in self.flow_window if o["ts"] >= cutoff and o["side"] == "buy")
            sells = sum(o["usdt"] for o in self.flow_window if o["ts"] >= cutoff and o["side"] == "sell")
            stats.append({"window": mins, "buy": round(buys, 0), "sell": round(sells, 0),
                          "net": round(buys - sells, 0),
                          "ratio": round(buys / sells, 2) if sells > 0 else 0})
        return stats

    def _poll_kline_rest(self):
        """REST 轮询 K 线 (fstream WS 被阻断, fapi REST 正常, 作为主数据源)

        ⚠️ 死锁防御: check_signal() → compute_indicators() 会再次获取 self.lock,
        threading.Lock 不可重入 → 必须在锁外调用 check_signal/_print_status/_print_signal。
        """
        last_heal = 0.0
        while self.running:
            try:
                # 自愈: 启动回补失败导致缓冲不足(只有几根K线) → 每30s重试拉全量历史
                # (limit=2 轮询只能维持最新2根, 不主动回补前端永远只有2根K线)
                if len(self.bars) < HISTORY_BARS and time.time() - last_heal >= 30:
                    last_heal = time.time()
                    self.fetch_history()
                data = self._rest_get(f"{REST_KLINE_URL}?symbol=ETHUSDT&interval=1h&limit=2")
                if not data:
                    time.sleep(5); continue

                # Phase 1: 锁内只更新 bars (不调用 check_signal, 避免死锁)
                closed_bar_copy = None   # 收盘K线副本, 供锁外信号检测用
                pending_new_bar = None   # 新K线(暂不追加, 等信号检测后再追加)
                need_signal_check = False
                with self.lock:
                    for k in data:
                        bar = {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                               "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                        if not self.bars:
                            self.bars.append(bar)
                        elif bar["t"] > self.bars[-1]["t"]:
                            # 新K线出现 → 前一根(self.bars[-1])刚收盘
                            # 暂不追加新K线, 让 check_signal 用收盘的那根计算
                            closed_bar_copy = dict(self.bars[-1])
                            pending_new_bar = bar
                            need_signal_check = True
                            self.bar_counter += 1
                        elif bar["t"] == self.bars[-1]["t"]:
                            # 当前K线更新
                            self.bars[-1] = bar

                # Phase 2: 锁外信号检测 (check_signal 内部自己加锁, 不死锁)
                if need_signal_check:
                    # self.bars[-1] 此时仍是收盘K线 (新K线未追加), check_signal 用它计算
                    events = self.check_signal()
                    self._print_status(closed_bar_copy)
                    for ev in events:
                        if ev["type"] == "OPEN":
                            self._print_signal(ev["signal"])
                        elif ev["type"] == "CLOSE":
                            self._print_close(ev["trade"])
                    # Phase 3: 信号检测后追加新K线
                    if pending_new_bar is not None:
                        with self.lock:
                            self.bars.append(pending_new_bar)
                            if len(self.bars) > HISTORY_BARS:
                                self.bars = self.bars[-HISTORY_BARS:]
                else:
                    # 当前K线更新 → 实时 SL/TP 检查 (锁外)
                    with self.lock:
                        cur_c = self.bars[-1]["c"] if self.bars else 0
                        cur_t = self.bars[-1]["t"] if self.bars else 0
                    self._check_realtime_sl_tp(cur_c, cur_t)
            except Exception as e:
                print(f"  ⚠️ [{now()}] REST K线轮询错误: {e}")
            time.sleep(5)

    def _poll_ticker_rest(self):
        """REST 轮询最新价格 (替代 fstream ticker WS)"""
        while self.running:
            try:
                data = self._rest_get(REST_TICKER_URL)
                if data and "price" in data:
                    self.last_price = float(data["price"])
            except Exception:
                pass
            time.sleep(2)

    def _poll_aggtrade_rest(self):
        """REST 轮询大单 (替代 fstream aggTrade WS, 5s 一次)"""
        last_id = 0
        while self.running:
            try:
                data = self._rest_get("https://fapi.binance.com/fapi/v1/aggTrades?symbol=ETHUSDT&limit=100")
                if data:
                    for t in data:
                        if t.get("a", 0) <= last_id:
                            continue
                        price = float(t["p"]); qty = float(t["q"])
                        usdt = price * qty
                        if usdt >= LARGE_ORDER_USDT:
                            side = "sell" if t.get("m") else "buy"
                            order = {"ts": int(t.get("T", time.time()*1000)),
                                     "price": round(price, 2), "usdt": round(usdt, 0), "side": side}
                            self.large_orders.append(order)
                            self.flow_window.append(order)
                    last_id = data[-1].get("a", last_id)
            except Exception:
                pass
            time.sleep(5)

    def _poll_long_short_ratio(self):
        """合约多空比轮询 (币安无 WS, 每 5 分钟 REST; 接口受限则降级跳过)"""
        while self.running:
            try:
                top = self._rest_get(REST_LSR_TOP_URL)
                acct = self._rest_get(REST_LSR_ACCT_URL)
                if top and acct and len(top) and len(acct):
                    t, a = top[-1], acct[-1]
                    self.long_short_ratio = {
                        "top_ratio": float(t["longShortRatio"]),
                        "top_long": float(t["longAccount"]), "top_short": float(t["shortAccount"]),
                        "acct_ratio": float(a["longShortRatio"]),
                        "acct_long": float(a["longAccount"]), "acct_short": float(a["shortAccount"]),
                        "ts": int(t["timestamp"]),
                    }
            except Exception:
                pass
            time.sleep(LSR_POLL_INTERVAL)

    def start(self):
        print(f"{'='*65}")
        print(f"  🎯 ETH 双ROC共振策略 — 合约({MARKET_NAME})实时模拟交易")
        print(f"  策略: ROC({ROC_SHORT})/ROC({ROC_MEDIUM})/ROC({ROC_LONG}) + VolMA({VOL_MA_PERIOD}) + MA({TREND_MA_PERIOD}) + ATR({ATR_PERIOD})")
        print(f"  做多: ROC{ROC_SHORT}>0 & ROC{ROC_MEDIUM}>0 & ROC{ROC_SHORT}>ROC{ROC_MEDIUM} & 放量 & 价>MA{TREND_MA_PERIOD} & ROC{ROC_LONG}>0 & ATR放大")
        print(f"  做空: 全镜像")
        print(f"  本金: {CAPITAL}U | 杠杆: {LEVERAGE}x | 仓位: {FRACTION_BASE*100:.0f}% (有效{LEVERAGE*FRACTION_BASE:.1f}x)")
        print(f"  止损: {SL_ATR_MULT}×ATR | 止盈: 动量衰竭(ROC{ROC_SHORT}穿零) | 超时: {MAX_HOLD_BARS}根")
        print(f"  数据源: {MARKET_NAME} | {WS_KLINE_URL}")
        print(f"  面板  : http://127.0.0.1:{SERVER_PORT} (合约)")
        print(f"{'='*65}")

        # 历史回补
        self.fetch_history()
        # 初始信号检测 (启动时 dry_run, 仅更新指标状态, 不开仓)
        self.check_signal(dry_run=True)
        if self.bars:
            self._print_status(self.bars[-1])

        # 启动数据线程
        # ⚠️ fstream.binance.com WS 被阻断(代理/直连均超时), 改用 fapi REST 轮询作为主数据源
        # WS 线程保留持续重连(fstream 恢复后自动接管低延迟模式); REST 轮询兜底保证数据更新
        threading.Thread(target=self.connect_kline, daemon=True, name="kline-ws").start()
        threading.Thread(target=self.connect_ticker, daemon=True, name="ticker-ws").start()
        threading.Thread(target=self.connect_aggtrade, daemon=True, name="aggtrade-ws").start()
        threading.Thread(target=self._poll_kline_rest, daemon=True, name="kline-rest").start()
        threading.Thread(target=self._poll_ticker_rest, daemon=True, name="ticker-rest").start()
        threading.Thread(target=self._poll_aggtrade_rest, daemon=True, name="aggtrade-rest").start()
        threading.Thread(target=self._poll_long_short_ratio, daemon=True, name="lsr-poll").start()
        print(f"  🐋 大资金流向: REST轮询(fstream WS阻断) + 多空比轮询已启动 (≥{LARGE_ORDER_USDT/10000:.0f}万U)")

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
        roc50_arr = calc_roc(closes, ROC_LONG)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        trend_ma_arr = calc_ma(closes, TREND_MA_PERIOD)
        atr_arr = calc_atr(highs, lows, closes, ATR_PERIOD)
        atr_state_arr = calc_ma(atr_arr, ATR_STATE_PERIOD)

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
        # (50期指标 warmup 内为 NaN, 此时回退缓存, 避免 JSON 序列化 500)
        if len(closes) and not np.isnan(roc5[-1]) and not np.isnan(vol_ma[-1]) \
                and not np.isnan(roc50_arr[-1]) and not np.isnan(trend_ma_arr[-1]):
            _price = round(float(closes[-1]), 2)
            _vol, _volma = float(vols[-1]), float(vol_ma[-1])
            _roc5, _roc20 = float(roc5[-1]), float(roc20[-1])
            _roc50 = float(roc50_arr[-1])
            _trend_ma = float(trend_ma_arr[-1])
            _atr = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0
            _atr_state = float(atr_state_arr[-1]) if not np.isnan(atr_state_arr[-1]) else 0
            indicator_state = {
                "price": _price,
                "roc5": round(_roc5, 4),
                "roc20": round(_roc20, 4),
                "roc50": round(_roc50, 4),
                "trend_ma": round(_trend_ma, 2),
                "volume": round(_vol, 2),
                "vol_ma": round(_volma, 2),
                "vol_ratio": round(_vol / _volma, 2) if _volma > 0 else 0,
                "atr": round(_atr, 2),
                "atr_state": round(_atr_state, 2),
                "ts": int(bars[-1]["t"]),
            }
            vol_ok = _vol > _volma
            atr_expanding = _atr > _atr_state
            long_roc = (_roc5 > 0 and _roc20 > 0 and _roc5 > _roc20
                        and _price > _trend_ma and _roc50 > 0)
            short_roc = (_roc5 < 0 and _roc20 < 0 and _roc5 < _roc20
                         and _price < _trend_ma and _roc50 < 0)
            signal_readiness = {
                "vol_confirmed": bool(vol_ok),
                "atr_expanding": bool(atr_expanding),
                "trend_up": bool(_price > _trend_ma),
                "trend_down": bool(_price < _trend_ma),
                "long_roc5": bool(_roc5 > 0),
                "long_roc20": bool(_roc20 > 0),
                "long_acc": bool(_roc5 > _roc20),
                "long_trend": bool(_price > _trend_ma),
                "long_roc50": bool(_roc50 > 0),
                "short_roc5": bool(_roc5 < 0),
                "short_roc20": bool(_roc20 < 0),
                "short_acc": bool(_roc5 < _roc20),
                "short_trend": bool(_price < _trend_ma),
                "short_roc50": bool(_roc50 < 0),
                "long_ready": bool(vol_ok and atr_expanding and long_roc),
                "short_ready": bool(vol_ok and atr_expanding and short_roc),
                "neutral": bool(not long_roc and not short_roc),
            }
        else:
            indicator_state = self.indicator_state
            signal_readiness = self.signal_readiness

        # 持仓视图 + 账户状态
        position_view = self._get_position_view()
        drawdown = (self.peak_balance - self.balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0
        return_pct = (self.balance - CAPITAL) / CAPITAL * 100

        # 趋势判断 + 支撑/突破位 + 布林带 (实时计算, 前端每次轮询拿到最新值)
        trend_state = analyze_trend(closes) if len(closes) >= 60 else \
            {"trend": "unknown", "label": "--", "ma_fast": None, "ma_slow": None,
             "spread_pct": None, "slope_pct": None}
        supports = find_supports(bars, closes)
        resistances = find_resistances(bars, closes)
        boll_upper, boll_mid, boll_lower = calc_boll(closes)

        return {
            "klines": klines,
            "roc5": series(roc5),
            "roc20": series(roc20),
            "vol_ma": series(vol_ma),
            "volma20": series(calc_ma(vols, 20)),
            "boll_upper": series(boll_upper),
            "boll_mid": series(boll_mid),
            "boll_lower": series(boll_lower),
            "signals": signals,
            "last_price": self.last_price or (round(float(closes[-1]), 2) if len(closes) else 0),
            "last_ts": int(bars[-1]["t"]) if bars else 0,
            "indicator_state": indicator_state,
            "signal_readiness": signal_readiness,
            "trend_state": trend_state,
            "supports": supports,
            "resistances": resistances,
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
            "large_orders": list(self.large_orders)[-30:],
            "flow_stats": self._calc_flow_stats(),
            "long_short_ratio": self.long_short_ratio,
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
<title>ETH 五维共振策略 · 合约 20x 实时信号面板</title>
<script src="/static/echarts.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;overflow:hidden}
.app-shell{display:flex;height:100vh}
.app-sidebar{width:190px;flex-shrink:0;background:#161b22;border-right:2px solid #30363d;display:flex;flex-direction:column;padding:16px 0;overflow-y:auto}
.app-sidebar .logo{font-size:15px;font-weight:700;color:#58a6ff;padding:0 18px 14px;border-bottom:1px solid #30363d;white-space:nowrap}
.app-sidebar .nav-group{margin-top:14px}
.app-sidebar .group-title{font-size:11px;color:#8b949e;padding:0 18px;margin-bottom:4px;letter-spacing:.5px}
.app-sidebar a{display:block;padding:8px 18px;color:#8b949e;text-decoration:none;font-size:13px;border-left:3px solid transparent;white-space:nowrap}
.app-sidebar a:hover{color:#f0f6fc;background:#21262d}
.app-sidebar a.active{color:#58a6ff;background:#1f6feb22;border-left-color:#1f6feb}
.app-main{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden}
.header{background:#161b22;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #30363d;gap:12px}
.header h1{font-size:18px;color:#58a6ff}
.price-display{font-size:28px;font-weight:bold;color:#f0f6fc}
.price-display .change{font-size:14px;margin-left:10px}
.layout{display:flex;flex:1;min-height:0}
.chart-area{flex:1;min-width:0}
#kline-chart{width:100%;height:60%}
#roc-chart{width:100%;height:40%}
.sidebar{width:340px;background:#161b22;border-left:2px solid #30363d;padding:15px;overflow-y:auto}
.section{margin-bottom:18px}
.section h3{font-size:13px;color:#8b949e;text-transform:uppercase;margin-bottom:8px;border-bottom:1px solid #30363d;padding-bottom:5px}
.indicator-row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px}
.indicator-row .label{color:#8b949e}
.indicator-row .value{font-weight:bold}
.trend-up{color:#3fb950;font-weight:bold}
.trend-down{color:#f85149;font-weight:bold}
.trend-side{color:#d29922;font-weight:bold}
.condition{display:flex;align-items:center;padding:6px 10px;margin:4px 0;border-radius:4px;font-size:12px}
.condition.met{background:#1a3a1a;color:#3fb950}
.condition.not-met{background:#3a1a1a;color:#f85149}
.condition .icon{margin-right:8px;font-size:16px}
.cond-group{border-radius:4px;padding:4px 8px;margin:8px 0;background:#0d1117}
.cond-group.long{border:1px solid #1f4a2a}
.cond-group.short{border:1px solid #4a1f1f}
.cond-group.ready{border-color:#3fb950}
.cond-group-title{font-size:12px;font-weight:bold;color:#8b949e;margin-bottom:2px}
.cond-group .condition{padding:4px 8px;margin:3px 0}
.signal-alert{padding:12px;border-radius:6px;margin:8px 0;font-weight:bold;font-size:14px;text-align:center}
.signal-alert.buy{background:#1a3a1a;border:2px solid #3fb950;color:#3fb950}
.signal-alert.sell{background:#3a1a1a;border:2px solid #f85149;color:#f85149}
.signal-alert.neutral{background:#1a1a2e;border:2px solid #30363d;color:#8b949e}
.order-form{display:flex;flex-direction:column;gap:8px}
.order-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.o-label{font-size:12px;color:#8b949e;white-space:nowrap}
.o-side{display:flex;gap:6px}
.o-btn{padding:4px 12px;border:1px solid #30363d;border-radius:6px;background:#161b22;color:#8b949e;cursor:pointer;font-size:12px}
.o-btn.buy.active{background:#238636;color:#fff;border-color:#238636}
.o-btn.sell.active{background:#da3633;color:#fff;border-color:#da3633}
.order-form select,.order-form input[type=number]{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:4px 6px;font-size:12px;width:110px}
.o-submit{width:100%;padding:8px;border:none;border-radius:6px;background:#2f81f7;color:#fff;font-size:13px;font-weight:600;cursor:pointer}
.o-submit:hover{background:#388bfd}
.o-msg{margin-top:6px;font-size:12px;color:#8b949e;word-break:break-all}
.o-msg.ok{color:#3fb950}
.o-msg.err{color:#f85149}
.signal-list{max-height:250px;overflow-y:auto}
.signal-item{display:flex;align-items:center;padding:6px 8px;margin:3px 0;border-radius:3px;font-size:12px;background:#21262d}
.signal-item.buy-signal{border-left:3px solid #3fb950}
.signal-item.sell-signal{border-left:3px solid #f85149}
.signal-item .dir{font-weight:bold;width:35px}
.signal-item .price{flex:1;text-align:right}
.signal-item .time{color:#8b949e;font-size:11px;margin-left:8px}
.signal-item.close-signal{border-left:3px solid #f0883e}
.signal-item .t-pnl{font-weight:bold}
.signal-item .t-pnl.pos{color:#3fb950}
.signal-item .t-pnl.neg{color:#f85149}
.signal-item .t-reason{color:#8b949e;font-size:11px}
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

<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports">📊 策略及回测</a>
      <a href="http://127.0.0.1:8082/testnet">🔌 Testnet</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="/" class="active">📊 信号</a>
      <a href="/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1><span class="live-dot"></span>ETH 五维共振策略 · 合约 20x</h1>
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

    <!-- 手动下单 -->
    <div class="section">
      <h3>🖐 手动下单</h3>
      <div class="order-form">
        <div class="order-row"><span class="o-label">方向</span>
          <div class="o-side">
            <button type="button" class="o-btn buy active" id="obuy">买入多</button>
            <button type="button" class="o-btn sell" id="osell">卖出空</button>
          </div>
        </div>
        <div class="order-row"><span class="o-label">类型</span>
          <select id="otype"><option value="market">市价单</option><option value="limit">限价单</option></select>
        </div>
        <div class="order-row" id="price-row" style="display:none"><span class="o-label">限价</span>
          <input type="number" id="oprice" step="0.01" min="0" placeholder="0.00">
        </div>
        <div class="order-row"><span class="o-label">金额USDT</span>
          <input type="number" id="ousdt" step="10" min="10" value="45">
        </div>
        <div class="order-row"><span class="o-label">杠杆</span>
          <select id="olev">
            <option value="1">1x</option><option value="2">2x</option>
            <option value="3">3x</option><option value="5">5x</option>
            <option value="10">10x</option><option value="20" selected>20x</option>
          </select>
        </div>
        <button type="button" class="o-submit" id="osubmit">🚀 下单</button>
        <div class="o-msg" id="omsg"></div>
      </div>
    </div>

    <!-- 入场条件 (五维共振逐条) -->
    <div class="section">
      <h3>📋 入场条件检查</h3>
      <div class="condition not-met" id="cond-vol">
        <span class="icon">⬜</span> 放量: 成交量 > VolMA(20)
      </div>
      <div class="condition not-met" id="cond-atr">
        <span class="icon">⬜</span> 波动放大: ATR(14) > ATR均值(50)
      </div>
      <div class="cond-group long" id="cond-long-group">
        <div class="cond-group-title">🟢 做多共振 (0/5)</div>
        <div class="condition not-met" id="cond-l-roc5"><span class="icon">⬜</span> ROC(8) &gt; 0</div>
        <div class="condition not-met" id="cond-l-roc20"><span class="icon">⬜</span> ROC(20) &gt; 0</div>
        <div class="condition not-met" id="cond-l-acc"><span class="icon">⬜</span> 加速: ROC(8) &gt; ROC(20)</div>
        <div class="condition not-met" id="cond-l-trend"><span class="icon">⬜</span> 价格 &gt; MA(50)</div>
        <div class="condition not-met" id="cond-l-roc50"><span class="icon">⬜</span> ROC(50) &gt; 0</div>
      </div>
      <div class="cond-group short" id="cond-short-group">
        <div class="cond-group-title">🔴 做空共振 (0/5)</div>
        <div class="condition not-met" id="cond-s-roc5"><span class="icon">⬜</span> ROC(8) &lt; 0</div>
        <div class="condition not-met" id="cond-s-roc20"><span class="icon">⬜</span> ROC(20) &lt; 0</div>
        <div class="condition not-met" id="cond-s-acc"><span class="icon">⬜</span> 加速: ROC(8) &lt; ROC(20)</div>
        <div class="condition not-met" id="cond-s-trend"><span class="icon">⬜</span> 价格 &lt; MA(50)</div>
        <div class="condition not-met" id="cond-s-roc50"><span class="icon">⬜</span> ROC(50) &lt; 0</div>
      </div>
    </div>

    <!-- 实时指标 -->
    <div class="section">
      <h3>📊 当前指标</h3>
      <div class="indicator-row"><span class="label">趋势</span><span class="value" id="ind-trend">--</span></div>
      <div class="indicator-row"><span class="label">支撑位</span><span class="value" id="ind-supports">--</span></div>
      <div class="indicator-row"><span class="label">突破位</span><span class="value" id="ind-resist">--</span></div>
      <div class="indicator-row"><span class="label">布林带 上/中/下</span><span class="value" id="ind-boll">--</span></div>
      <div class="indicator-row"><span class="label">ROC(8)</span><span class="value" id="ind-roc5">--</span></div>
      <div class="indicator-row"><span class="label">ROC(20)</span><span class="value" id="ind-roc20">--</span></div>
      <div class="indicator-row"><span class="label">ROC(50)</span><span class="value" id="ind-roc50">--</span></div>
      <div class="indicator-row"><span class="label">MA(50)</span><span class="value" id="ind-ma50">--</span></div>
      <div class="indicator-row"><span class="label">成交量</span><span class="value" id="ind-vol">--</span></div>
      <div class="indicator-row"><span class="label">VolMA(20)</span><span class="value" id="ind-volma">--</span></div>
      <div class="indicator-row"><span class="label">量比</span><span class="value" id="ind-volratio">--</span></div>
      <div class="indicator-row"><span class="label">ATR(14)</span><span class="value" id="ind-atr">--</span></div>
      <div class="indicator-row"><span class="label">ATR均值(50)</span><span class="value" id="ind-atr-state">--</span></div>
    </div>

    <!-- 策略参数 -->
    <div class="section">
      <h3>⚙️ 策略参数</h3>
      <div class="param-grid">
        <div class="param-item"><span class="p-label">本金</span><br><span class="p-val" id="p-capital">150U</span></div>
        <div class="param-item"><span class="p-label">杠杆</span><br><span class="p-val" id="p-leverage">20x</span></div>
        <div class="param-item"><span class="p-label">仓位</span><br><span class="p-val" id="p-fraction">20%</span></div>
        <div class="param-item"><span class="p-label">ROC 8/20/50</span><br><span class="p-val" id="p-roc">8/20/50</span></div>
        <div class="param-item"><span class="p-label">MA趋势</span><br><span class="p-val" id="p-ma">MA(50)</span></div>
        <div class="param-item"><span class="p-label">止损</span><br><span class="p-val" id="p-sl">1.5×ATR</span></div>
        <div class="param-item"><span class="p-label">止盈</span><br><span class="p-val" id="p-tp">关闭</span></div>
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
  const buyMarks = [], sellMarks = [], closeMarks = [];
  data.signals.forEach(s => {
    const idx = data.klines.findIndex(k => k[0] === s.ts);
    if (idx >= 0) {
      if (s.type === 'BUY') {
        buyMarks.push({coord: [dates[idx], data.klines[idx][3]], value: s.price});
      } else if (s.type === 'CLOSE') {
        closeMarks.push({coord: [dates[idx], data.klines[idx][2]], value: s.price});
      } else {
        sellMarks.push({coord: [dates[idx], data.klines[idx][2]], value: s.price});
      }
    }
  });

  // 成交量MA
  const volMaData = data.volma20;

  // 支撑位 + 突破位标记 (水平虚线 + 价格标签)
  const suppMarks = (data.supports||[]).map(s => ({
    yAxis: s.price,
    label: {formatter: '支撑 ' + s.price, position: 'insideEndTop',
            color: '#2f81f7', fontSize: 11},
    lineStyle: {color: '#2f81f7', type: 'dashed', width: 1.5},
  }));
  const resMarks = (data.resistances||[]).map(s => ({
    yAxis: s.price,
    label: {formatter: '突破 ' + s.price, position: 'insideEndBottom',
            color: '#f85149', fontSize: 11},
    lineStyle: {color: '#f85149', type: 'dashed', width: 1.5},
  }));

  // K线缩放: 滚轮/拖动(inside) + 底部滑块; 首次默认显示最近25%, 之后保留用户缩放
  const zoomOpts = [
    {type:'inside', xAxisIndex:[0,1]},
    {type:'slider', xAxisIndex:[0,1], bottom:4, height:16}
  ];
  // getOption() 在实例首次 setOption 前返回 undefined, 需判空
  const curOpt = klineChart.getOption();
  if (!curOpt || !curOpt.dataZoom) {
    zoomOpts.forEach(z => { z.start = 75; z.end = 100; });
  }

  klineChart.setOption({
    grid: [{left:'8%',right:'3%',top:'5%',height:'55%'},
           {left:'8%',right:'3%',top:'68%',height:'25%'}],
    xAxis: [{type:'category',data:dates,gridIndex:0,axisLabel:{show:false}},
            {type:'category',data:dates,gridIndex:1,axisLabel:{fontSize:10}}],
    yAxis: [{type:'value',gridIndex:0,scale:true,splitArea:{show:true}},
            {type:'value',gridIndex:1}],
    dataZoom: zoomOpts,
    series: [
      {name:'K线',type:'candlestick',data:kdata,xAxisIndex:0,yAxisIndex:0,
       itemStyle:{color:'#3fb950',color0:'#f85149',borderColor:'#3fb950',borderColor0:'#f85149'},
       markLine:{silent:true,symbol:'none',animation:false,data:suppMarks.concat(resMarks)}},
      {name:'BOLL上轨',type:'line',data:data.boll_upper,xAxisIndex:0,yAxisIndex:0,
       symbol:'none',lineStyle:{width:1,color:'#8b949e',opacity:0.7},z:2},
      {name:'BOLL中轨',type:'line',data:data.boll_mid,xAxisIndex:0,yAxisIndex:0,
       symbol:'none',lineStyle:{width:1,color:'#2f81f7',opacity:0.8},z:2},
      {name:'BOLL下轨',type:'line',data:data.boll_lower,xAxisIndex:0,yAxisIndex:0,
       symbol:'none',lineStyle:{width:1,color:'#8b949e',opacity:0.7},z:2},
      {name:'买点',type:'scatter',data:buyMarks,xAxisIndex:0,yAxisIndex:0,
       symbol:'triangle',symbolSize:12,itemStyle:{color:'#3fb950'}},
      {name:'卖点',type:'scatter',data:sellMarks,xAxisIndex:0,yAxisIndex:0,
       symbol:'triangle',symbolSize:12,symbolRotate:180,itemStyle:{color:'#f85149'}},
      {name:'平仓点',type:'scatter',data:closeMarks,xAxisIndex:0,yAxisIndex:0,
       symbol:'circle',symbolSize:9,itemStyle:{color:'#f0883e'}},
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

  // 指标 (五维共振全指标)
  const s = data.indicator_state || {};
  document.getElementById('ind-roc5').textContent = (s.roc5||0).toFixed(4) + '%';
  document.getElementById('ind-roc20').textContent = (s.roc20||0).toFixed(4) + '%';
  document.getElementById('ind-roc50').textContent = (s.roc50||0).toFixed(4) + '%';
  document.getElementById('ind-ma50').textContent = (s.trend_ma||0).toFixed(1);
  document.getElementById('ind-vol').textContent = (s.volume||0).toFixed(1);
  document.getElementById('ind-volma').textContent = (s.vol_ma||0).toFixed(1);
  document.getElementById('ind-volratio').textContent = (s.vol_ratio||0).toFixed(2) + 'x';
  document.getElementById('ind-atr').textContent = (s.atr||0).toFixed(2);

  // 趋势 + 支撑位
  const tr = data.trend_state || {};
  const trendEl = document.getElementById('ind-trend');
  if (tr.label && tr.trend !== 'unknown') {
    const tcls = tr.trend === 'up' ? 'trend-up' : (tr.trend === 'down' ? 'trend-down' : 'trend-side');
    const tico = tr.trend === 'up' ? '📈' : (tr.trend === 'down' ? '📉' : '↔️');
    trendEl.innerHTML = `<span class="${tcls}">${tico} ${tr.label}</span>`;
  } else {
    trendEl.textContent = '--';
  }
  const supps = data.supports || [];
  document.getElementById('ind-supports').textContent =
    supps.length ? supps.map(x => x.price).join(' / ') : '--';
  const reses = data.resistances || [];
  document.getElementById('ind-resist').textContent =
    reses.length ? reses.map(x => x.price).join(' / ') : '--';
  const bUp = data.boll_upper, bMid = data.boll_mid, bLo = data.boll_lower;
  if (bUp && bMid && bLo && bUp.length && bUp[bUp.length-1] != null) {
    document.getElementById('ind-boll').textContent =
      `${bUp[bUp.length-1].toFixed(2)} / ${bMid[bMid.length-1].toFixed(2)} / ${bLo[bLo.length-1].toFixed(2)}`;
  } else {
    document.getElementById('ind-boll').textContent = '--';
  }
  document.getElementById('ind-atr-state').textContent = (s.atr_state||0).toFixed(2);

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
    alertDiv.innerHTML = '📈 <b>做多信号就绪！</b><br>ROC(8)>0 & ROC(20)>0 & 放量 & 价>MA50 & ROC50>0 & ATR放大';
  } else if (rdy.short_ready) {
    alertDiv.className += ' sell';
    alertDiv.innerHTML = '📉 <b>做空信号就绪！</b><br>ROC(8)<0 & ROC(20)<0 & 放量 & 价<MA50 & ROC50<0 & ATR放大';
  } else {
    alertDiv.className += ' neutral';
    alertDiv.innerHTML = '⚪ 等待入场信号<br><small>条件未全部满足</small>';
  }

  // 条件检查 (五维共振逐条)
  updateCondition('cond-vol', rdy.vol_confirmed);
  updateCondition('cond-atr', rdy.atr_expanding);
  // 做多逐维度
  updateCondition('cond-l-roc5', rdy.long_roc5);
  updateCondition('cond-l-roc20', rdy.long_roc20);
  updateCondition('cond-l-acc', rdy.long_acc);
  updateCondition('cond-l-trend', rdy.long_trend);
  updateCondition('cond-l-roc50', rdy.long_roc50);
  updateCondGroup('cond-long-group', 'long', [rdy.long_roc5, rdy.long_roc20, rdy.long_acc, rdy.long_trend, rdy.long_roc50]);
  // 做空逐维度
  updateCondition('cond-s-roc5', rdy.short_roc5);
  updateCondition('cond-s-roc20', rdy.short_roc20);
  updateCondition('cond-s-acc', rdy.short_acc);
  updateCondition('cond-s-trend', rdy.short_trend);
  updateCondition('cond-s-roc50', rdy.short_roc50);
  updateCondGroup('cond-short-group', 'short', [rdy.short_roc5, rdy.short_roc20, rdy.short_acc, rdy.short_trend, rdy.short_roc50]);

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

  // 历史信号列表 (BUY/SELL 开仓 + CLOSE 平仓)
  const listDiv = document.getElementById('signal-list');
  const signals = data.signals || [];
  if (signals.length !== currentSignals.length) {
    currentSignals = signals;
    listDiv.innerHTML = signals.slice().reverse().map(s => {
      const d = new Date(s.ts + 3600000); // 信号触发(收盘)时刻=开盘+1h, 与微信通知一致
      const t = d.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      if (s.type === 'CLOSE') {
        const dirTxt = s.direction === 'long' ? '📈多' : '📉空';
        const pnl = s.pnl || 0;
        const pnlCls = pnl >= 0 ? 'pos' : 'neg';
        const reasonMap = {'momentum_death':'动量衰竭','SL':'止损','TP':'止盈','timeout':'超时','force_close':'强平'};
        return `<div class="signal-item close-signal">
          <span class="dir">🔚${dirTxt}</span>
          <span class="price">$${s.entry_price}→$${s.price.toFixed(2)}</span>
          <span class="t-pnl ${pnlCls}">${pnl>=0?'+':''}${pnl.toFixed(2)}U</span>
          <span class="t-reason">${reasonMap[s.reason]||s.reason}·${s.held_bars}根</span>
          <span class="time">${t}</span>
        </div>`;
      }
      const cls = s.type === 'BUY' ? 'buy-signal' : 'sell-signal';
      const dir = s.type === 'BUY' ? '📈多' : '📉空';
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

function updateCondGroup(groupId, side, states) {
  // 多/空共振分组: 更新标题计数 + 全满足时高亮边框
  const el = document.getElementById(groupId);
  const metCount = states.filter(Boolean).length;
  const label = side === 'long' ? '🟢 做多共振' : '🔴 做空共振';
  el.querySelector('.cond-group-title').textContent = `${label} (${metCount}/5)`;
  el.className = 'cond-group ' + side + (metCount === 5 ? ' ready' : '');
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
  initManualOrder();
  window.onresize = () => { klineChart?.resize(); rocChart?.resize(); };
};

// ===== 手动下单 =====
function initManualOrder() {
  let oSide = 'buy';
  const obuy = document.getElementById('obuy'), osell = document.getElementById('osell');
  const omsg = document.getElementById('omsg');
  obuy.addEventListener('click', () => { oSide='buy'; obuy.classList.add('active'); osell.classList.remove('active'); });
  osell.addEventListener('click', () => { oSide='sell'; osell.classList.add('active'); obuy.classList.remove('active'); });
  document.getElementById('otype').addEventListener('change', (e) => {
    document.getElementById('price-row').style.display = e.target.value === 'limit' ? '' : 'none';
  });
  document.getElementById('osubmit').addEventListener('click', async () => {
    const body = {
      side: oSide,
      order_type: document.getElementById('otype').value,
      leverage: parseInt(document.getElementById('olev').value),
      usdt: parseFloat(document.getElementById('ousdt').value),
    };
    if (body.order_type === 'limit') {
      body.price = parseFloat(document.getElementById('oprice').value);
      if (!body.price || body.price <= 0) { omsg.className='o-msg err'; omsg.textContent='请填写有效限价'; return; }
    }
    if (!body.usdt || body.usdt <= 0) { omsg.className='o-msg err'; omsg.textContent='请填写下单金额'; return; }
    omsg.className='o-msg'; omsg.textContent='下单中...';
    try {
      const r = await fetch('/api/manual_order', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
      });
      const j = await r.json();
      omsg.className = 'o-msg ' + (j.ok ? 'ok' : 'err');
      omsg.textContent = j.message || JSON.stringify(j);
      if (j.ok) setTimeout(() => { omsg.className='o-msg'; omsg.textContent=''; }, 8000);
    } catch (e) {
      omsg.className='o-msg err'; omsg.textContent='请求失败: ' + e.message;
    }
  });
}
</script>
</body>
</html>'''


# ==================== FastAPI ====================
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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


HTML_PAGE_FLOW = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETH 大资金流向 — 实时监控 (合约)</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; }
.header { display: flex; align-items: center; gap: 16px; padding: 10px 20px; background: #161b22; border-bottom: 1px solid #30363d; height: 50px; }
.header h1 { font-size: 16px; font-weight: 600; color: #f0f6fc; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: #3fb950; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.nav { display: flex; gap: 4px; }
.nav a { padding: 5px 12px; color: #8b949e; text-decoration: none; border-radius: 4px; font-size: 13px; }
.nav a:hover { background: #21262d; color: #f0f6fc; }
.nav a.active { background: #1f6feb; color: #fff; }
.price-display { margin-left: auto; font-size: 16px; font-weight: 700; color: #3fb950; }
.container { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
.section-title { font-size: 14px; font-weight: 600; color: #f0f6fc; margin: 16px 0 8px; padding-bottom: 6px; border-bottom: 1px solid #30363d; }
.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
.stat-card .window { font-size: 12px; color: #8b949e; margin-bottom: 6px; }
.stat-card .net { font-size: 24px; font-weight: 700; }
.stat-card .net.pos { color: #3fb950; }
.stat-card .net.neg { color: #f85149; }
.stat-card .detail { font-size: 11px; color: #8b949e; margin-top: 8px; display: flex; justify-content: space-between; }
.stat-card .detail .buy { color: #3fb950; }
.stat-card .detail .sell { color: #f85149; }
.order-list { background: #161b22; border: 1px solid #30363d; border-radius: 8px; max-height: 520px; overflow-y: auto; }
.order-list:empty::before { content: "⏳ 等待大单成交 (单笔 ≥ 10万U)..."; display: block; padding: 30px; text-align: center; color: #8b949e; }
.order-item { display: flex; align-items: center; gap: 10px; padding: 7px 14px; border-bottom: 1px solid #21262d; font-size: 13px; }
.order-item.buy { border-left: 3px solid #3fb950; }
.order-item.sell { border-left: 3px solid #f85149; }
.order-item .side { width: 48px; font-weight: 600; }
.order-item.buy .side { color: #3fb950; }
.order-item.sell .side { color: #f85149; }
.order-item .usdt { flex: 1; text-align: right; font-weight: 600; color: #f0f6fc; }
.order-item .price { color: #8b949e; width: 90px; text-align: right; }
.order-item .time { color: #6e7681; width: 70px; text-align: right; font-size: 11px; }
.lsr-box { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-top: 12px; display: none; }
.lsr-row { display: flex; justify-content: space-between; padding: 5px 0; font-size: 13px; border-bottom: 1px solid #21262d; }
.lsr-row:last-child { border-bottom: none; }
.lsr-row .label { color: #8b949e; }
.lsr-row .value { color: #f0f6fc; font-weight: 600; }
.empty-hint { color: #6e7681; font-size: 12px; padding: 8px 0; }
.app-shell{display:flex;min-height:100vh}
.app-sidebar{width:190px;flex-shrink:0;background:#161b22;border-right:2px solid #30363d;display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;height:100vh;overflow-y:auto}
.app-sidebar .logo{font-size:15px;font-weight:700;color:#58a6ff;padding:0 18px 14px;border-bottom:1px solid #30363d;white-space:nowrap}
.app-sidebar .nav-group{margin-top:14px}
.app-sidebar .group-title{font-size:11px;color:#8b949e;padding:0 18px;margin-bottom:4px;letter-spacing:.5px}
.app-sidebar a{display:block;padding:8px 18px;color:#8b949e;text-decoration:none;font-size:13px;border-left:3px solid transparent;white-space:nowrap}
.app-sidebar a:hover{color:#f0f6fc;background:#21262d}
.app-sidebar a.active{color:#58a6ff;background:#1f6feb22;border-left-color:#1f6feb}
.app-main{flex:1;min-width:0}
</style>
</head>
<body>

<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports">📊 策略及回测</a>
      <a href="http://127.0.0.1:8082/testnet">🔌 Testnet</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="/">📊 信号</a>
      <a href="/flow" class="active">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1><span class="live-dot"></span>ETH 大资金流向 — 实时监控 (合约)</h1>
      <div class="price-display" id="price-display">--</div>
    </div>

<div class="container">
  <div class="section-title">📊 大单净买卖统计</div>
  <div class="stats-grid" id="stats-grid">
    <div class="empty-hint">连接中...</div>
  </div>

  <div class="lsr-box" id="lsr-box">
    <div class="section-title" style="margin-top:0">👥 大户多空比</div>
    <div id="lsr-content"></div>
  </div>

  <div class="section-title">📋 大单滚动 (单笔 ≥ 10万U)</div>
  <div class="order-list" id="order-list"></div>
</div>
  </div>
</div>

<script>
const ws = new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws');
ws.onopen = () => console.log('WS connected');
ws.onmessage = (e) => {
  let msg;
  try { msg = JSON.parse(e.data); } catch(_) { return; }
  if (msg.type !== 'init' && msg.type !== 'update') return;
  const d = msg.data || {};

  if (d.last_price) document.getElementById('price-display').textContent = '$' + d.last_price;

  const grid = document.getElementById('stats-grid');
  const stats = d.flow_stats || [];
  if (stats.length) {
    grid.innerHTML = stats.map(s => {
      const pos = s.net >= 0;
      const cls = pos ? 'pos' : 'neg';
      const sign = pos ? '+' : '';
      return '<div class="stat-card">' +
        '<div class="window">' + s.window + ' 分钟净买卖</div>' +
        '<div class="net ' + cls + '">' + sign + (s.net/10000).toFixed(1) + '万U</div>' +
        '<div class="detail">' +
          '<span class="buy">买 ' + (s.buy/10000).toFixed(1) + '万</span>' +
          '<span>比 ' + s.ratio + 'x</span>' +
          '<span class="sell">卖 ' + (s.sell/10000).toFixed(1) + '万</span>' +
        '</div></div>';
    }).join('');
  }

  const lsrBox = document.getElementById('lsr-box');
  if (d.long_short_ratio) {
    lsrBox.style.display = 'block';
    const r = d.long_short_ratio;
    document.getElementById('lsr-content').innerHTML =
      '<div class="lsr-row"><span class="label">大户持仓多空比</span><span class="value">' + r.top_ratio.toFixed(3) + ' (多 ' + (r.top_long*100).toFixed(1) + '% / 空 ' + (r.top_short*100).toFixed(1) + '%)</span></div>' +
      '<div class="lsr-row"><span class="label">账户多空比</span><span class="value">' + r.acct_ratio.toFixed(3) + ' (多 ' + (r.acct_long*100).toFixed(1) + '% / 空 ' + (r.acct_short*100).toFixed(1) + '%)</span></div>';
  } else {
    lsrBox.style.display = 'none';
  }

  const list = document.getElementById('order-list');
  const orders = d.large_orders || [];
  list.innerHTML = orders.slice().reverse().map(o => {
    const cls = o.side === 'buy' ? 'buy' : 'sell';
    const emoji = o.side === 'buy' ? '🟢买' : '🔴卖';
    const dt = new Date(o.ts);
    const t = dt.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    return '<div class="order-item ' + cls + '">' +
      '<span class="side">' + emoji + '</span>' +
      '<span class="usdt">' + (o.usdt/10000).toFixed(1) + '万U</span>' +
      '<span class="price">$' + o.price + '</span>' +
      '<span class="time">' + t + '</span>' +
    '</div>';
  }).join('');
};
ws.onclose = () => { document.getElementById('price-display').textContent = '连接断开, 重连中...'; };
</script>
</body>
</html>
"""


app = FastAPI(title="ETH v12 合约实时交易信号", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)


@app.get("/flow")
async def flow_page():
    return HTMLResponse(HTML_PAGE_FLOW)


@app.get("/api/data")
async def api_data():
    return JSONResponse(trader.get_chart_data())


@app.post("/api/manual_order")
async def api_manual_order(req: Request):
    """手动下单(模拟): body = {side: buy|sell, order_type: market|limit, price?, usdt, leverage?}"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效的请求体"}, status_code=400)
    side = str(body.get("side", "")).lower()
    otype = str(body.get("order_type", "")).lower()
    price = body.get("price")
    usdt = body.get("usdt")
    lev = int(body.get("leverage") or LEVERAGE)
    if side not in ("buy", "sell"):
        return JSONResponse({"ok": False, "message": "方向必须为 buy 或 sell"})
    if otype not in ("market", "limit"):
        return JSONResponse({"ok": False, "message": "类型必须为 market 或 limit"})
    if otype == "limit" and (price is None or float(price) <= 0):
        return JSONResponse({"ok": False, "message": "限价单必须填写有效价格"})
    if usdt is None or float(usdt) <= 0:
        return JSONResponse({"ok": False, "message": "请填写下单金额(USDT)"})
    usdt = float(usdt)
    exec_price = trader.last_price if otype == "market" else float(price)
    if not exec_price or exec_price <= 0:
        return JSONResponse({"ok": False, "message": "当前价格不可用, 请稍后再试"})
    result = trader.manual_order(side, exec_price, usdt, lev, int(time.time() * 1000))
    return JSONResponse(result)


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
        f"- **K线时间(开盘)**: {ts_to_str(signal['ts'])}\n"
        f"- **信号触发(收盘)**: {now()}\n"
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
        f"- **K线时间(开盘)**: {ts_to_str(trade['exit_time'])}\n"
        f"- **信号触发(收盘)**: {now()}\n"
        f"- **余额**: {trader.balance:.2f} USDT\n"
        f"\n> ETH 双ROC动量策略 · 合约实时交易"
    )
    threading.Thread(target=wx_notify, args=(title, content), daemon=True).start()
trader._print_close = _print_close_with_broadcast


def _check_single_instance():
    """单实例守卫: 开发环境偶尔把一条命令拉起两个进程(克隆), 会抢同一 8081 端口互相冲突。
    用 Windows 命名互斥量让后启动的克隆自动退出 (必须 use_last_error=True, 直接调
    GetLastError 会被 ctypes 内部调用覆盖导致失效)"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateMutexW = k32.CreateMutexW
    CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    CreateMutexW.restype = wintypes.HANDLE
    for name in ("Local\\live_trader_contract_single", "Global\\live_trader_contract_single"):
        h = CreateMutexW(None, True, name)
        if not h:
            continue
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            print(f"[{time.strftime('%H:%M:%S')}] 已有 live_trader_contract 实例运行, 本实例退出")
            sys.exit(0)
        return h  # 保持引用, 防止句柄被 GC 释放


if __name__ == "__main__":
    _hold = _check_single_instance()  # 单实例守卫: 克隆进程启动即退出, 防止抢 8081 端口
    import uvicorn
    daily_log.setup("contract")  # 控制台 + logs/contract/<当天日期>.log
    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")
