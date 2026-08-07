"""ETH 月线均值回归策略 v9 (1h + MA150过滤 + 逆势抄底/摸顶)"""
import numpy as np
from base import BaseStrategy, compute_stats, generate_html_report


def calc_rsi(close_series, period=14):
    """计算 RSI 指标 (Wilder平滑)"""
    delta = np.diff(close_series, prepend=close_series[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)

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


class EthMAReversionV9(BaseStrategy):
    """
    ETH 月线均值回归策略 v9 (1h线 + 3x杠杆 + 均值回归)

    核心思路: 当价格偏离月均线(MA150)较远时做均值回归交易
    - 数据周期: **1小时线 (1h)**
    - 本金 150 USDT, 3倍杠杆
    - 趋势判断: MA150 作为"月均线"基准
      做多: 价格 **低于** MA150 (超跌区域)
      做空: 价格 **远高于** MA150 (>105%, 过热区域)
    - RSI < 30 → 开多仓 (深度超卖, 抄底反弹)
    - RSI > 70 → 开空仓 (深度超买, 摸顶回调)
    - 止盈止损基于 **本金比例**: TP=+5%本金 / SL=-3%本金
      即 TP_USDT = 150*5% = 7.5U, SL_USDT = 150*3% = 4.5U
    - 方向互斥: 同一时间只能持有一个方向的仓
    - 动态仓位: 根据当前回撤幅度调整开仓大小
      回撤 <10% → 满仓位 FRACTION_BASE
      回撤 10~20% → 70% 仓位
      回撤 20~30% → 50% 仓位
      回撤 >30% → 30% 仓位
    - 手续费 0.04% (开+平)
    """

    name = "ETH月线均值回归策略-v9"
    CAPITAL = 150.0
    LEVERAGE = 3
    FRACTION_BASE = 0.8       # 基础仓位比例
    TP_PCT = 0.05             # 止盈: 本金的 5%
    SL_PCT = 0.03             # 止损: 本金的 3%
    FEE_RATE = 0.0004         # 手续费率
    RSI_PERIOD = 14
    MA_PERIOD = 720           # 月均线: 1h数据用 150天×24h=720根 (≈30天)

    RSI_OVERSOLD = 30         # 做多阈值 (超卖)
    RSI_OVERBOUGHT = 70       # 做空阈值 (深度超买)
    MA_HIGH_RATIO = 1.05      # "远高于"均线: 价格 > MA的105%

    # 回撤减仓阈值
    DRAWDOWN_THRESHOLDS = [
        (0.10, 1.0),
        (0.20, 0.7),
        (0.30, 0.5),
        (1.00, 0.3),
    ]

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)
        n_bars = len(closes)

        # 计算 RSI 和 MA150
        rsi = calc_rsi(closes, self.RSI_PERIOD)
        ma150 = calc_ma(closes, self.MA_PERIOD)

        # 止盈止损目标金额 (基于本金比例)
        tp_target = self.CAPITAL * self.TP_PCT   # +7.5U
        sl_target = self.CAPITAL * self.SL_PCT     # -4.5U

        balance = self.CAPITAL
        peak_balance = self.CAPITAL
        position = None              # 当前持仓 (None 或 dict)
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        # 预热期
        warmup = max(self.RSI_PERIOD, self.MA_PERIOD) + 1

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = closes[i]
            cur_rsi = rsi[i]
            cur_ma = ma150[i]

            # ---- 1. 检查现有持仓的止盈止损 ----
            if position is not None:
                pnl, should_close = self._check_tp_sl(position, price, tp_target, sl_target)

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

                # --- 做多条件: 价格低于MA150 + RSI<30 (均值回归抄底) ---
                if cur_rsi < self.RSI_OVERSOLD and price < cur_ma:
                    position = self._open_long(price, ts, actual_fraction, "L1", balance)

                # --- 做空条件: 价格远高于MA150(>105%) + RSI>70 (均值回归摸顶) ---
                # [方案A: 禁用做空, 纯做多]
                # elif cur_rsi > self.RSI_OVERBOUGHT and price > cur_ma * self.MA_HIGH_RATIO:
                #     position = self._open_short(price, ts, actual_fraction, "S1", balance)

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
        return 0.3

    def _open_long(self, price, ts, fraction, level, balance):
        """开多仓"""
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
        """开空仓"""
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
        """计算盈亏 (不含手续费)"""
        if pos["direction"] == "long":
            price_diff = current_price - pos["entry_price"]
        else:
            price_diff = pos["entry_price"] - current_price

        pct_change = price_diff / pos["entry_price"]
        pnl = pct_change * pos["size_usdt"]
        return pnl

    def _check_tp_sl(self, pos, current_price, tp_target, sl_target):
        """检查止盈止损, 返回 (pnl, should_close)"""
        pnl = self._calc_pnl(pos, current_price)

        if pnl >= tp_target:
            return pnl, True
        elif pnl <= -sl_target:
            return pnl, True
        return pnl, False
