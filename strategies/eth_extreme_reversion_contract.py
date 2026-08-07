"""ETH 极端行情反弹策略 — 合约专属 (1h超跌反弹做多)

与 eth_roc_momentum_contract.py 完全独立, 互不影响。
触发: 1h close-to-close 跌幅 > 5%
方向: 仅做多 (超跌反弹)
出场: 固定%止盈(分档) / 固定%止损 / 24h超时

数据依据 (近5年 ETHUSDT-1h 合约数据):
  - 1h跌>5% 共33次, 24h内平均反弹10.51%, 中位数9.08%
  - 1h跌>8% 后24h平均反弹17.16% (越极端反弹越强 → 分档止盈)

数据周期: 1h | 本金: 150 USDT | 杠杆: 3x (低于趋势策略8x, 逆势接刀需保守)
"""
import numpy as np
from base import BaseStrategy, compute_stats


class EthExtremeReversionContract(BaseStrategy):
    """
    ETH 极端行情反弹策略 — 合约专属

    核心思路: 1h 超跌后大概率有反弹, 固定%止盈止损捕捉反弹利润。
             与趋势策略互补: 趋势策略在常态行情赚钱, 反弹策略在极端行情赚钱。

    数据周期: 1小时线 (1h)
    本金: 150 USDT, 杠杆: 3x

    === 触发条件 ===
      1h close-to-close 跌幅 <= -5% → 下一根开盘价做多
      (用 drop_pct[i-1] 判断, open[i] 进场, 严格无未来函数)

    === 分档止盈 (落实"越极端反弹越强") ===
      普通档 (跌5%~8%): 止盈 +8%
      极端档 (跌>8%):   止盈 +12%

    === 出场规则 ===
      止盈: 价格触及 entry_price × (1 + TP_PCT)
      止损: 价格触及 entry_price × (1 - SL_PCT)  (-5%)
      超时: 持仓 >= 24 根K线 (24h)
      intrabar 用 high/low 触发 (比仅用close更真实)

    === 风控 ===
      冷却期: 出场后 3 根K线内不开仓 (防连跌反复接刀)
      回撤减仓: 10%/20%/30% → 仓位×1.0/0.7/0.5/0.3
    """

    name = "ETH极端行情反弹策略-合约"
    CAPITAL = 150.0
    LEVERAGE = 3              # 低杠杆(逆势接刀, 入场在极端波动后)
    FRACTION_BASE = 0.30
    FEE_RATE = 0.0004

    # 触发阈值 (1h close-to-close 跌幅%)
    DROP_THRESH = -5.0        # 1h跌>5% 触发
    DROP_EXTREME = -8.0       # 1h跌>8% 极端档 (更高止盈)

    # 止盈止损 (固定%, 不用ATR — 极端行情后ATR失真)
    TP_PCT_NORMAL = 0.08      # 普通档止盈 +8% (接近中位数9.08%)
    TP_PCT_EXTREME = 0.12     # 极端档止盈 +12%
    SL_PCT = 0.05             # 止损 -5%

    MAX_HOLD_BARS = 24        # 24h 反弹窗口
    COOLDOWN_BARS = 3         # 出场后冷却3根 (防连跌接刀)

    # 回撤减仓阈值 (复用现有模式)
    DRAWDOWN_THRESHOLDS = [
        (0.10, 1.0),
        (0.20, 0.7),
        (0.30, 0.5),
        (1.00, 0.3),
    ]

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)

        n_bars = len(closes)

        # 1h close-to-close 跌幅%
        drop_pct = np.full(n_bars, np.nan)
        drop_pct[1:] = (closes[1:] / closes[:-1] - 1.0) * 100.0

        balance = self.CAPITAL
        peak_balance = self.CAPITAL
        position = None
        last_exit_bar = -10**9
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        warmup = 2  # drop_pct 从 i=1 起有效

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            # ---- 1. 入场: 上一根触发超跌 → 本根开盘价进场 (无未来函数) ----
            if position is None and (i - 1) >= warmup:
                prev_drop = drop_pct[i - 1]
                if (not np.isnan(prev_drop)
                        and prev_drop <= self.DROP_THRESH
                        and (i - last_exit_bar) > self.COOLDOWN_BARS):
                    tier = "extreme" if prev_drop <= self.DROP_EXTREME else "normal"
                    if balance > peak_balance:
                        peak_balance = balance
                    dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                    frac = self.FRACTION_BASE * self._get_position_size(dd)
                    position = self._open_position(o, ts, i, tier, frac, balance, prev_drop)

            # ---- 2. 出场: intrabar high/low 检查止盈止损 ----
            if position is not None:
                held_bars = i - position["entry_bar"]
                exit_price, reason = self._check_exit(position, h, l, c, held_bars)
                if reason:
                    pnl = self._calc_pnl(position, exit_price)
                    close_fee = position["size_usdt"] * self.FEE_RATE / 2
                    net_pnl = pnl - close_fee

                    trades.append({
                        "direction": "long",
                        "level": position["tier"],
                        "entry_price": position["entry_price"],
                        "exit_price": round(exit_price, 2),
                        "pnl": round(net_pnl, 4),
                        "entry_time": position["entry_time"],
                        "exit_time": ts,
                        "held_bars": held_bars,
                        "entry_drop": position["entry_drop"],
                        "sl_price": position["sl_price"],
                        "tp_price": position["tp_price"],
                        "reason": reason,
                    })
                    balance += net_pnl
                    last_exit_bar = i
                    position = None

            # 更新资金峰值
            if balance > peak_balance:
                peak_balance = balance
            # 记录权益曲线
            unrealized = self._calc_pnl(position, c) if position else 0.0
            equity_curve.append((ts, round(balance + unrealized, 4)))

        # ---- 3. 期末强平 ----
        final_price = closes[-1]
        final_ts = int(timestamps[-1])
        if position is not None:
            pnl = self._calc_pnl(position, final_price)
            close_fee = position["size_usdt"] * self.FEE_RATE / 2
            net_pnl = pnl - close_fee
            trades.append({
                "direction": "long",
                "level": position["tier"],
                "entry_price": position["entry_price"],
                "exit_price": round(final_price, 2),
                "pnl": round(net_pnl, 4),
                "entry_time": position["entry_time"],
                "exit_time": final_ts,
                "held_bars": n_bars - 1 - position["entry_bar"],
                "entry_drop": position["entry_drop"],
                "sl_price": position["sl_price"],
                "tp_price": position["tp_price"],
                "reason": "force_close",
            })
            balance += net_pnl

        equity_curve.append((final_ts, round(balance, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        # 分档统计
        for tier in ["normal", "extreme"]:
            tier_trades = [t for t in trades if t["level"] == tier]
            stats[f"n_{tier}"] = len(tier_trades)
            stats[f"pnl_{tier}"] = round(sum(t["pnl"] for t in tier_trades), 2) if tier_trades else 0

        # 出场原因统计
        for reason in ["TP", "SL", "timeout", "force_close"]:
            reason_trades = [t for t in trades if t.get("reason") == reason]
            stats[f"n_{reason}"] = len(reason_trades)
            stats[f"pnl_{reason}"] = round(sum(t["pnl"] for t in reason_trades), 2) if reason_trades else 0

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

    def _open_position(self, entry_price, ts, bar_idx, tier, fraction, balance, entry_drop):
        """开仓: 计算分档止盈/止损价格"""
        notional = balance * fraction * self.LEVERAGE
        tp_pct = self.TP_PCT_EXTREME if tier == "extreme" else self.TP_PCT_NORMAL
        return {
            "direction": "long",
            "tier": tier,
            "entry_price": round(float(entry_price), 2),
            "size_usdt": round(notional, 2),
            "fraction": round(fraction, 4),
            "entry_time": int(ts),
            "entry_bar": bar_idx,
            "entry_drop": round(float(entry_drop), 2),
            "sl_price": round(float(entry_price * (1 - self.SL_PCT)), 2),
            "tp_price": round(float(entry_price * (1 + tp_pct)), 2),
        }

    def _check_exit(self, pos, high, low, close, held_bars):
        """intrabar 检查止盈止损. 同根SL/TP都触及时保守假设SL先触发."""
        sl_hit = low <= pos["sl_price"]
        tp_hit = high >= pos["tp_price"]
        if sl_hit and tp_hit:
            return pos["sl_price"], "SL"      # 保守: 假设SL先触发
        if sl_hit:
            return pos["sl_price"], "SL"
        if tp_hit:
            return pos["tp_price"], "TP"
        if held_bars >= self.MAX_HOLD_BARS:
            return close, "timeout"
        return 0.0, ""

    def _get_position_size(self, drawdown):
        """根据回撤动态调整仓位倍数 (复用现有模式)"""
        for threshold, multiplier in self.DRAWDOWN_THRESHOLDS:
            if drawdown <= threshold:
                return multiplier
        return 0.3

    def _calc_pnl(self, pos, current_price):
        """计算持仓盈亏 (仅做多)"""
        pct_change = (current_price - pos["entry_price"]) / pos["entry_price"]
        return pct_change * pos["size_usdt"]
