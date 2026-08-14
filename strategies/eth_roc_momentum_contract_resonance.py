"""ETH 双ROC动量策略 — 合约共振版 (五维信号共振 + 20x 杠杆)

与 eth_roc_momentum_contract.py (基础版) 独立, 专为高杠杆合约设计:

高杠杆(20x)必须配高胜率, 入场改为【五维信号共振, 缺一不开仓】:
  ① ROC(8) 短期加速 (速度为正)
  ② ROC(20) 中期趋势 (方向同向)
  ③ ROC(8) > ROC(20) (加速度为正)
  ④ 成交量 > VolMA(20) (量能燃料确认)
  ⑤ 价格 > MA(50) (2天趋势线, 时间框架共振, 做多) / < MA(50) (做空)
  ⑥ ROC(50) 同向 (2天动量确认)
  ⑦ ATR(14) > ATR均值(50) (波动放大期, 趋势发动)

出场 (多空对称, 不预设方向偏好):
  动量衰竭: ROC(8) 反向穿零
  ATR止损: 1.5 × ATR (波动率自适应)
  超时:     72根K线

数据周期: 1小时线 (1h) | 本金: 150 USDT | 杠杆: 20x | 基础仓位: 20%
"""
import numpy as np
from base import BaseStrategy, compute_stats


def calc_roc(close_series, period):
    """ROC(N) = (Price_t - Price_{t-N}) / Price_{t-N} × 100"""
    n = len(close_series)
    roc = np.full(n, np.nan)
    for i in range(period, n):
        if close_series[i - period] > 0:
            roc[i] = (close_series[i] - close_series[i - period]) / close_series[i - period] * 100
    return roc


def calc_ma(values, period):
    """简单移动平均 (支持 NaN 前缀)"""
    ma = np.full(len(values), np.nan)
    if len(values) < period:
        return ma
    cumsum = np.nancumsum(np.where(np.isnan(values), 0, values))
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


def calc_atr(highs, lows, closes, period):
    """ATR: TR 的简单移动平均, TR = max(H-L, |H-prevC|, |L-prevC|)"""
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


