"""ETH 低位金字塔建仓策略 — 合约专属 (1h, 5年滚动分位, 金字塔分批+中位数止盈)

与 eth_roc_momentum_contract.py / eth_extreme_reversion_contract.py 完全独立。

核心: 价格处于5年滚动分位 ≤20% 时金字塔分批做多:
  T20(≤20%)买1份 → T15(≤15%)买2份 → T10(≤10%)买3份 → T05(≤5%)买4份
  最多10份, 每份5%保证金×2x=10% notional, 满仓50%保证金/100% notional.
  TP: 价格回到滚动50%分位(中位数)全部平仓. 无止损(金字塔均价下移即风控).

数据周期: 1h | 本金: 150 USDT | 杠杆: 2x

可选加速依赖: pip install sortedcontainers (分位数计算从~30s降到~3s)
"""
import numpy as np
import pandas as pd
from base import BaseStrategy, compute_stats


def calc_rolling_percentile(close_arr, window, min_periods):
    """滚动分位数 (0~1). 当前close在窗口内的排名比例.

    快路径: SortedList O(N log W) — 需 sortedcontainers
    慢路径: pandas rolling().rank(pct=True) — Cython加速, 约10-30s
    """
    n = len(close_arr)
    pct = np.full(n, np.nan)
    try:
        from sortedcontainers import SortedList
        sl = SortedList()
        for i in range(n):
            sl.add(float(close_arr[i]))
            if i >= window:
                sl.remove(float(close_arr[i - window]))
            cur_len = len(sl)
            if cur_len >= min_periods:
                rank = sl.index(float(close_arr[i]))
                pct[i] = rank / (cur_len - 1) if cur_len > 1 else 0.5
        return pct
    except ImportError:
        s = pd.Series(close_arr)
        return s.rolling(window=window, min_periods=min_periods).rank(pct=True).values


