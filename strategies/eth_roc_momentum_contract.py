"""ETH 双ROC动量策略 — 合约专属 (v12 + 动量衰竭阈值 + ATR自适应止盈止损)

与现货 eth_roc_momentum_v12.py 独立, 专为合约(USDⓈ-M Futures)优化:
  1. MOMENTUM_DEATH_THRESH: 动量衰竭出场需 ROC 跌破阈值(非0), 过滤震荡行情频繁出场
  2. ATR 自适应止盈止损: SL=1.5×ATR, TP=3.0×ATR (2:1 风险回报比), 替代固定U止损
  3. 新增止盈(TP): v12 仅靠动量衰竭出场, 合约策略增加 ATR 止盈锁定利润

数据周期: 1小时线 (1h)
本金: 150 USDT, 杠杆: 4x
"""
import numpy as np
from base import BaseStrategy, compute_stats


def calc_roc(close_series, period):
    """计算 Rate of Change (价格变化率 %)
    ROC(N) = (Price_t - Price_{t-N}) / Price_{t-N} × 100
    """
    n = len(close_series)
    roc = np.full(n, np.nan)
    for i in range(period, n):
        if close_series[i - period] > 0:
            roc[i] = (close_series[i] - close_series[i - period]) / close_series[i - period] * 100
    return roc


def calc_ma(close_series, period):
    """计算简单移动平均线"""
    ma = np.full(len(close_series), np.nan)
    if len(close_series) < period:
        return ma
    cumsum = np.cumsum(close_series)
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


