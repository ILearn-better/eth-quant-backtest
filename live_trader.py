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
# 大单成交流 (实时聚合交易, 用于大资金流向)
WS_AGGTRADE_URL = "wss://stream.binance.com:9443/ws/ethusdt@aggTrade"
# 大资金流向参数
LARGE_ORDER_USDT = 100000   # 单笔成交额 ≥ 10万U 算大单
FLOW_WINDOWS = [5, 15, 60]  # 净买卖统计窗口(分钟)
LARGE_ORDER_KEEP = 50       # 后端保留大单条数(deque maxlen)
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

        # 持仓状态 (与回测策略 eth_roc_momentum_v12.py 逻辑一致)
        self.position = None         # None=空仓, dict=持仓(entry_price/direction/entry_ts/entry_bar/size_usdt)
        self.bar_count = 0           # K线收盘计数(用于冷却期计算)
        self.last_close_bar = -999   # 上次平仓的K线序号
        COOLDOWN_BARS = 3            # 平仓后冷却3根K线再入场

        # 大资金流向 (aggTrade 大单聚合, 独立线程高频更新)
        from collections import deque
        self.large_orders = deque(maxlen=LARGE_ORDER_KEEP)   # 大单滚动列表(前端展示)
        self.flow_window = deque(maxlen=5000)                # 统计窗口(留足60min大单)
        self.long_short_ratio = None                         # 现货无多空比数据, 恒 None

        # 启动时恢复历史信号 + 持仓状态(避免重启丢失信号 / 持仓断链)
        self._load_state()

    # ---- 持久化: 信号 JSONL + 持仓状态 JSON ----
    def _signal_file(self):
        return os.path.join(BASE_DIR, "data", "signals_spot.jsonl")

    def _state_file(self):
        return os.path.join(BASE_DIR, "data", "state_spot.json")

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
            "bar_count": self.bar_count,
            "last_close_bar": self.last_close_bar,
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
            print(f"  📂 已加载 {len(self.signal_log)} 条历史信号 (data/signals_spot.jsonl)")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  ⚠️ 加载历史信号失败: {e}")
        # 2. 持仓状态
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                st = json.load(f)
            if st.get("position"):
                self.position = st["position"]
                self.bar_count = st.get("bar_count", 0)
                self.last_close_bar = st.get("last_close_bar", -999)
                p = self.position
                print(f"  ⚠️ 检测到未平仓位: {p['direction']} @ {p['entry_price']} USDT")
                print(f"  ⚠️ 已自动恢复持仓跟踪。如已手动平仓, 请删除 data/state_spot.json 后重启")
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
        vols = np.array([b["v"] for b in bars])
        roc5 = calc_roc(closes, ROC_SHORT)
        roc20 = calc_roc(closes, ROC_MEDIUM)
        vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        return closes, vols, roc5, roc20, vol_ma

    # ---- 信号检测 ----
    def check_signal(self):
        """基于最新收盘K线检测信号 (与回测策略逻辑一致: 先出场再入场)

        信号流: BUY(入场) → 持仓中(不发信号) → CLOSE(平仓) → 冷却期 → 空仓 → 等下一个入场
        """
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

        self.bar_count += 1

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

        # 入场条件检查 (用于面板显示, 同时用于触发原因追溯)
        vol_ok = cur_vol > cur_vol_ma
        roc5_pos = cur_roc5 > 0
        roc20_pos = cur_roc20 > 0
        roc_accel = cur_roc5 > cur_roc20   # 短期加速: 做多时true, 做空时short_accel
        long_roc = roc5_pos and roc20_pos and roc_accel
        short_roc = (not roc5_pos) and (not roc20_pos) and (not roc_accel)
        vol_ratio_val = round(float(cur_vol / cur_vol_ma), 2) if cur_vol_ma > 0 else 0
        cond_dict = {
            "vol_ok": bool(vol_ok),
            "roc5_pos": bool(roc5_pos),
            "roc20_pos": bool(roc20_pos),
            "roc_accel": bool(roc_accel),
        }

        self.signal_readiness = {
            "vol_confirmed": bool(vol_ok),
            "long_ready": bool(vol_ok and long_roc),
            "short_ready": bool(vol_ok and short_roc),
            "neutral": bool(not long_roc and not short_roc),
        }

        signal = None

        # ============================================================
        # 1. 有持仓 → 先检查出场条件 (动量衰竭 / 硬止损 / 超时)
        # ============================================================
        if self.position is not None:
            pos = self.position
            held_bars = self.bar_count - pos["entry_bar"]
            should_close = False
            reason = ""

            # 动量衰竭: 做多时 ROC(8)<0, 做空时 ROC(8)>0
            if pos["direction"] == "long" and cur_roc5 < 0:
                should_close = True
                reason = "momentum_death"
            elif pos["direction"] == "short" and cur_roc5 > 0:
                should_close = True
                reason = "momentum_death"

            # 硬止损: 单笔亏损 >= SL_USDT
            if pos["direction"] == "long":
                pnl = (price - pos["entry_price"]) / pos["entry_price"] * pos["size_usdt"]
            else:
                pnl = (pos["entry_price"] - price) / pos["entry_price"] * pos["size_usdt"]
            if pnl <= -SL_USDT:
                should_close = True
                reason = "SL"

            # 超时: 持仓 >= MAX_HOLD_BARS
            if held_bars >= MAX_HOLD_BARS:
                should_close = True
                reason = "timeout"

            if should_close:
                reason_map = {"momentum_death": "动量衰竭", "SL": "硬止损", "timeout": "超时平仓"}
                reason_text = reason_map.get(reason, reason)
                # 平仓原因详细描述
                if reason == "momentum_death":
                    if pos["direction"] == "long":
                        desc = f"做多时ROC({ROC_SHORT})={round(float(cur_roc5),2)}% 跌破0, 动量衰竭"
                    else:
                        desc = f"做空时ROC({ROC_SHORT})={round(float(cur_roc5),2)}% 突破0, 动量衰竭"
                elif reason == "SL":
                    desc = f"单笔亏损{round(float(pnl),2)}U >= 止损{SL_USDT}U"
                elif reason == "timeout":
                    desc = f"持仓{held_bars}根K线 >= 最大持仓{MAX_HOLD_BARS}根"
                else:
                    desc = reason_text

                signal = {
                    "type": "CLOSE",
                    "direction": pos["direction"],
                    "price": round(float(price), 2),
                    "ts": int(ts),
                    "roc5": round(float(cur_roc5), 2),
                    "roc20": round(float(cur_roc20), 2),
                    "reason": reason,
                    "reason_desc": desc,
                    "pnl": round(float(pnl), 4),
                    "entry_price": round(float(pos["entry_price"]), 2),
                    "held_bars": held_bars,
                }
                self.position = None
                self.last_close_bar = self.bar_count
                self.current_signal = None
                self._save_state()                    # 持仓状态落盘

        # ============================================================
        # 2. 空仓 → 检查入场条件 (需过冷却期)
        # ============================================================
        elif self.bar_count - self.last_close_bar >= 3:  # 冷却3根K线
            if vol_ok:
                if long_roc:
                    signal = {"type": "BUY", "direction": "long", "price": round(float(price), 2),
                              "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                              "roc20": round(float(cur_roc20), 2),
                              "conds": cond_dict, "vol_ratio": vol_ratio_val}
                    self.current_signal = "long"
                    # 记录持仓
                    notional = CAPITAL * FRACTION_BASE * LEVERAGE
                    self.position = {
                        "direction": "long",
                        "entry_price": float(price),
                        "entry_ts": int(ts),
                        "entry_bar": self.bar_count,
                        "size_usdt": notional,
                    }
                    self._save_state()                # 持仓状态落盘
                elif short_roc:
                    signal = {"type": "SELL", "direction": "short", "price": round(float(price), 2),
                              "ts": int(ts), "roc5": round(float(cur_roc5), 2),
                              "roc20": round(float(cur_roc20), 2),
                              "conds": cond_dict, "vol_ratio": vol_ratio_val}
                    self.current_signal = "short"
                    notional = CAPITAL * FRACTION_BASE * LEVERAGE
                    self.position = {
                        "direction": "short",
                        "entry_price": float(price),
                        "entry_ts": int(ts),
                        "entry_bar": self.bar_count,
                        "size_usdt": notional,
                    }
                    self._save_state()                # 持仓状态落盘

        if signal:
            signal["received_at"] = now()           # 本地触发时间, 排查延迟用
            self.signal_log.append(signal)
            self.signal_log = self.signal_log[-1000:]   # 50 → 1000
            self._append_signal_jsonl(signal)            # 落盘 JSONL

        return signal

    def manual_order(self, side, exec_price, usdt, leverage, ts):
        """手动下单(模拟): 空仓→开仓; 反方向→平仓. 返回 {"ok": bool, "message": str}"""
        direction = "long" if side == "buy" else "short"
        cn_dir = "多" if direction == "long" else "空"
        with self.lock:
            if self.position is not None:
                pos = self.position
                if pos["direction"] == direction:
                    return {"ok": False, "message": f"已有同方向持仓(做{cn_dir}), 请先平仓或反向下单"}
                # 反方向 → 平仓 (盈亏按持仓方向计算, 而非新单方向)
                if pos["direction"] == "long":
                    pnl = (exec_price - pos["entry_price"]) / pos["entry_price"] * pos["size_usdt"]
                else:
                    pnl = (pos["entry_price"] - exec_price) / pos["entry_price"] * pos["size_usdt"]
                signal = {
                    "type": "CLOSE", "direction": pos["direction"],
                    "price": round(float(exec_price), 2), "ts": int(ts),
                    "roc5": None, "roc20": None,
                    "reason": "manual", "reason_desc": f"手动平仓(反向做{cn_dir})",
                    "pnl": round(float(pnl), 4),
                    "entry_price": round(float(pos["entry_price"]), 2),
                    "held_bars": self.bar_count - pos["entry_bar"],
                    "received_at": now(),
                }
                self.signal_log.append(signal)
                self.signal_log = self.signal_log[-1000:]
                self._append_signal_jsonl(signal)
                self.position = None
                self.current_signal = None
                self.last_close_bar = self.bar_count
                self._save_state()
                return {"ok": True, "message": f"平仓成功(做{cn_dir}平), 成交 {exec_price:.2f}, 盈亏 {pnl:+.2f}U"}
            # 空仓 → 开仓
            self.position = {
                "direction": direction,
                "entry_price": float(exec_price),
                "entry_ts": int(ts),
                "entry_bar": self.bar_count,
                "size_usdt": round(float(usdt), 2),
                "leverage": int(leverage),
                "manual": True,
            }
            signal = {
                "type": "BUY" if direction == "long" else "SELL",
                "direction": direction,
                "price": round(float(exec_price), 2), "ts": int(ts),
                "roc5": None, "roc20": None,
                "reason": "manual", "reason_desc": f"手动开仓(做{cn_dir}, 杠杆{int(leverage)}x)",
                "manual": True, "received_at": now(),
            }
            self.signal_log.append(signal)
            self.signal_log = self.signal_log[-1000:]
            self._append_signal_jsonl(signal)
            self._save_state()
            return {"ok": True, "message": f"开仓成功(做{cn_dir}), 成交 {exec_price:.2f}, 名义 {usdt:.0f}U @ {int(leverage)}x"}

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
        sig_type = signal["type"]

        if sig_type == "CLOSE":
            # 平仓信号
            direction = signal["direction"]
            pnl = signal["pnl"]
            pnl_emoji = "💰" if pnl >= 0 else "💸"
            reason_map = {"momentum_death": "动量衰竭", "SL": "硬止损", "timeout": "超时平仓"}
            reason_text = reason_map.get(signal["reason"], signal["reason"])
            border = "🔵" * 20

            print(f"\n{border}")
            print(f"  🔚 🔚 🔚  *** CLOSE 平仓信号 — {reason_text} ***  🔚 🔚 🔚")
            print(f"  ═══════════════════════════════════════════════════════")
            print(f"  方向: {'做多' if direction == 'long' else '做空'}")
            print(f"  入场价: {signal['entry_price']} USDT  →  平仓价: {signal['price']} USDT")
            print(f"  {pnl_emoji} 盈亏: {pnl:+.4f} USDT")
            print(f"  持仓: {signal['held_bars']} 根K线")
            print(f"  ROC({ROC_SHORT}): {signal['roc5']}%  |  ROC({ROC_MEDIUM}): {signal['roc20']}%")
            print(f"  触发原因: {signal.get('reason_desc', reason_text)}")
            print(f"  时间: {ts_to_str(signal['ts'])}")
            print(f"  ═══════════════════════════════════════════════════════")
            print(f"{border}\n")
        else:
            # 入场信号
            direction = signal["direction"]
            if direction == "long":
                border = "🟢" * 20
                emoji = "📈"
                label = "做多"
            else:
                border = "🔴" * 20
                emoji = "📉"
                label = "做空"

            # 组装触发原因 (4个条件打勾)
            cd = signal.get("conds", {})
            v_ok = "✅" if cd.get("vol_ok") else "❌"
            r8p = "✅" if cd.get("roc5_pos") else "❌"
            r20p = "✅" if cd.get("roc20_pos") else "❌"
            r_acc = "✅" if cd.get("roc_accel") else "❌"
            vol_ratio = signal.get("vol_ratio", "?")
            if direction == "long":
                reason_lines = [
                    f"    {v_ok} 放量 (量比 {vol_ratio}x)",
                    f"    {r8p} ROC({ROC_SHORT})>0 ({signal['roc5']}%)",
                    f"    {r20p} ROC({ROC_MEDIUM})>0 ({signal['roc20']}%)",
                    f"    {r_acc} ROC({ROC_SHORT})>ROC({ROC_MEDIUM}) (短期加速)",
                ]
            else:
                r8n = "✅" if (not cd.get("roc5_pos")) else "❌"
                r20n = "✅" if (not cd.get("roc20_pos")) else "❌"
                r_acc_n = "✅" if (not cd.get("roc_accel")) else "❌"
                reason_lines = [
                    f"    {v_ok} 放量 (量比 {vol_ratio}x)",
                    f"    {r8n} ROC({ROC_SHORT})<0 ({signal['roc5']}%)",
                    f"    {r20n} ROC({ROC_MEDIUM})<0 ({signal['roc20']}%)",
                    f"    {r_acc_n} ROC({ROC_SHORT})<ROC({ROC_MEDIUM}) (短期加速下跌)",
                ]

            print(f"\n{border}")
            print(f"  {emoji} {emoji} {emoji}  *** {sig_type} 信号触发 — {label} ***  {emoji} {emoji} {emoji}")
            print(f"  ═══════════════════════════════════════════════════════")
            print(f"  价格: {signal['price']} USDT")
            print(f"  ROC({ROC_SHORT}): {signal['roc5']}%  |  ROC({ROC_MEDIUM}): {signal['roc20']}%")
            print(f"  入场条件:")
            for ln in reason_lines:
                print(ln)
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
                self.large_orders.append(order)    # deque 线程安全, 自动滚动
                self.flow_window.append(order)      # 统计窗口
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
        threading.Thread(target=self.connect_aggtrade, daemon=True, name="aggtrade-ws").start()
        print(f"  🐋 大资金流向: aggTrade 大单流已订阅 (≥{LARGE_ORDER_USDT/10000:.0f}万U)")

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
            "trades": [s for s in signals if s.get("type") == "CLOSE"][-20:],
            "last_price": self.last_price or (round(float(closes[-1]), 2) if len(closes) else 0),
            "last_ts": int(bars[-1]["t"]) if bars else 0,
            "indicator_state": indicator_state,
            "signal_readiness": signal_readiness,
            "trend_state": trend_state,
            "supports": supports,
            "resistances": resistances,
            "position": {
                "direction": self.position["direction"],
                "entry_price": self.position["entry_price"],
                "entry_ts": self.position["entry_ts"],
                "held_bars": self.bar_count - self.position["entry_bar"],
            } if self.position else None,
            "params": {
                "roc_short": ROC_SHORT, "roc_medium": ROC_MEDIUM,
                "vol_ma_period": VOL_MA_PERIOD, "sl_usdt": SL_USDT,
                "leverage": LEVERAGE, "capital": CAPITAL,
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
<title>ETH v12 实时交易信号面板</title>
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
.trade-list{max-height:250px;overflow-y:auto}
.trade-item{display:flex;align-items:center;flex-wrap:wrap;padding:6px 8px;margin:3px 0;border-radius:3px;font-size:12px;background:#21262d;border-left:3px solid #58a6ff}
.trade-item .t-dir{font-weight:bold;width:38px}
.trade-item .t-path{flex:1;min-width:70px;color:#c9d1d9}
.trade-item .t-pnl{font-weight:bold}
.trade-item .t-pnl.pos{color:#3fb950}
.trade-item .t-pnl.neg{color:#f85149}
.trade-item .t-reason{color:#8b949e;font-size:11px;margin-left:6px}
.trade-item .t-time{color:#8b949e;font-size:11px;margin-left:8px}
.param-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px}
.param-item{background:#21262d;padding:5px 8px;border-radius:3px}
.param-item .p-label{color:#8b949e}
.param-item .p-val{font-weight:bold;color:#58a6ff}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
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
      <a href="/" class="active">📊 信号</a>
      <a href="/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1><span class="live-dot"></span>ETH v12 双ROC动量策略 — 实时信号</h1>
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
            <option value="3" selected>3x</option><option value="5">5x</option>
            <option value="10">10x</option><option value="20">20x</option>
          </select>
        </div>
        <button type="button" class="o-submit" id="osubmit">🚀 下单</button>
        <div class="o-msg" id="omsg"></div>
      </div>
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
      <div class="indicator-row"><span class="label">趋势</span><span class="value" id="ind-trend">--</span></div>
      <div class="indicator-row"><span class="label">MA20 / MA50</span><span class="value" id="ind-ma">--</span></div>
      <div class="indicator-row"><span class="label">支撑位</span><span class="value" id="ind-supports">--</span></div>
      <div class="indicator-row"><span class="label">突破位</span><span class="value" id="ind-resist">--</span></div>
      <div class="indicator-row"><span class="label">布林带 上/中/下</span><span class="value" id="ind-boll">--</span></div>
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

    <!-- 最近交易 -->
    <div class="section">
      <h3>📋 最近交易</h3>
      <div class="trade-list" id="trade-list"><div style="color:#8b949e;font-size:12px">暂无交易</div></div>
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
let currentTrades = [];

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

  // 指标
  const s = data.indicator_state || {};
  document.getElementById('ind-roc5').textContent = (s.roc5||0).toFixed(4) + '%';
  document.getElementById('ind-roc20').textContent = (s.roc20||0).toFixed(4) + '%';
  document.getElementById('ind-vol').textContent = (s.volume||0).toFixed(1);
  document.getElementById('ind-volma').textContent = (s.vol_ma||0).toFixed(1);
  document.getElementById('ind-volratio').textContent = (s.vol_ratio||0).toFixed(2) + 'x';

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
  document.getElementById('ind-ma').textContent =
    (tr.ma_fast != null && tr.ma_slow != null) ? `${tr.ma_fast} / ${tr.ma_slow}` : '--';
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
        const reasonMap = {'momentum_death':'动量衰竭','SL':'止损','timeout':'超时'};
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

  // 最近交易列表 (每笔已平仓交易)
  const tradeDiv = document.getElementById('trade-list');
  const trades = data.trades || [];
  if (trades.length !== currentTrades.length) {
    currentTrades = trades;
    tradeDiv.innerHTML = trades.length === 0
      ? '<div style="color:#8b949e;font-size:12px">暂无交易</div>'
      : trades.slice().reverse().map(t => {
          const dirTxt = t.direction === 'long' ? '📈多' : '📉空';
          const pnl = t.pnl || 0;
          const pnlCls = pnl >= 0 ? 'pos' : 'neg';
          const reasonMap = {'momentum_death':'动量衰竭','SL':'止损','timeout':'超时'};
          const d = new Date(t.exit_time || t.ts);
          const tm = d.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
          return `<div class="trade-item">
            <span class="t-dir">${dirTxt}</span>
            <span class="t-path">$${t.entry_price}→$${t.exit_price || t.price}</span>
            <span class="t-pnl ${pnlCls}">${pnl>=0?'+':''}${pnl.toFixed(2)}U</span>
            <span class="t-reason">${reasonMap[t.reason]||t.reason}·${t.held_bars}根</span>
            <span class="t-time">${tm}</span>
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


HTML_PAGE_FLOW = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETH 大资金流向 — 实时监控</title>
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
      <a href="/">📊 信号</a>
      <a href="/flow" class="active">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1><span class="live-dot"></span>ETH 大资金流向 — 实时监控</h1>
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


app = FastAPI(title="ETH v12 实时交易信号", lifespan=lifespan)
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
    pos = trader.position
    pos_info = None
    if pos:
        pos_info = {
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "entry_ts": ts_to_str(pos["entry_ts"]),
            "held_bars": trader.bar_count - pos["entry_bar"],
        }
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
        "position": pos_info,
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
    sig_type = signal["type"]
    if sig_type == "CLOSE":
        # 平仓通知
        direction = signal["direction"]
        pnl = signal["pnl"]
        reason_map = {"momentum_death": "动量衰竭", "SL": "硬止损", "timeout": "超时平仓"}
        reason_text = reason_map.get(signal["reason"], signal["reason"])
        reason_desc = signal.get("reason_desc", reason_text)
        pnl_emoji = "💰" if pnl >= 0 else "💸"
        dir_text = "做多" if direction == "long" else "做空"
        title = f"🔚 平仓 {reason_text} | {pnl_emoji}{pnl:+.2f}U"
        content = (
            f"## 🔚 平仓信号 — {reason_text}\n\n"
            f"- **方向**: {dir_text}\n"
            f"- **入场价**: {signal['entry_price']} USDT\n"
            f"- **平仓价**: {signal['price']} USDT\n"
            f"- **盈亏**: {pnl_emoji} {pnl:+.4f} USDT\n"
            f"- **持仓**: {signal['held_bars']} 根K线\n"
            f"- **ROC(8)**: {signal['roc5']}%  |  ROC(20): {signal['roc20']}%\n"
            f"- **平仓原因**: {reason_desc}\n"
            f"- **K线时间(开盘)**: {ts_to_str(signal['ts'])}\n"
            f"- **信号触发(收盘)**: {now()}\n"
            f"\n> ETH v12 双ROC动量策略 · 平仓信号"
        )
    else:
        # 入场通知: 加入4项条件明细 + 触发原因说明
        direction = signal["direction"]
        label = "🟢 做多" if direction == "long" else "🔴 做空"
        label_text = "做多" if direction == "long" else "做空"
        cd = signal.get("conds", {})
        vr = signal.get("vol_ratio", "?")

        # 组装条件打勾列表 + 原因总结
        if direction == "long":
            conds_md = [
                f"- 放量 (量比{vr}x): {'✅' if cd.get('vol_ok') else '❌'}",
                f"- ROC(8)>0 ({signal['roc5']}%): {'✅' if cd.get('roc5_pos') else '❌'}",
                f"- ROC(20)>0 ({signal['roc20']}%): {'✅' if cd.get('roc20_pos') else '❌'}",
                f"- ROC(8)>ROC(20) 短期加速: {'✅' if cd.get('roc_accel') else '❌'}",
            ]
            reason_summary = f"双ROC同向为正({signal['roc5']}%/{signal['roc20']}%)且短期加速, 量比{vr}x(放量确认)"
        else:
            conds_md = [
                f"- 放量 (量比{vr}x): {'✅' if cd.get('vol_ok') else '❌'}",
                f"- ROC(8)<0 ({signal['roc5']}%): {'✅' if not cd.get('roc5_pos') else '❌'}",
                f"- ROC(20)<0 ({signal['roc20']}%): {'✅' if not cd.get('roc20_pos') else '❌'}",
                f"- ROC(8)<ROC(20) 短期加速下跌: {'✅' if not cd.get('roc_accel') else '❌'}",
            ]
            reason_summary = f"双ROC同向为负({signal['roc5']}%/{signal['roc20']}%)且短期加速下跌, 量比{vr}x(放量确认)"

        title = f"{label}信号触发 | ETH={signal['price']} USDT"
        content = (
            f"## {sig_type} 信号 — {label}\n\n"
            f"### 🎯 触发原因\n{reason_summary}\n\n"
            f"### 📋 入场条件明细\n"
            + "\n".join(conds_md) + "\n\n"
            + f"- **价格**: {signal['price']} USDT\n"
            f"- **K线时间(开盘)**: {ts_to_str(signal['ts'])}\n"
            f"- **信号触发(收盘)**: {now()}\n"
            f"- **建议仓位**: {FRACTION_BASE*100:.0f}% × {LEVERAGE}x\n"
            f"- **止损**: {SL_USDT} USDT / 超时: {MAX_HOLD_BARS}根K线\n"
            f"\n> ETH v12 双ROC动量策略 · {label_text}入场信号"
        )
    threading.Thread(target=wx_notify, args=(title, content), daemon=True).start()
trader._print_signal = _print_signal_with_broadcast


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
