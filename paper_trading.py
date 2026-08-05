"""ETH 双ROC动量策略 v12 — 实盘模拟盘 (Paper Trading)

原理:
- 通过 Binance 现货 WebSocket (data-stream.binance.vision) 实时接收 1h K线
- 用现货价格模拟合约 3x 杠杆交易 (基差极小, 可忽略)
- 每根K线收盘时, 执行 v12 策略逻辑 (双ROC动量 + 成交量确认)
- 状态持久化到 JSON, 进程重启可恢复

数据源:
- WS:  wss://data-stream.binance.vision/ws/ethusdt@kline_1h
- REST: https://data-api.binance.vision/api/v3/klines (历史回补)
- 代理: 127.0.0.1:7897

运行:
  python paper_trading.py
"""
import json
import os
import sys
import time
import datetime
import threading
import urllib.request
import urllib.error

import numpy as np
import websocket

# ==================== 配置 ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "paper_state", "state.json")
LOG_FILE = os.path.join(BASE_DIR, "paper_state", "trades.jsonl")
SNAPSHOT_FILE = os.path.join(BASE_DIR, "paper_state", "daily_snapshots.jsonl")

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897

SYMBOL = "ethusdt"
INTERVAL = "1h"
WS_URL = "wss://data-stream.binance.vision/ws/ethusdt@kline_1h"
REST_KLINE_URL = "https://data-api.binance.vision/api/v3/klines"

HISTORY_BARS = 300       # 历史回补根数 (足够预热 ROC20 + VolMA20)
RECONNECT_DELAY = 5      # 断线重连延迟 (秒)

# ---- v12 策略参数 (与 eth_roc_momentum_v12.py 一致, 2026-08-05参数优化) ----
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


# ==================== 工具函数 ====================

def get_proxy_opener():
    proxy = urllib.request.ProxyHandler({
        "http": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "https": f"http://{PROXY_HOST}:{PROXY_PORT}",
    })
    return urllib.request.build_opener(proxy)


