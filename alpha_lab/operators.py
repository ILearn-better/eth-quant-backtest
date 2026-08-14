"""Alpha 因子算子库 (L1 基础 + L2 时序)

世坤(WorldQuant)风格算子, 全部基于 numpy/pandas 向量化实现.
所有时序算子返回与输入等长的数组, 窗口不足处以 NaN 填充.

字段可用: open / high / low / close / volume / returns(对数收益率)

用法:
    from alpha_lab.operators import apply_operator
    out = apply_operator("ts_delta", close, [5])
"""
import numpy as np
import pandas as pd


# ---- 基础工具 ----
def _as_series(x):
    if isinstance(x, pd.Series):
        return x.astype(float)
    return pd.Series(np.asarray(x, dtype=float))


def _rolling(x, n, fn):
    """滚动窗口聚合, n<=1 时退化"""
    s = _as_series(x)
    if n <= 1:
        return s
    return s.rolling(window=n, min_periods=n).apply(fn, raw=True)


# ---- L1 基础算子 ----
def op_rank(x, n=None):
    """时序百分位排名: 当前值在整段序列(或滚动n窗口)中的百分位 [0,1]"""
    a = np.asarray(x, dtype=float)
    if n and n > 1:
        # 滚动窗口内当前值的百分位
        def _win_rank(v):
            last = v[-1]
            r = int((v < last).sum()) + 0.5 * int((v == last).sum())
            return r / (len(v) - 1) if len(v) > 1 else 0.5
        return _rolling(a, n, _win_rank)
    # 整段百分位 (NaN 保持, 平局取中位)
    out = np.full_like(a, np.nan)
    mask = ~np.isnan(a)
    valid = a[mask]
    m = len(valid)
    if m == 0:
        return out
    order = np.argsort(valid, kind="mergesort")
    ranks = np.empty(m)
    ranks[order] = np.arange(m) / (m - 1) if m > 1 else 0.5
    out[mask] = ranks
    return out


def op_delay(x, n=1):
    """前移 n 根: delay(x, n) = x 在 n 根之前的值"""
    s = _as_series(x)
    return s.shift(int(n))


def op_delta(x, n=1):
    """差值: delta(x, n) = x - delay(x, n)"""
    s = _as_series(x)
    return s - s.shift(int(n))


def op_sign(x):
    """符号: -1/0/1"""
    return np.sign(np.asarray(x, dtype=float))


def op_abs(x):
    return np.abs(np.asarray(x, dtype=float))


def op_log(x):
    """自然对数, 非正值→NaN"""
    a = np.asarray(x, dtype=float)
    return pd.Series(np.where(a > 0, np.log(a), np.nan))


def op_power(x, p=2):
    return np.power(np.asarray(x, dtype=float), float(p))


def op_sqrt(x):
    a = np.asarray(x, dtype=float)
    return pd.Series(np.where(a >= 0, np.sqrt(a), np.nan))


def op_scale(x, n=1):
    """世坤 scale: x * n / sum(|x|)"""
    a = np.asarray(x, dtype=float)
    s = np.nansum(np.abs(a))
    if s == 0 or not np.isfinite(s):
        return pd.Series(np.full_like(a, np.nan))
    return pd.Series(a * n / s)


# ---- L2 时序算子 ----
def op_ts_mean(x, n=5):
    return _rolling(x, int(n), lambda a: np.mean(a))


def op_ts_std(x, n=5):
    return _rolling(x, int(n), lambda a: np.std(a, ddof=0))


def op_ts_sum(x, n=5):
    s = _as_series(x)
    return s.rolling(window=int(n), min_periods=1).sum() if int(n) <= 1 else s.rolling(window=int(n), min_periods=int(n)).sum()


def op_ts_min(x, n=5):
    return _rolling(x, int(n), lambda a: np.min(a))


def op_ts_max(x, n=5):
    return _rolling(x, int(n), lambda a: np.max(a))


def op_ts_rank(x, n=5):
    """滚动窗口内当前值的百分位 [0,1]"""
    return op_rank(x, n)


def op_ts_corr(x, y, n=10):
    """滚动相关系数"""
    a, b = _as_series(x), _as_series(y)
    return a.rolling(window=int(n), min_periods=int(n)).corr(b)


