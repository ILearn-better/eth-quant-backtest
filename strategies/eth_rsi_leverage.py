"""ETH RSI 双向策略 - 日线回测 (v8: 3x杠杆 + 做多/做空 + 方向互斥 + MA150过滤)"""
import numpy as np
from base import BaseStrategy, compute_stats, generate_html_report


def calc_rsi(close_series, period=14):
    """计算 RSI 指标"""
    delta = np.diff(close_series, prepend=close_series[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)

    # Wilder 平滑
    avg_gain = np.zeros_like(gain, dtype=float)
    avg_loss = np.zeros_like(loss, dtype=float)

    if len(gain) < period:
        return np.full(len(close_series), 50.0)

    avg_gain[period - 1] = np.mean(gain[:period])
    avg_loss[period - 1] = np.mean(loss[:period])

    for i in range(period, len(gain)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

    rs = np.where(avg_loss > 1e-10, avg_gain / avg_loss, 100.0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[:period - 1] = 50.0
    return rsi


def calc_ma(close_series, period):
    """计算简单移动平均线"""
    ma = np.full(len(close_series), np.nan)
    if len(close_series) < period:
        return ma
    cumsum = np.cumsum(close_series)
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


class EthRSILeverageStrategy(BaseStrategy):
    """
    ETH RSI 双向策略 v8 (日线 + 3x杠杆 + 做多做空 + 方向互斥)

    规则:
    - 数据周期: **日线 (1d)**
    - 本金 150 USDT, 3倍杠杆
    - 趋势过滤:
      做多: 价格必须在 MA150 上方
      做空: 价格必须在 MA150 下方
    - RSI < 40 → 开多仓 (超卖反弹)
    - RSI > 70 → 开空仓 (超买回调)
    - 固定止盈 +5U / 固定止损 -2U (每笔仓位独立计算)
    - 方向互斥: 同一时间只能持有一个方向的仓 (有多不能开空，有空不能开多)
    - 动态仓位: 根据当前回撤幅度调整开仓大小
      回撤 <10% → 满仓位 FRACTION_BASE
      回撤 10~20% → 70% 仓位
      回撤 20~30% → 50% 仓位
      回撤 >30% → 30% 仓位
    - 手续费 0.04% (开+平)
    """

    name = "ETH-RSI双向策略+MA150-v8"
    CAPITAL = 150.0
    LEVERAGE = 3
    FRACTION_BASE = 0.2     # 基础仓位比例
    TP_USDT = 5.0           # 止盈
    SL_USDT = 2.0           # 止损
    FEE_RATE = 0.0004       # 手续费率
    RSI_PERIOD = 14
    MA_PERIOD = 150         # 趋势均线周期

    OVERSOLD_LONG = 40      # RSI<40 做多阈值
    OVERBOUGHT_SHORT = 70   # RSI>70 做空阈值

    # 回撤减仓阈值
    DRAWDOWN_THRESHOLDS = [
        (0.10, 1.0),    # 回撤<10% → 100%仓位
        (0.20, 0.7),    # 回撤10-20% → 70%
        (0.30, 0.5),    # 回撤20-30% → 50%
        (1.00, 0.3),    # 回撤>30% → 30%
    ]

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)
        n_bars = len(closes)

        # 计算 RSI 和 MA150
        rsi = calc_rsi(closes, self.RSI_PERIOD)
        ma150 = calc_ma(closes, self.MA_PERIOD)

        balance = self.CAPITAL
        peak_balance = self.CAPITAL  # 资金峰值(用于计算回撤)
        position = None              # 当前持仓 (None 或一个持仓dict)
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        # 预热期: max(RSI_PERIOD, MA_PERIOD) + 1
        warmup = max(self.RSI_PERIOD, self.MA_PERIOD) + 1

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = closes[i]
            cur_rsi = rsi[i]
            cur_ma = ma150[i]

            # ---- 1. 检查现有持仓的止盈止损 ----
            if position is not None:
                pnl, should_close = self._check_tp_sl(position, price)

                if should_close:
                    close_fee = position["size_usdt"] * self.FEE_RATE / 2
                    net_pnl = pnl - close_fee

                    trade_record = {
                        "direction": position["direction"],
                        "level": position["level"],
                        "entry_price": position["entry_price"],
                        "exit_price": round(price, 2),
                        "pnl": round(net_pnl, 4),
                        "entry_time": position["entry_time"],
                        "exit_time": ts,
                        "reason": "TP" if pnl > 0 else "SL",
                    }
                    trades.append(trade_record)
                    balance += net_pnl
                    position = None

            # 更新资金峰值和当前回撤
            if balance > peak_balance:
                peak_balance = balance
            current_drawdown = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0

            # ---- 2. 开仓逻辑 (方向互斥) ----
            if position is None and not np.isnan(cur_ma):
                # 动态仓位计算
                position_multiplier = self._get_position_size(current_drawdown)
                actual_fraction = self.FRACTION_BASE * position_multiplier

                # --- 做多条件 ---
                if (cur_rsi < self.OVERSOLD_LONG
                        and price > cur_ma):
                    position = self._open_long(price, ts, actual_fraction, "L1", balance)

                # --- 做空条件 ---
                elif (cur_rsi > self.OVERBOUGHT_SHORT
                      and price < cur_ma):
                    position = self._open_short(price, ts, actual_fraction, "S1", balance)

            # 记录权益曲线
            unrealized = self._calc_unrealized(position, price)
            equity_curve.append((ts, round(balance + unrealized, 4)))

        # ---- 3. 期末强平所有持仓 ----
        final_price = closes[-1]
        final_ts = int(timestamps[-1])
        if position is not None:
            pnl = self._calc_pnl(position, final_price)
            close_fee = position["size_usdt"] * self.FEE_RATE / 2
            net_pnl = pnl - close_fee
            trades.append({
                "direction": position["direction"],
                "level": position["level"],
                "entry_price": position["entry_price"],
                "exit_price": round(final_price, 2),
                "pnl": round(net_pnl, 4),
                "entry_time": position["entry_time"],
                "exit_time": final_ts,
                "reason": "force_close",
            })
            balance += net_pnl

        equity_curve.append((final_ts, round(balance, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

    def _get_position_size(self, drawdown):
        """根据回撤幅度返回仓位系数"""
        for threshold, multiplier in self.DRAWDOWN_THRESHOLDS:
            if drawdown <= threshold:
                return multiplier
        return 0.3  # 最小30%

    def _open_long(self, price, ts, fraction, level, balance):
        """开多仓，返回持仓字典"""
        notional = balance * fraction * self.LEVERAGE
        size_usdt = notional
        open_fee = size_usdt * self.FEE_RATE / 2
        return {
            "direction": "long",
            "entry_price": price,
            "size_usdt": size_usdt,
            "fraction": fraction,
            "level": level,
            "fee_paid": open_fee,
            "entry_time": ts,
        }

    def _open_short(self, price, ts, fraction, level, balance):
        """开空仓，返回持仓字典"""
        notional = balance * fraction * self.LEVERAGE
        size_usdt = notional
        open_fee = size_usdt * self.FEE_RATE / 2
        return {
            "direction": "short",
            "entry_price": price,
            "size_usdt": size_usdt,
            "fraction": fraction,
            "level": level,
            "fee_paid": open_fee,
            "entry_time": ts,
        }

    def _calc_unrealized(self, pos, current_price):
        """计算未实现盈亏"""
        if pos is None:
            return 0.0
        return self._calc_pnl(pos, current_price)

    def _calc_pnl(self, pos, current_price):
        """计算已实现/未实现盈亏 (不含手续费)"""
        if pos["direction"] == "long":
            price_diff = current_price - pos["entry_price"]
        else:  # short
            price_diff = pos["entry_price"] - current_price

        pct_change = price_diff / pos["entry_price"]
        pnl = pct_change * pos["size_usdt"]
        return pnl

    def _check_tp_sl(self, pos, current_price):
        """检查止盈止损, 返回 (pnl, should_close)"""
        pnl = self._calc_pnl(pos, current_price)

        if pnl >= self.TP_USDT:
            return pnl, True
        elif pnl <= -self.SL_USDT:
            return pnl, True
        return pnl, False