class EthROCMomentumContractResonance(BaseStrategy):
    """
    ETH 双ROC动量策略 — 合约共振版

    核心思路: 高杠杆(20x)必须高胜率 → 五维信号共振(趋势/加速度/量能/时间框架/波动率)
             全部同向才入场, 出场保持动量衰竭+ATR止损+超时(多空对称)。

    入场 (方向互斥, 7 条件全部满足):
      做多: ROC(8)>0 且 ROC(20)>0 且 ROC(8)>ROC(20)
            且 量>VolMA(20) 且 close>MA(50) 且 ROC(50)>0 且 ATR(14)>MA(ATR,50)
      做空: 全镜像

    出场:
      动量衰竭: 做多时 ROC(8)<0 → 平; 做空时 ROC(8)>0 → 平
      ATR止损:  entry ∓ 1.5×ATR
      超时:      72 根K线
    """

    name = "ETH双ROC共振策略-合约20x"
    CAPITAL = 150.0
    LEVERAGE = 20              # 高杠杆: 必须高胜率支撑
    FRACTION_BASE = 0.20       # 基础仓位 (20x杠杆下压缩仓位, 有效杠杆4x)
    FEE_RATE = 0.0004

    ROC_SHORT = 8
    ROC_MEDIUM = 20
    ROC_LONG = 50              # 2天动量确认 (时间框架共振)
    VOL_MA_PERIOD = 20
    TREND_MA_PERIOD = 50       # 2天趋势线
    ATR_PERIOD = 14
    ATR_STATE_PERIOD = 50      # ATR 状态均线

    MOMENTUM_DEATH_THRESH = 0
    SL_ATR_MULT = 1.5
    TP_ATR_MULT = 999          # 关闭止盈, 靠动量出场
    MAX_HOLD_BARS = 72

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

        if "volume" in df.columns:
            volumes = df["volume"].values.astype(float)
        else:
            volumes = np.ones(len(closes))

        n_bars = len(closes)

        roc_short = calc_roc(closes, self.ROC_SHORT)
        roc_medium = calc_roc(closes, self.ROC_MEDIUM)
        roc_long = calc_roc(closes, self.ROC_LONG)
        vol_ma = calc_ma(volumes, self.VOL_MA_PERIOD)
        trend_ma = calc_ma(closes, self.TREND_MA_PERIOD)
        atr = calc_atr(highs, lows, closes, self.ATR_PERIOD)
        atr_state = calc_ma(atr, self.ATR_STATE_PERIOD)

        balance = self.CAPITAL
        peak_balance = self.CAPITAL
        position = None
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        warmup = max(self.ROC_LONG, self.TREND_MA_PERIOD, self.ATR_STATE_PERIOD,
                     self.VOL_MA_PERIOD) + 2

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = closes[i]

            cur_roc5 = roc_short[i]
            cur_roc20 = roc_medium[i]
            cur_roc50 = roc_long[i]
            cur_vol = volumes[i]
            cur_vol_ma = vol_ma[i]
            cur_trend_ma = trend_ma[i]
            cur_atr = atr[i]
            cur_atr_state = atr_state[i]

            if np.isnan(cur_roc5) or np.isnan(cur_roc20) or np.isnan(cur_roc50) \
               or np.isnan(cur_vol_ma) or np.isnan(cur_trend_ma) \
               or np.isnan(cur_atr) or np.isnan(cur_atr_state):
                equity_curve.append((ts, round(balance, 4)))
                continue

            # ---- 1. 出场条件 (多空对称) ----
            if position is not None:
                pnl = self._calc_pnl(position, price)
                held_bars = i - position["entry_bar"]
                should_close = False
                reason = ""

                if position["direction"] == "long" and cur_roc5 < -self.MOMENTUM_DEATH_THRESH:
                    should_close, reason = True, "momentum_death"
                elif position["direction"] == "short" and cur_roc5 > self.MOMENTUM_DEATH_THRESH:
                    should_close, reason = True, "momentum_death"

                if not should_close:
                    if position["direction"] == "long" and price <= position["sl_price"]:
                        should_close, reason = True, "SL"
                    elif position["direction"] == "short" and price >= position["sl_price"]:
                        should_close, reason = True, "SL"

                if not should_close:
                    if position["direction"] == "long" and price >= position["tp_price"]:
                        should_close, reason = True, "TP"
                    elif position["direction"] == "short" and price <= position["tp_price"]:
                        should_close, reason = True, "TP"

                if not should_close and held_bars >= self.MAX_HOLD_BARS:
                    should_close, reason = True, "timeout"

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

            if balance > peak_balance:
                peak_balance = balance
            current_drawdown = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0

            # ---- 2. 入场: 五维共振 ----
            if position is None:
                position_multiplier = self._get_position_size(current_drawdown)
                actual_fraction = self.FRACTION_BASE * position_multiplier

                vol_confirmed = cur_vol > cur_vol_ma
                # 波动放大期: 当前波动高于50期平均 (趋势发动)
                atr_expanding = cur_atr > cur_atr_state

                if vol_confirmed and atr_expanding:
                    # 做多共振: 双ROC加速 + 2天趋势线之上 + 2天动量正向
                    if (cur_roc5 > 0 and cur_roc20 > 0 and cur_roc5 > cur_roc20
                            and price > cur_trend_ma and cur_roc50 > 0):
                        position = self._open_position("long", price, ts, i,
                                                       actual_fraction, balance,
                                                       cur_roc5, cur_roc20, cur_atr)
                    # 做空共振 (镜像)
                    elif (cur_roc5 < 0 and cur_roc20 < 0 and cur_roc5 < cur_roc20
                          and price < cur_trend_ma and cur_roc50 < 0):
                        position = self._open_position("short", price, ts, i,
                                                       actual_fraction, balance,
                                                       cur_roc5, cur_roc20, cur_atr)

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

        long_trades = [t for t in trades if t["direction"] == "long"]
        short_trades = [t for t in trades if t["direction"] == "short"]
        stats["long_count"] = len(long_trades)
        stats["long_pnl"] = round(sum(t["pnl"] for t in long_trades), 2) if long_trades else 0
        stats["short_count"] = len(short_trades)
        stats["short_pnl"] = round(sum(t["pnl"] for t in short_trades), 2) if short_trades else 0

        for reason in ["momentum_death", "SL", "TP", "timeout", "force_close"]:
            rt = [t for t in trades if t.get("reason") == reason]
            stats[f"n_{reason}"] = len(rt)
            stats[f"pnl_{reason}"] = round(sum(t["pnl"] for t in rt), 2) if rt else 0

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
        notional = balance * fraction * self.LEVERAGE

        if direction == "long":
            sl_price = price - self.SL_ATR_MULT * entry_atr
            tp_price = price + self.TP_ATR_MULT * entry_atr
        else:
            sl_price = price + self.SL_ATR_MULT * entry_atr
            tp_price = price - self.TP_ATR_MULT * entry_atr

        return {
            "direction": direction,
            "entry_price": price,
            "size_usdt": notional,
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
