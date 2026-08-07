"""技术指标计算工具

归纳自项目中重复定义的指标函数, 统一到此类. 原先 `calc_roc`/`calc_ma`/
`calc_atr`/`calc_rsi` 在以下文件各自重复实现:

    calc_roc:  live_trader.py / live_trader_contract.py / dashboard_server.py /
               paper_trading.py / eth_roc_momentum_v12.py / eth_roc_momentum_contract.py  (6处)
    calc_ma:   上述4个服务 + eth_dca_v10 / eth_ma_reversion_v9 / eth_rsi_leverage 等  (9处)
    calc_atr:  live_trader_contract.py / eth_roc_momentum_contract.py                  (2处)
    calc_rsi:  eth_ma_reversion_v9 / eth_rsi_dca_v11 / eth_rsi_leverage                (3处)

本文件不改原文件, 仅提供统一封装供新代码引用. 既有文件可逐步迁移至此.

用法:
    from components.quant.indicators import Indicators, calc_roc, calc_ma
    roc = Indicators.roc(closes, 8)        # 类方法调用
    ma  = calc_ma(volumes, 20)             # 兼容旧习惯的模块级接口
"""
import numpy as np


class Indicators:
    """技术指标计算器 (全部为静态方法, 无状态)

    所有方法接受 array-like 序列, 返回与输入等长的 np.ndarray;
    不够周期长度的前若干位用 np.nan 填充 (RSI 例外, 用 50.0 中性值填充).
    """

    # ---------------- 趋势 / 动量 ----------------
    @staticmethod
    def roc(close_series, period):
        """Rate of Change 价格变化率 (%)

        ROC(N) = (Price_t - Price_{t-N}) / Price_{t-N} × 100
        前 period 个为 nan.
        """
        closes = np.asarray(close_series, dtype=float)
        n = len(closes)
        roc = np.full(n, np.nan)
        for i in range(period, n):
            if closes[i - period] > 0:
                roc[i] = (closes[i] - closes[i - period]) / closes[i - period] * 100
        return roc

    @staticmethod
    def ma(close_series, period):
        """简单移动平均 SMA

        用 cumsum 实现 O(n). 前 period-1 个为 nan.
        """
        values = np.asarray(close_series, dtype=float)
        ma = np.full(len(values), np.nan)
        if len(values) < period:
            return ma
        cumsum = np.cumsum(values)
        ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
        return ma

    # ---------------- 波动率 ----------------
    @staticmethod
    def atr(highs, lows, closes, period):
        """Average True Range 平均真实波幅

        TR = max(H-L, |H-prevC|, |L-prevC|)
        ATR = TR 的简单移动平均 (SMA)
        第 0 位为 nan (无 prev_close); 用于动态止盈止损: 高波动放宽, 低波动收紧.
        """
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        closes = np.asarray(closes, dtype=float)
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
            valid_tr = tr[1:]  # 从 index=1 开始有有效 TR
            if len(valid_tr) >= period:
                cumsum = np.nancumsum(valid_tr)
                atr_sma = np.full(len(valid_tr), np.nan)
                atr_sma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
                atr[1:] = atr_sma  # 回写, index 0 保持 nan
        return atr

    # ---------------- 超买超卖 ----------------
    @staticmethod
    def rsi(close_series, period=14):
        """Relative Strength Index 相对强弱指标 (Wilder 平滑)

        前 period-1 个填充 50.0 (中性); 数据不足时整体返回 50.0.
        """
        closes = np.asarray(close_series, dtype=float)
        delta = np.diff(closes, prepend=closes[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)

        avg_gain = np.zeros_like(gain, dtype=float)
        avg_loss = np.zeros_like(loss, dtype=float)

        if len(gain) < period:
            return np.full(len(closes), 50.0)

        avg_gain[period - 1] = np.mean(gain[:period])
        avg_loss[period - 1] = np.mean(loss[:period])

        for i in range(period, len(gain)):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

        rs = np.where(avg_loss > 1e-10, avg_gain / avg_loss, 100.0)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[:period - 1] = 50.0
        return rsi


# ============ 模块级接口函数 (兼容旧调用习惯) ============
# 旧代码用 `calc_roc(closes, 8)` 调用, 迁移时只需改 import 路径:
#   from components.quant.indicators import calc_roc
calc_roc = Indicators.roc
calc_ma = Indicators.ma
calc_atr = Indicators.atr
calc_rsi = Indicators.rsi