class EthLowPyramidContract(BaseStrategy):
    """
    ETH 低位金字塔建仓策略 — 合约专属

    核心思路: 价格处于5年滚动分位低位时, 金字塔分批建仓做多,
             越低买越多, 价格回到中位数(50%分位)止盈.
             目标跑赢买入持有(B&H)基准.

    数据周期: 1小时线 (1h)
    本金: 150 USDT, 杠杆: 2x

    === 金字塔档位 (从高阈值到低阈值) ===
      T20: 分位≤20% 买1份
      T15: 分位≤15% 买2份
      T10: 分位≤10% 买3份
      T05: 分位≤5%  买4份
      最多10份, 每份5%保证金×2x杠杆=10% notional

    === 出场规则 ===
      止盈: 滚动分位 ≥ 50% (中位数) → 全部平仓
      止损: 无 (金字塔越跌越买, 均价下移即风控)
      强平保护: 2x杠杆+50%buffer, 强平价642-853U远低于5年最低994U

    === 防抖 ===
      同档同周期只填一次 (tier_filled集合)
      TP后重置, 20%↔50%天然间隔防抖
    """

    name = "ETH低位金字塔建仓策略-合约"
    CAPITAL = 150.0
    LEVERAGE = 2
    FEE_RATE = 0.0004

    # --- 滚动分位数参数 ---
    ROLLING_WINDOW = 43800   # 5年(365*24*5)
    MIN_PERIODS = 8760       # 1年最小数据量才开始交易

    # --- 金字塔档位 (从高阈值到低阈值, 顺序勿改) ---
    TIERS = [
        {"name": "T20", "pct": 0.20, "shares": 1},
        {"name": "T15", "pct": 0.15, "shares": 2},
        {"name": "T10", "pct": 0.10, "shares": 3},
        {"name": "T05", "pct": 0.05, "shares": 4},
    ]
    FRACTION_PER_SHARE = 0.05   # 每份保证金比例 (5%本金)
    TP_PERCENTILE = 0.50        # 止盈分位
    MAX_HOLD_BARS = 0           # 0=不限制 (低位策略本质是持有至复苏)

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)
        n_bars = len(closes)

        # 计算滚动分位数
        pct_arr = calc_rolling_percentile(closes, self.ROLLING_WINDOW, self.MIN_PERIODS)

        balance = self.CAPITAL
        position = None
        tier_filled = set()
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]
        warmup = self.MIN_PERIODS

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = float(closes[i])
            cur_pct = pct_arr[i]

            if np.isnan(cur_pct):
                equity_curve.append((ts, round(balance, 4)))
                continue

            # ---- 1. 出场: 滚动50%分位全部平仓 ----
            if position is not None:
                should_close = False
                reason = ""
                if cur_pct >= self.TP_PERCENTILE:
                    should_close = True
                    reason = "TP"
                elif self.MAX_HOLD_BARS > 0 and (i - position["entry_bar_first"]) >= self.MAX_HOLD_BARS:
                    should_close = True
                    reason = "timeout"

                if should_close:
                    net_pnl = self._close_position(position, price)
                    trades.append(self._make_trade(position, price, ts, i, net_pnl, reason))
                    balance += net_pnl
                    position = None
                    tier_filled = set()

            # ---- 2. 入场/加仓: 扫描未填充档 (从高阈值到低阈值) ----
            new_tiers = [t for t in self.TIERS
                         if t["name"] not in tier_filled and cur_pct <= t["pct"]]
            if new_tiers:
                if position is None:
                    position = self._init_position(new_tiers, price, ts, i, balance)
                else:
                    self._add_tiers(position, new_tiers, price, ts, i, balance)
                tier_filled.update(t["name"] for t in new_tiers)

            # ---- 3. 权益曲线 ----
            unrealized = self._calc_unrealized(position, price) if position else 0.0
            equity_curve.append((ts, round(balance + unrealized, 4)))

        # ---- 4. 期末强平 ----
        final_price = float(closes[-1])
        final_ts = int(timestamps[-1])
        if position is not None:
            net_pnl = self._close_position(position, final_price)
            trades.append(self._make_trade(position, final_price, final_ts,
                                            n_bars - 1, net_pnl, "force_close"))
            balance += net_pnl

        equity_curve.append((final_ts, round(balance, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        # 档位组合统计
        from collections import Counter
        combo_counter = Counter(t["level"] for t in trades)
        for combo, cnt in combo_counter.most_common():
            stats[f"n_{combo}"] = cnt
            combo_pnl = sum(t["pnl"] for t in trades if t["level"] == combo)
            stats[f"pnl_{combo}"] = round(combo_pnl, 2)

        # 出场原因统计
        for reason in ["TP", "timeout", "force_close"]:
            reason_trades = [t for t in trades if t.get("reason") == reason]
            stats[f"n_{reason}"] = len(reason_trades)
            stats[f"pnl_{reason}"] = round(sum(t["pnl"] for t in reason_trades), 2) if reason_trades else 0

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

    def _share_notional(self, shares, balance):
        """单档 notional = 每份保证金 × 份数 × 杠杆"""
        return balance * self.FRACTION_PER_SHARE * shares * self.LEVERAGE

    def _init_position(self, tiers, price, ts, bar, balance):
        """初始化持仓"""
        pos = {
            "tiers": [],
            "total_shares": 0,
            "total_size_usdt": 0.0,
            "entry_time_first": ts,
            "entry_bar_first": bar,
            "tiers_filled": set(),
            "avg_entry_price": 0.0,
        }
        self._add_tiers(pos, tiers, price, ts, bar, balance)
        return pos

    def _add_tiers(self, pos, tiers, price, ts, bar, balance):
        """追加档位, 更新加权均价"""
        for t in tiers:
            size = self._share_notional(t["shares"], balance)
            pos["tiers"].append({
                "tier": t["name"],
                "entry_price": round(price, 2),
                "shares": t["shares"],
                "entry_time": ts,
                "entry_bar": bar,
                "size_usdt": round(size, 2),
            })
            pos["total_shares"] += t["shares"]
            pos["total_size_usdt"] += size
            pos["tiers_filled"].add(t["name"])
        # 加权均价
        tot_notional = sum(x["entry_price"] * x["shares"] for x in pos["tiers"])
        pos["avg_entry_price"] = round(tot_notional / pos["total_shares"], 2)

    def _calc_unrealized(self, pos, price):
        """计算未实现盈亏"""
        if pos is None:
            return 0.0
        return sum((price - x["entry_price"]) / x["entry_price"] * x["size_usdt"]
                   for x in pos["tiers"])

    def _close_position(self, pos, exit_price):
        """总净PnL. 手续费: 每档 close_fee = size × FEE_RATE/2 (仅平仓侧, 镜像现有合约策略)"""
        gross = sum((exit_price - x["entry_price"]) / x["entry_price"] * x["size_usdt"]
                    for x in pos["tiers"])
        fees = sum(x["size_usdt"] * self.FEE_RATE / 2 for x in pos["tiers"])
        return gross - fees

    def _make_trade(self, pos, exit_price, exit_ts, exit_bar, net_pnl, reason):
        """构造兼容 generate_html_report 的 trade dict (一个金字塔周期=一笔trade)"""
        return {
            "direction": "long",
            "level": "+".join(sorted(pos["tiers_filled"])),
            "entry_price": pos["avg_entry_price"],
            "exit_price": round(float(exit_price), 2),
            "pnl": round(net_pnl, 4),
            "entry_time": pos["entry_time_first"],
            "exit_time": exit_ts,
            "held_bars": exit_bar - pos["entry_bar_first"],
            "shares": pos["total_shares"],
            "reason": reason,
        }
