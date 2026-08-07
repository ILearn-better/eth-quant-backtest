"""回测统计指标计算

归纳自 base.py:26 的 `compute_stats`. 该函数被所有 main_*.py 回测入口调用,
是项目唯一的统计实现 (无重复), 但作为量化核心工具归档至此.

用法:
    from components.quant.statistics import compute_stats
    stats = compute_stats(trades, initial_capital=150.0)
"""
import numpy as np


def compute_stats(trades, initial_capital):
    """从交易记录计算统计指标

    Args:
        trades: 交易记录列表, 每条需含 'pnl' 字段 (单笔盈亏, USDT)
        initial_capital: 初始本金 (USDT)

    Returns:
        dict, 含以下字段:
          total_trades    交易次数
          win_rate        胜率 (%)
          total_pnl       总盈亏 (USDT)
          return_pct      收益率 (%)
          max_drawdown    最大回撤 (%)
          profit_factor   盈亏比 (毛利/毛亏)
          avg_win         平均盈利 (USDT)
          avg_loss        平均亏损 (USDT)
          best_trade      最佳单笔 (USDT)
          worst_trade     最差单笔 (USDT)
          initial_capital 初始本金
          final_capital   最终资金
          sharpe_ratio    夏普比率 (年化, 假设每小时收益率, √8760)
    """
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0, "total_pnl": 0,
            "return_pct": 0, "max_drawdown": 0, "profit_factor": 0,
            "avg_win": 0, "avg_loss": 0, "best_trade": 0, "worst_trade": 0,
            "initial_capital": initial_capital, "final_capital": initial_capital,
            "sharpe_ratio": 0,
        }

    pnls = [t.get("pnl", 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_pnl = sum(pnls)
    n_wins = len(wins)
    n_losses = len(losses)
    n_total = len(pnls)
    win_rate = (n_wins / n_total * 100) if n_total else 0

    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses)) if losses else 1e-10
    profit_factor = gross_profit / gross_loss

    final_capital = initial_capital + total_pnl
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    # 最大回撤 (基于资金曲线)
    equity = [initial_capital]
    for p in pnls:
        equity.append(equity[-1] + p)

    peak = equity[0]
    max_dd = 0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (简化年化: 假设每小时一笔收益率, 8760 小时/年)
    returns = [p / initial_capital for p in pnls]
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(8760)
    else:
        sharpe = 0.0

    return {
        "total_trades": n_total,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round(return_pct, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best_trade": round(max(pnls), 2) if pnls else 0,
        "worst_trade": round(min(pnls), 2) if pnls else 0,
        "initial_capital": round(initial_capital, 2),
        "final_capital": round(final_capital, 2),
        "sharpe_ratio": round(sharpe, 2),
    }