def op_zscore(x, n=20):
    """滚动 z-score 标准化: (x - rolling_mean) / rolling_std"""
    s = _as_series(x)
    mean = s.rolling(window=int(n), min_periods=int(n)).mean()
    std = s.rolling(window=int(n), min_periods=int(n)).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def op_roc(x, n=1):
    """ROC 涨跌幅 (%): roc(x, n) = (x / delay(x, n) - 1) * 100"""
    s = _as_series(x)
    prev = s.shift(int(n))
    return (s / prev - 1) * 100


# 兼容别名
def op_mean(x, n=5): return op_ts_mean(x, n)
def op_std(x, n=5): return op_ts_std(x, n)
def op_min(x, n=5): return op_ts_min(x, n)
def op_max(x, n=5): return op_ts_max(x, n)
def op_ts_delta(x, n=1): return op_delta(x, n)


# ---- 算子注册表 ----
OPERATORS = {
    # L1 基础
    "rank":     {"fn": op_rank,     "args": ["x", "n?"], "desc": "时序百分位排名[0,1]"},
    "delay":    {"fn": op_delay,    "args": ["x", "n=1"], "desc": "前移n根的值"},
    "delta":    {"fn": op_delta,    "args": ["x", "n=1"], "desc": "x-delay(x,n)"},
    "sign":     {"fn": op_sign,     "args": ["x"], "desc": "符号-1/0/1"},
    "abs":      {"fn": op_abs,      "args": ["x"], "desc": "绝对值"},
    "log":      {"fn": op_log,      "args": ["x"], "desc": "自然对数"},
    "power":    {"fn": op_power,    "args": ["x", "p=2"], "desc": "x^p"},
    "sqrt":     {"fn": op_sqrt,     "args": ["x"], "desc": "平方根"},
    "scale":    {"fn": op_scale,    "args": ["x", "n=1"], "desc": "x*n/sum(|x|)"},
    "mean":     {"fn": op_mean,     "args": ["x", "n=5"], "desc": "滚动均值"},
    "std":      {"fn": op_std,      "args": ["x", "n=5"], "desc": "滚动标准差"},
    "min":      {"fn": op_min,      "args": ["x", "n=5"], "desc": "滚动最小值"},
    "max":      {"fn": op_max,      "args": ["x", "n=5"], "desc": "滚动最大值"},
    # L2 时序
    "ts_delta": {"fn": op_ts_delta, "args": ["x", "n=1"], "desc": "x-delay(x,n)"},
    "ts_mean":  {"fn": op_ts_mean,  "args": ["x", "n=5"], "desc": "滚动均值"},
    "ts_std":   {"fn": op_ts_std,   "args": ["x", "n=5"], "desc": "滚动标准差"},
    "ts_sum":   {"fn": op_ts_sum,   "args": ["x", "n=5"], "desc": "滚动求和"},
    "ts_min":   {"fn": op_ts_min,   "args": ["x", "n=5"], "desc": "滚动最小值"},
    "ts_max":   {"fn": op_ts_max,   "args": ["x", "n=5"], "desc": "滚动最大值"},
    "ts_rank":  {"fn": op_ts_rank,  "args": ["x", "n=5"], "desc": "窗口内百分位[0,1]"},
    "ts_corr":  {"fn": op_ts_corr,  "args": ["x", "y", "n=10"], "desc": "滚动相关系数"},
    "zscore":   {"fn": op_zscore,   "args": ["x", "n=20"], "desc": "滚动z-score标准化"},
    "roc":      {"fn": op_roc,      "args": ["x", "n=1"], "desc": "ROC涨跌幅%(x/delay(x,n)-1)*100"},
}

# 字段白名单
FIELDS = ["open", "high", "low", "close", "volume", "returns"]


def validate_field(name):
    """校验字段名是否可用"""
    if name not in FIELDS:
        raise ValueError(f"未知字段 '{name}', 可用字段: {', '.join(FIELDS)}")


def get_operator(name):
    if name not in OPERATORS:
        raise ValueError(f"未知算子 '{name}', 可用: {', '.join(sorted(OPERATORS))}")
    return OPERATORS[name]
