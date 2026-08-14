# -*- coding: utf-8 -*-
"""跑合约回测, 结果写入文件"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import pandas as pd
from strategies.eth_roc_momentum_contract import EthROCMomentumContract

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result_contract.txt")
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
    time.strftime("%Y-%m-%d %H:%M", time.localtime(int(df["timestamp"].iloc[-1]) / 1000)),
))

s = EthROCMomentumContract()
log("策略: %s | 本金=%s 杠杆=%sx 仓位=%s%% | ROC(%s/%s) VolMA=%s" % (
    s.name, s.CAPITAL, s.LEVERAGE, s.FRACTION_BASE * 100, s.ROC_SHORT, s.ROC_MEDIUM, s.VOL_MA_PERIOD))
log("ATR止损=%sxATR 止盈=%s 动量衰竭阈=%s 最大持仓=%s根 手续费=%s" % (
    s.SL_ATR_MULT, s.TP_ATR_MULT, s.MOMENTUM_DEATH_THRESH, s.MAX_HOLD_BARS, s.FEE_RATE))
log("=" * 60)

t0 = time.time()
r = s.run_backtest(df)
log("回测耗时 %.1fs" % (time.time() - t0))
st = r["stats"]
for k, v in st.items():
    if isinstance(v, (int, float)):
        log("  %-18s %s" % (k, v))
    elif isinstance(v, list):
        log("  %-18s [%d项]" % (k, len(v)))

trades = r["trades"]
log("=" * 60)
log("出场原因分布:")
for reason in ["momentum_death", "SL", "TP", "timeout", "force_close"]:
    n = st.get("n_" + reason, 0)
    p = st.get("pnl_" + reason, 0)
    if n:
        log("  %s: %d笔, PnL=%+.2f" % (reason, n, p))
log("=" * 60)
log("最近 8 笔交易:")
for t in trades[-8:]:
    log("  %s %s 开@%.2f→平@%.2f PnL=%+.4fU 持仓%sbars %s" % (
        t["direction"], t.get("level", "?"), t["entry_price"], t["exit_price"],
        t["pnl"], t["held_bars"], t["reason"]))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE ->", OUT)
