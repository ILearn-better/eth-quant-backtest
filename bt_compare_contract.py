# -*- coding: utf-8 -*-
"""对比: 合约基础版(8x) vs 合约共振版(20x)"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from strategies.eth_roc_momentum_contract import EthROCMomentumContract
from strategies.eth_roc_momentum_contract_resonance import EthROCMomentumContractResonance

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result_resonance.txt")
lines = []


def log(s=""):
    lines.append(s)
    print(s)


df = pd.read_csv("data/futures/ETHUSDT-1h.csv")
df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
for c in ["open", "high", "low", "close"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
if "volume" in df.columns:
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
df.dropna(subset=["close", "timestamp"], inplace=True)
df.sort_values("timestamp", inplace=True)
df.reset_index(drop=True, inplace=True)

log("合约数据: %d 根 | %s ~ %s" % (
    len(df),
    time.strftime("%Y-%m-%d", time.localtime(int(df["timestamp"].iloc[0]) / 1000)),
    time.strftime("%Y-%m-%d", time.localtime(int(df["timestamp"].iloc[-1]) / 1000)),
))

strategies = [
    EthROCMomentumContract(),
    EthROCMomentumContractResonance(),
]

for s in strategies:
    log("")
    log("=" * 64)
    log("策略: %s" % s.name)
    log("杠杆=%sx 仓位=%s%% 有效杠杆=%sx | ROC(%s/%s/%s) VolMA=%s MA=%s ATR=%sx 动量衰竭=%s" % (
        s.LEVERAGE, s.FRACTION_BASE * 100, s.LEVERAGE * s.FRACTION_BASE,
        s.ROC_SHORT, s.ROC_MEDIUM, getattr(s, "ROC_LONG", "-"),
        s.VOL_MA_PERIOD, getattr(s, "TREND_MA_PERIOD", "-"),
        s.SL_ATR_MULT, s.MOMENTUM_DEATH_THRESH))
    log("=" * 64)
    t0 = time.time()
    r = s.run_backtest(df)
    log("回测耗时 %.1fs" % (time.time() - t0))
    st = r["stats"]
    for k in ["total_trades", "win_rate", "total_pnl", "return_pct", "max_drawdown",
              "profit_factor", "avg_win", "avg_loss", "best_trade", "worst_trade",
              "final_capital", "sharpe_ratio", "long_count", "long_pnl",
              "short_count", "short_pnl"]:
        log("  %-18s %s" % (k, st.get(k)))
    log("  出场原因:")
    for reason in ["momentum_death", "SL", "TP", "timeout"]:
        n = st.get("n_" + reason, 0)
        p = st.get("pnl_" + reason, 0)
        if n:
            log("    %-15s %d笔, PnL=%+.2f" % (reason, n, p))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE ->", OUT)