def fetch_history(limit=HISTORY_BARS):
    """从 REST 拉取历史K线 [open_time, open, high, low, close, volume]"""
    url = f"{REST_KLINE_URL}?symbol=ETHUSDT&interval={INTERVAL}&limit={limit}"
    opener = get_proxy_opener()
    resp = opener.open(url, timeout=15)
    data = json.loads(resp.read().decode())
    bars = []
    for k in data:
        bars.append({
            "open_time": int(k[0]),
            "close_time": int(k[6]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return bars


def calc_roc(closes, period):
    """ROC 变化率 %"""
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


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 模拟盘账户 ====================

class PaperAccount:
    """模拟账户: 权益/持仓/交易记录/状态持久化"""

    def __init__(self):
        self.balance = CAPITAL            # 账户权益 (USDT)
        self.peak_balance = CAPITAL
        self.position = None              # 当前持仓 dict or None
        self.trades = []                  # 已平仓交易
        self.last_signal_bar_ts = None    # 最后触发信号的K线时间
        self.started_at = now_str()
        self._load()

    # ---- 持久化 ----
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    st = json.load(f)
                self.balance = st.get("balance", CAPITAL)
                self.peak_balance = st.get("peak_balance", CAPITAL)
                self.position = st.get("position")
                self.trades = st.get("trades", [])
                self.last_signal_bar_ts = st.get("last_signal_bar_ts")
                self.started_at = st.get("started_at", self.started_at)
                print(f"  🔄 已恢复状态: 权益={self.balance:.2f}U, 持仓={'有' if self.position else '无'}, "
                      f"已平仓{len(self.trades)}笔")
            except Exception as e:
                print(f"  ⚠️ 状态恢复失败, 重新开始: {e}")

    def save(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "balance": self.balance,
                "peak_balance": self.peak_balance,
                "position": self.position,
                "trades": self.trades[-500:],      # 保留最近500笔
                "last_signal_bar_ts": self.last_signal_bar_ts,
                "started_at": self.started_at,
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)

    def log_trade(self, trade):
        self.trades.append(trade)
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")

    def snapshot_daily(self):
        """每日权益快照"""
        today = datetime.date.today().isoformat()
        os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
        # 避免重复写入同一天
        last_line = None
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                last_line = json.loads(lines[-1])
        if last_line and last_line.get("date") == today:
            return
        snap = {
            "date": today,
            "time": now_str(),
            "balance": round(self.balance, 2),
            "position": "long" if self.position and self.position["direction"] == "long"
                        else ("short" if self.position else "none"),
            "trades_done": len(self.trades),
        }
        with open(SNAPSHOT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # ---- 策略 ----
    def get_position_size_multiplier(self):
        dd = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0
        for threshold, mult in DRAWDOWN_THRESHOLDS:
            if dd <= threshold:
                return mult
        return 0.3

    def open_position(self, direction, price, ts, bar_idx, roc5, roc20):
        if self.position is not None:
            return False
        fraction = FRACTION_BASE * self.get_position_size_multiplier()
        notional = self.balance * fraction * LEVERAGE
        # 保证金检查: 名义/杠杆 <= 权益
        if notional / LEVERAGE > self.balance:
            print(f"  ⚠️ 保证金不足, 跳过开仓: 需要{notional/LEVERAGE:.2f}U > 权益{self.balance:.2f}U")
            return False
        self.position = {
            "direction": direction,
            "entry_price": price,
            "size_usdt": notional,
            "fraction": round(fraction, 4),
            "level": "L1" if direction == "long" else "S1",
            "entry_time": ts,
            "entry_bar": bar_idx,
            "entry_roc5": round(roc5, 2),
            "entry_roc20": round(roc20, 2),
        }
        open_fee = notional * FEE_RATE / 2
        self.balance -= open_fee  # 开仓手续费从权益扣除
        print(f"  ✅ [{now_str()}] 开仓 {'做多' if direction=='long' else '做空'} @ {price:.2f} "
              f"| 名义{notional:.2f}U (3x) | ROC5={roc5:.2f} ROC20={roc20:.2f} | 手续费{open_fee:.4f}U")
        self.save()
        return True

    def close_position(self, price, ts, bar_idx, reason):
        if self.position is None:
            return
        pos = self.position
        if pos["direction"] == "long":
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
        else:
            pnl_pct = (pos["entry_price"] - price) / pos["entry_price"]
        pnl = pnl_pct * pos["size_usdt"]
        close_fee = pos["size_usdt"] * FEE_RATE / 2
        net_pnl = pnl - close_fee
        self.balance += net_pnl
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        held_bars = bar_idx - pos["entry_bar"]
        trade = {
            "direction": pos["direction"],
            "level": pos["level"],
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(price, 2),
            "pnl": round(net_pnl, 4),
            "entry_time": pos["entry_time"],
            "exit_time": ts,
            "held_bars": held_bars,
            "entry_roc5": pos["entry_roc5"],
            "entry_roc20": pos["entry_roc20"],
            "reason": reason,
            "balance_after": round(self.balance, 2),
        }
        self.log_trade(trade)
        print(f"  🔔 [{now_str()}] 平仓 {'做多' if pos['direction']=='long' else '做空'} "
              f"@{price:.2f} | 盈亏{net_pnl:+.4f}U | 持仓{held_bars}根 | 原因:{reason} "
              f"| 权益={self.balance:.2f}U")
        self.position = None
        self.save()

    def on_bar_close(self, bar, bar_idx, roc5_hist, roc20_hist, vol_ma_hist):
        """每根K线收盘时调用: 执行策略逻辑"""
        price = bar["close"]
        ts = bar["close_time"]
        i = bar_idx

        cur_roc5 = roc5_hist[i]
        cur_roc20 = roc20_hist[i]
        cur_vol = bar["volume"]
        cur_vol_ma = vol_ma_hist[i]

        if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma):
            return

        # ---- 出场检查 ----
        if self.position is not None:
            pnl = self._calc_pnl(self.position, price)
            held_bars = i - self.position["entry_bar"]
            should_close = False
            reason = ""

            if self.position["direction"] == "long" and cur_roc5 < 0:
                should_close, reason = True, "momentum_death"
            elif self.position["direction"] == "short" and cur_roc5 > 0:
                should_close, reason = True, "momentum_death"
            if pnl <= -SL_USDT:
                should_close, reason = True, "SL"
            if held_bars >= MAX_HOLD_BARS:
                should_close, reason = True, "timeout"

            if should_close:
                self.close_position(price, ts, i, reason)

        # ---- 入场检查 (方向互斥) ----
        if self.position is None:
            vol_confirmed = cur_vol > cur_vol_ma
            if vol_confirmed:
                if cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20:
                    self.open_position("long", price, ts, i, cur_roc5, cur_roc20)
                elif cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20:
                    self.open_position("short", price, ts, i, cur_roc5, cur_roc20)

        self.last_signal_bar_ts = ts
        self.save()

    def _calc_pnl(self, pos, current_price):
        if pos["direction"] == "long":
            price_diff = current_price - pos["entry_price"]
        else:
            price_diff = pos["entry_price"] - current_price
        return price_diff / pos["entry_price"] * pos["size_usdt"]

    def print_status(self, price=None):
        pos_str = "无持仓"
        if self.position:
            dir_label = "做多" if self.position["direction"] == "long" else "做空"
            pos_str = f"{dir_label} @ {self.position['entry_price']:.2f} (名义{self.position['size_usdt']:.2f}U)"
            if price:
                unrealized = self._calc_pnl(self.position, price)
                pos_str += f" | 浮动盈亏 {unrealized:+.2f}U"
        print(f"  💰 [{now_str()}] 权益={self.balance:.2f}U (峰值{self.peak_balance:.2f}U) | {pos_str} | 已平仓{len(self.trades)}笔")


# ==================== WebSocket 主循环 ====================

class PaperTrader:
    def __init__(self):
        self.account = PaperAccount()
        self.bars = []          # 已收盘K线缓冲
        self.roc5 = np.array([])
        self.roc20 = np.array([])
        self.vol_ma = np.array([])
        self.running = True
        self.ws = None

    # ---- 历史回补 ----
    def init_history(self):
        print("  📥 拉取历史K线预热指标...")
        bars = fetch_history(HISTORY_BARS)
        self.bars = bars
        closes = np.array([b["close"] for b in bars])
        vols = np.array([b["volume"] for b in bars])
        self.roc5 = calc_roc(closes, ROC_SHORT)
        self.roc20 = calc_roc(closes, ROC_MEDIUM)
        self.vol_ma = calc_ma(vols, VOL_MA_PERIOD)
        print(f"  ✅ 历史回补完成: {len(bars)} 根, "
              f"最新: {datetime.datetime.fromtimestamp(bars[-1]['close_time']/1000)}")

        # 处理历史断档期间错过的信号 (进程重启后, 完整补跑错过的K线)
        self._catch_up_missed_bars()
        self.account.save()   # 确保状态尽早持久化

    def _catch_up_missed_bars(self):
        """进程重启/断线后, 补跑错过的K线 (完整策略逻辑: 开仓+平仓+止损)"""
        account = self.account

        # 1. 重新对齐当前持仓的 entry_bar 索引 (bars 数组是全新的300根)
        if account.position is not None:
            pos = account.position
            entry_idx = None
            for i, b in enumerate(self.bars):
                if b["close_time"] == pos["entry_time"]:
                    entry_idx = i
                    break
            if entry_idx is None:
                print("  ⚠️ 持仓进入点不在回补窗口内, 保持持仓等待新K线判断")
            else:
                pos["entry_bar"] = entry_idx
                print(f"  🔄 持仓对齐: {'做多' if pos['direction']=='long' else '做空'} "
                      f"@{pos['entry_price']:.2f} (bar#{entry_idx})")

        # 2. 找出最后已处理的K线位置, 之后的K线逐根补跑
        last_ts = account.last_signal_bar_ts
        if last_ts is None:
            return  # 首次运行, 无需补跑

        start_idx = None
        for i, b in enumerate(self.bars):
            if b["close_time"] <= last_ts:
                start_idx = i
            else:
                break

        if start_idx is None:
            print("  ⚠️ 无法定位最后处理点, 跳过补跑")
            return

        missed_count = len(self.bars) - 1 - start_idx
        if missed_count <= 0:
            return

        print(f"  🔄 断线期间错过 {missed_count} 根K线, 开始补跑...")
        for i in range(start_idx + 1, len(self.bars)):
            bar = self.bars[i]
            account.on_bar_close(bar, i, self.roc5, self.roc20, self.vol_ma)
        print(f"  ✅ 补跑完成, 当前权益={account.balance:.2f}U, 已平仓{len(account.trades)}笔")

    # ---- 指标更新 ----
    def _update_indicators(self):
        closes = np.array([b["close"] for b in self.bars])
        vols = np.array([b["volume"] for b in self.bars])
        self.roc5 = calc_roc(closes, ROC_SHORT)
        self.roc20 = calc_roc(closes, ROC_MEDIUM)
        self.vol_ma = calc_ma(vols, VOL_MA_PERIOD)

    # ---- K线处理 ----
    def on_kline(self, k):
        ts = int(k["t"])            # K线开盘时间
        is_closed = k["x"]          # 是否收盘
        bar = {
            "open_time": ts,
            "close_time": int(k["T"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }

        if is_closed:
            # 收盘K线: 追加到缓冲, 触发策略
            # 防止重复处理同一根 (WS 可能重复推送)
            if self.bars and self.bars[-1]["close_time"] == bar["close_time"]:
                self.bars[-1] = bar   # 覆盖, 幂等
                self._update_indicators()
            else:
                self.bars.append(bar)
                if len(self.bars) > HISTORY_BARS:
                    self.bars = self.bars[-HISTORY_BARS:]
                self._update_indicators()

            bar_idx = len(self.bars) - 1
            self.account.on_bar_close(bar, bar_idx, self.roc5, self.roc20, self.vol_ma)
            self.account.snapshot_daily()
            # 每根收盘K线打印一次状态
            t_str = datetime.datetime.fromtimestamp(bar["close_time"] / 1000).strftime("%m-%d %H:%M")
            print(f"  📊 [{now_str()}] K线收盘 {t_str} close={bar['close']:.2f} vol={bar['volume']:.1f}")
            self.account.print_status()
        else:
            # 未收盘K线: 仅更新浮动盈亏显示 (不打印, 避免刷屏)
            if self.account.position:
                pass  # 实时价格浮动盈亏可通过 ticker 显示, 这里从简

    # ---- WebSocket 事件 ----
    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "k" in data:
                self.on_kline(data["k"])
        except Exception as e:
            print(f"  ⚠️ 消息解析错误: {e}")

    def on_error(self, ws, error):
        print(f"  ❌ WS错误: {error}")

    def on_close(self, ws, code, msg):
        print(f"  🔌 WS连接关闭 ({code} {msg}), {RECONNECT_DELAY}s后重连...")
        if self.running:
            threading.Timer(RECONNECT_DELAY, self.connect).start()

    def on_open(self, ws):
        print(f"  🔗 WS已连接: {WS_URL}")

    def connect(self):
        try:
            self.ws = websocket.WebSocketApp(
                WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            self.ws.run_forever(
                http_proxy_host=PROXY_HOST,
                http_proxy_port=PROXY_PORT,
                proxy_type="http",
                ping_interval=20,       # Binance 要求 20s 内心跳
                ping_timeout=10,
            )
        except Exception as e:
            print(f"  ❌ 连接失败: {e}, {RECONNECT_DELAY}s后重试...")
            if self.running:
                threading.Timer(RECONNECT_DELAY, self.connect).start()

    def run(self):
        print(f"{'='*65}")
        print(f"🎯 实盘模拟盘: v12 双ROC动量策略")
        print(f"   账户权益: {CAPITAL} USDT | 杠杆: {LEVERAGE}x | 手续费: {FEE_RATE*100:.2f}%")
        print(f"   做多: ROC5>0 & ROC20>0 & ROC5>ROC20 & 放量")
        print(f"   做空: ROC5<0 & ROC20<0 & ROC5<ROC20 & 放量")
        print(f"   出场: 动量衰竭 / 止损{SL_USDT}U / 超时{MAX_HOLD_BARS}根")
        print(f"   数据源: {WS_URL}")
        print(f"   状态文件: {STATE_FILE}")
        print(f"{'='*65}")

        # 历史回补
        try:
            self.init_history()
        except Exception as e:
            print(f"  ❌ 历史回补失败: {e}")
            print("  ⚠️ 请确认 Clash Verge 代理已启动 (127.0.0.1:7897)")
            self.running = False
            return

        self.account.print_status()
        print(f"\n  🚀 开始实时监听... (Ctrl+C 退出, 状态自动保存)\n")
        self.connect()

        # 保持主线程存活
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n  👋 退出, 状态已保存")
            self.running = False
            self.account.save()
            if self.ws:
                self.ws.close()


def main():
    trader = PaperTrader()
    trader.run()


if __name__ == "__main__":
    main()
