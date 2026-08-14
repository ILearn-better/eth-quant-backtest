"""Alpha 因子回测引擎 (时序单标的, 连续仓位)

流程:
    表达式 → 因子值序列 → 滚动 z-score → 连续仓位 clip(z/阈值, -1, 1)
        → 与未来一期收益相乘(信号 t-1 作用于收益 t, 无未来函数)
        → 扣除换手手续费 → 净值曲线 → 指标(年化/夏普/回撤/换手/IC)

数据格式: 与 data/ETHUSDT-1h.csv 一致
    timestamp, open, high, low, close, volume, ...
"""
import os

import numpy as np
import pandas as pd

from alpha_lab.operators import FIELDS
from alpha_lab.parser import evaluate_expression

# 年化期数 (7x24 市场)
PPY = {"1h": 24 * 365, "1d": 365, "4h": 6 * 365, "15m": 96 * 365, "5m": 288 * 365}


def load_data(csv_path):
    """加载 K 线 CSV → 字段 dict (含 returns 对数收益率)"""
    df = pd.read_csv(csv_path)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.dropna().reset_index(drop=True)
    data = {f: df[f].to_numpy(dtype=float) for f in FIELDS if f in df.columns}
    # 对数收益率
    close = data["close"]
    ret = np.full_like(close, np.nan)
    ret[1:] = np.log(close[1:] / close[:-1])
    data["returns"] = ret
    return df, data


def continuous_position(z, threshold=1.0, deadzone=0.3):
    """连续仓位映射: z 超过 ±阈值 → 满仓 ±1, 中间线性过渡.

    deadzone: |z| 低于该值 → 空仓(0), 减少噪声引起的无效调仓与手续费.
    """
    z = np.asarray(z, dtype=float)
    if deadzone > 0:
        z = np.where(np.abs(z) < deadzone, 0.0, z)
    pos = np.clip(z / threshold if threshold > 0 else np.sign(z), -1.0, 1.0)
    return pos


def run_backtest(expr, data, df=None, *, z_window=60, threshold=1.0,
                 deadzone=0.3, fee_rate=0.0005, leverage=1.0, ppy="1h"):
    """运行因子回测.

    Args:
        expr: 因子表达式
        data: 字段 dict (load_data 输出)
        df:   原始 DataFrame (用于返回时间戳), 可省略
        z_window: 滚动 z-score 窗口
        threshold: 仓位阈值 (±threshold 对应满仓)
        deadzone: 死区, |z| 低于该值空仓
        fee_rate: 单边手续费率 (按名义仓位收)
        leverage: 杠杆倍数 (收益与手续费均按名义仓位)
        ppy: 年化期数键 ("1h"/"1d")

    Returns:
        dict: 指标 + 序列
    """
    # 1. 求值因子
    factor = np.asarray(evaluate_expression(expr, data), dtype=float)

    # 2. 滚动 z-score → 连续仓位 (信号 t-1 作用于 t 期收益)
    s = pd.Series(factor)
    mean = s.rolling(window=z_window, min_periods=z_window).mean()
    std = s.rolling(window=z_window, min_periods=z_window).std(ddof=0)
    z = (s - mean) / std.replace(0, np.nan)
    pos = continuous_position(z.to_numpy(), threshold, deadzone)   # t 期信号 (杠杆前, ±1)
    pos_prev = np.concatenate([[0.0], pos[:-1]])                   # 上一期信号

    # 3. 收益与成本 (杠杆放大名义仓位)
    ret = pd.Series(data["returns"])
    strat_ret = pos_prev * ret.to_numpy() * leverage
    cost = np.abs(np.diff(np.concatenate([[0.0], pos]))) * fee_rate * leverage
    cost = np.concatenate([[0.0], cost[:-1]])
    net_ret = strat_ret - cost

    # 4. 净值与指标
    valid = ~np.isnan(net_ret)
    eq = np.nancumprod(1 + np.where(valid, net_ret, 0.0))
    eq_series = pd.Series(eq)

    n = len(net_ret)
    ppy_n = PPY.get(ppy, 24 * 365)
    total_ret = eq[-1] - 1
    annual_ret = (1 + total_ret) ** (ppy_n / max(n, 1)) - 1 if total_ret > -1 else -1

    valid_ret = net_ret[valid]
    if len(valid_ret) > 1:
        sharpe = np.nanmean(valid_ret) / np.nanstd(valid_ret, ddof=0) * np.sqrt(ppy_n) \
            if np.nanstd(valid_ret, ddof=0) > 0 else 0.0
    else:
        sharpe = 0.0

    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    win_rate = float((np.asarray(valid_ret) > 0).mean()) if len(valid_ret) else 0.0
    turnover = float(np.nanmean(np.abs(np.diff(pos_prev)))) if len(pos_prev) > 1 else 0.0

    # 5. IC (信号 t-1 与收益 t 的相关性, 去除 NaN 后对齐)
    sig = pos_prev[1:]     # t-1 期信号
    rtn = ret.to_numpy()[1:]  # t 期收益
    valid_ic = ~np.isnan(sig) & ~np.isnan(rtn)
    ic_vals = 0.0
    if valid_ic.sum() > 5:
        corr = np.corrcoef(sig[valid_ic], rtn[valid_ic])
        ic_vals = float(corr[0, 1]) if np.isfinite(corr[0, 1]) else 0.0
    # Spearman IC (秩相关)
    sp_ic = 0.0
    if valid_ic.sum() > 5:
        sp = np.corrcoef(
            pd.Series(sig[valid_ic]).rank().to_numpy(),
            pd.Series(rtn[valid_ic]).rank().to_numpy())
        sp_ic = float(sp[0, 1]) if np.isfinite(sp[0, 1]) else 0.0

    # 6. Fitness (世坤近似): Sharpe * sqrt(|年化收益|)
    fitness = sharpe * np.sqrt(abs(annual_ret))

    # 7. 序列输出
    times = df["timestamp"].to_numpy() if df is not None and "timestamp" in df else \
        np.arange(n, dtype=np.int64)
    result = {
        "metrics": {
            "total_return": round(float(total_ret) * 100, 2),   # %
            "annual_return": round(float(annual_ret) * 100, 2), # %
            "sharpe": round(float(sharpe), 3),
            "max_drawdown": round(float(max_dd) * 100, 2),      # %
            "win_rate": round(float(win_rate) * 100, 2),        # %
            "turnover": round(float(turnover), 4),              # 每期仓位变化均值
            "ic": round(float(ic_vals), 4),
            "ic_spearman": round(float(sp_ic), 4),
            "fitness": round(float(fitness), 3),
            "periods": int(n),
        },
        "series": {
            "times": times.tolist(),
            "factor": _clean(factor),
            "position": _clean(pos),
            "equity": _clean(eq),
            "drawdown": _clean(drawdown),
        },
    }
    return result


def _clean(a):
    """NaN → None, 便于 JSON 输出"""
    a = np.asarray(a, dtype=float)
    return [None if np.isnan(v) else round(float(v), 6) for v in a]


def available_data_files(base_dir):
    """列出可用历史数据 CSV: 现货 data/, 合约 data/futures/"""
    files = []
    spot_dir = os.path.join(base_dir, "data")
    fut_dir = os.path.join(base_dir, "data", "futures")
    for d, market in ((spot_dir, "现货"), (fut_dir, "合约")):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".csv"):
                files.append({"name": f, "path": os.path.join(d, f), "market": market})
    return files