def calc_atr(highs, lows, closes, period):
    """计算 ATR (Average True Range)

    TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
    ATR = TR 的简单移动平均

    用于动态止盈止损: 高波动放宽, 低波动收紧
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
        # 从 index=1 开始有有效 TR, SMA 窗口需对齐
        valid_tr = tr[1:]
        if len(valid_tr) >= period:
            cumsum = np.nancumsum(valid_tr)
            atr_sma = np.full(len(valid_tr), np.nan)
            atr_sma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
            # 回写到原数组 (index 0 保持 nan)
            atr[1:] = atr_sma
    return atr


class EthROCMomentumContract(BaseStrategy):
    """
    ETH 双ROC动量策略 — 合约专属

    核心思路: 用短期ROC和中期ROC捕捉价格涨跌速度,
             双ROC同向 + 成交量放大 = 趋势确认,
             动量衰竭(ROC反向跌破阈值) = 出场,
             ATR自适应止盈止损 = 锁定利润+控制风险。

    数据周期: 1小时线 (1h)
    本金: 150 USDT, 杠杆: 4x

    === 指标 ===
      ROC(短):  短期价格变化率 (涨速/跌速)
      ROC(中):  中期价格变化率 (趋势方向确认)
      VolMA:    成交量均线 (放量确认)
      ATR(14):  平均真实波幅 (动态止盈止损)

    === 入场规则 (方向互斥) ===
      做多: ROC(短) > 0 且 ROC(中) > 0 且 ROC(短) > ROC(中) (加速上涨)
            且 成交量 > VolMA (放量确认)
      做空: ROC(短) < 0 且 ROC(中) < 0 且 ROC(短) < ROC(中) (加速下跌)
            且 成交量 > VolMA (放量确认)

    === 出场规则 ===
      动量衰竭: 做多时 ROC(短) < -MOMENTUM_DEATH_THRESH → 平仓
               做空时 ROC(短) > MOMENTUM_DEATH_THRESH → 平仓
      止损(SL): 价格触及 entry_price - SL_ATR_MULT × ATR (做多)
               价格触及 entry_price + SL_ATR_MULT × ATR (做空)
      止盈(TP): 价格触及 entry_price + TP_ATR_MULT × ATR (做多)
               价格触及 entry_price - TP_ATR_MULT × ATR (做空)
      超时:     持仓 >= MAX_HOLD_BARS 根K线 → 强制平仓

    === 仓位管理 ===
      基础仓位: FRACTION_BASE = 0.25
      动态减仓: 回撤 10%/20%/30% → 仓位×1.0/0.7/0.5/0.3
    """

    name = "ETH双ROC动量策略-合约"
    CAPITAL = 150.0
    LEVERAGE = 8
    FRACTION_BASE = 0.25      # 基础仓位比例 (合约略降, 控制风险)
    FEE_RATE = 0.0004         # 手续费率

    ROC_SHORT = 8             # 短期ROC周期 (对齐v12, 反应更快)
    ROC_MEDIUM = 20            # 中期ROC周期 (对齐v12)
    VOL_MA_PERIOD = 20        # 成交量均线周期 (对齐v12)

    # 动量衰竭阈值: 0=穿零即出 (改回v12逻辑; 原0.8经验值导致利润回吐太重)
    MOMENTUM_DEATH_THRESH = 0

    # ATR 自适应止盈止损
    ATR_PERIOD = 14           # ATR计算周期 (保留, 做指标展示用)
    SL_ATR_MULT = 1.5         # 止损 = 1.5 × ATR
    TP_ATR_MULT = 999         # 关闭止盈 (设极大值=关闭; 让趋势跑完, 靠动量出场更优)

    MAX_HOLD_BARS = 72        # 最大持仓72根K线(3天, 对齐v12)

    # 回撤减仓阈值
    DRAWDOWN_THRESHOLDS = [
        (0.10, 1.0),
        (0.20, 0.7),
        (0.30, 0.5),
        (1.00, 0.3),
    ]

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)

        # 成交量
        if "volume" in df.columns:
            volumes = df["volume"].values.astype(float)
        else:
            volumes = np.ones(len(closes))

        n_bars = len(closes)

        # 计算指标
        roc5 = calc_roc(closes, self.ROC_SHORT)
        roc20 = calc_roc(closes, self.ROC_MEDIUM)
        vol_ma = calc_ma(volumes, self.VOL_MA_PERIOD)
        atr = calc_atr(highs, lows, closes, self.ATR_PERIOD)

        balance = self.CAPITAL
        peak_balance = self.CAPITAL
        position = None
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        warmup = max(self.ROC_MEDIUM, self.VOL_MA_PERIOD, self.ATR_PERIOD) + 2

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = closes[i]

            cur_roc5 = roc5[i]
            cur_roc20 = roc20[i]
            cur_vol = volumes[i]
            cur_vol_ma = vol_ma[i]
            cur_atr = atr[i]

            if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_vol_ma) or np.isnan(cur_atr):
                equity_curve.append((ts, round(balance, 4)))
                continue

            # ---- 1. 检查现有持仓出场条件 ----
            if position is not None:
                pnl = self._calc_pnl(position, price)
                held_bars = i - position["entry_bar"]
                should_close = False
                reason = ""

                # 动量衰竭 (带阈值, 过滤震荡)
                if position["direction"] == "long" and cur_roc5 < -self.MOMENTUM_DEATH_THRESH:
                    should_close = True
                    reason = "momentum_death"
                elif position["direction"] == "short" and cur_roc5 > self.MOMENTUM_DEATH_THRESH:
                    should_close = True
                    reason = "momentum_death"

                # ATR 止损
                if not should_close:
                    if position["direction"] == "long" and price <= position["sl_price"]:
                        should_close = True
                        reason = "SL"
                    elif position["direction"] == "short" and price >= position["sl_price"]:
                        should_close = True
                        reason = "SL"

                # ATR 止盈
                if not should_close:
                    if position["direction"] == "long" and price >= position["tp_price"]:
                        should_close = True
                        reason = "TP"
                    elif position["direction"] == "short" and price <= position["tp_price"]:
                        should_close = True
                        reason = "TP"

                # 最大持仓时间
                if not should_close and held_bars >= self.MAX_HOLD_BARS:
                    should_close = True
                    reason = "timeout"

                if should_close:
                    close_fee = position["size_usdt"] * self.FEE_RATE / 2
                    net_pnl = pnl - close_fee

                    trades.append({
                        "direction": position["direction"],
                        "entry_price": position["entry_price"],
                        "exit_price": round(price, 2),
                        "pnl": round(net_pnl, 4),
                        "entry_time": position["entry_time"],
                        "exit_time": ts,
                        "held_bars": held_bars,
                        "entry_roc5": position["entry_roc5"],
                        "entry_roc20": position["entry_roc20"],
                        "entry_atr": position["entry_atr"],
                        "sl_price": position["sl_price"],
                        "tp_price": position["tp_price"],
                        "exit_roc5": round(cur_roc5, 2),
                        "exit_roc20": round(cur_roc20, 2),
                        "reason": reason,
                    })
                    balance += net_pnl
                    position = None

            # 更新资金峰值和回撤
            if balance > peak_balance:
                peak_balance = balance
            current_drawdown = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0

            # ---- 2. 入场逻辑 (方向互斥, 单仓位) ----
            if position is None:
                position_multiplier = self._get_position_size(current_drawdown)
                actual_fraction = self.FRACTION_BASE * position_multiplier

                # 成交量确认
                vol_confirmed = cur_vol > cur_vol_ma

                if vol_confirmed:
                    # --- 做多: 双ROC正向 + 短期加速 ---
                    if cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20:
                        position = self._open_position("long", price, ts, i,
                                                       actual_fraction, balance,
                                                       cur_roc5, cur_roc20, cur_atr)

                    # --- 做空: 双ROC负向 + 短期加速下跌 ---
                    elif cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20:
                        position = self._open_position("short", price, ts, i,
                                                       actual_fraction, balance,
                                                       cur_roc5, cur_roc20, cur_atr)

            # 记录权益曲线
            unrealized = self._calc_unrealized(position, price)
            equity_curve.append((ts, round(balance + unrealized, 4)))

        # ---- 3. 期末强平 ----
        final_price = closes[-1]
        final_ts = int(timestamps[-1])
        if position is not None:
            pnl = self._calc_pnl(position, final_price)
            close_fee = position["size_usdt"] * self.FEE_RATE / 2
            net_pnl = pnl - close_fee
            trades.append({
                "direction": position["direction"],
                "entry_price": position["entry_price"],
                "exit_price": round(final_price, 2),
                "pnl": round(net_pnl, 4),
                "entry_time": position["entry_time"],
                "exit_time": final_ts,
                "held_bars": n_bars - 1 - position["entry_bar"],
                "entry_roc5": position["entry_roc5"],
                "entry_roc20": position["entry_roc20"],
                "entry_atr": position["entry_atr"],
                "sl_price": position["sl_price"],
                "tp_price": position["tp_price"],
                "exit_roc5": None,
                "exit_roc20": None,
                "reason": "force_close",
            })
            balance += net_pnl

        equity_curve.append((final_ts, round(balance, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        # 按方向拆分统计
        long_trades = [t for t in trades if t["direction"] == "long"]
        short_trades = [t for t in trades if t["direction"] == "short"]

        stats["long_count"] = len(long_trades)
        stats["long_pnl"] = round(sum(t["pnl"] for t in long_trades), 2) if long_trades else 0
        stats["short_count"] = len(short_trades)
        stats["short_pnl"] = round(sum(t["pnl"] for t in short_trades), 2) if short_trades else 0

        # 按出场原因统计 (新增 TP 止盈)
        for reason in ["momentum_death", "SL", "TP", "timeout", "force_close"]:
            reason_trades = [t for t in trades if t.get("reason") == reason]
            stats[f"n_{reason}"] = len(reason_trades)
            stats[f"pnl_{reason}"] = round(sum(t["pnl"] for t in reason_trades), 2) if reason_trades else 0

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

    def _get_position_size(self, drawdown):
        for threshold, multiplier in self.DRAWDOWN_THRESHOLDS:
            if drawdown <= threshold:
                return multiplier
        return 0.3

    def _open_position(self, direction, price, ts, bar_idx, fraction, balance,
                       entry_roc5, entry_roc20, entry_atr):
        """开仓: 计算动态SL/TP价格, 记录持仓"""
        notional = balance * fraction * self.LEVERAGE
        size_usdt = notional

        # ATR 自适应止盈止损价格
        if direction == "long":
            sl_price = price - self.SL_ATR_MULT * entry_atr
            tp_price = price + self.TP_ATR_MULT * entry_atr
        else:
            sl_price = price + self.SL_ATR_MULT * entry_atr
            tp_price = price - self.TP_ATR_MULT * entry_atr

        return {
            "direction": direction,
            "entry_price": price,
            "size_usdt": size_usdt,
            "fraction": fraction,
            "entry_time": ts,
            "entry_bar": bar_idx,
            "entry_roc5": round(entry_roc5, 2),
            "entry_roc20": round(entry_roc20, 2),
            "entry_atr": round(entry_atr, 2),
            "sl_price": round(sl_price, 2),
            "tp_price": round(tp_price, 2),
        }

    def _calc_unrealized(self, pos, current_price):
        if pos is None:
            return 0.0
        return self._calc_pnl(pos, current_price)

    def _calc_pnl(self, pos, current_price):
        if pos["direction"] == "long":
            price_diff = current_price - pos["entry_price"]
        else:
            price_diff = pos["entry_price"] - current_price
        pct_change = price_diff / pos["entry_price"]
        return pct_change * pos["size_usdt"]
