"""回测策略基类

归纳自 base.py:95 的 `BaseStrategy`. 所有策略文件 (strategies/eth_*.py)
均继承此类, 实现 run_backtest 完成自定义回测逻辑.

用法:
    from components.quant.strategy_base import BaseStrategy
    class MyStrategy(BaseStrategy):
        name = "我的策略"
        CAPITAL = 150.0
        def run_backtest(self, df):
            ...  # 返回 {'trades':..., 'equity_curve':..., 'stats':...}
"""
import os
import sys

# 兼容: 优先复用根目录 base.py 中已存在的 BaseStrategy, 避免行为分叉.
# 新代码可直接 from components.quant.strategy_base import BaseStrategy.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

try:
    from base import BaseStrategy  # noqa: F401  (根目录已有的基类)
except Exception:
    # 兜底: 若 base.py 不可用, 提供最小可用基类
    class BaseStrategy:
        """策略基类 — 子类需覆写 run_backtest

        返回约定:
            {
              'trades':       list[dict],   # 交易记录
              'equity_curve': list[(ts_ms, balance)],
              'stats':        dict,         # compute_stats 的返回
            }
        """
        name = "BaseStrategy"
        CAPITAL = 10000.0
        SYMBOL = "BTCUSDT"

        def run_backtest(self, df):
            raise NotImplementedError
