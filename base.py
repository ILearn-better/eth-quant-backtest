"""回测策略基类 + 统计/报告工具"""
import pandas as pd
import numpy as np
import os
from datetime import datetime
import matplotlib
matplotlib.use('Agg')  # 无GUI后端,必须最前
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch

# 全局中文字体和样式配置
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'PingFang SC', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['figure.facecolor'] = '#0f0f14'
plt.rcParams['axes.facecolor'] = '#1a1a24'
plt.rcParams['text.color'] = '#e0e0e0'
plt.rcParams['axes.labelcolor'] = '#e0e0e0'
plt.rcParams['xtick.color'] = '#888'
plt.rcParams['ytick.color'] = '#888'
plt.rcParams['axes.edgecolor'] = '#333'
plt.rcParams['grid.color'] = '#222'
plt.rcParams['font.size'] = 10


def compute_stats(trades, initial_capital):
    """从交易记录计算统计指标"""
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

    # Sharpe ratio (简化: 年化, 假设每小时收益率)
    returns = [p / initial_capital for p in pnls]
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(8760)  # 年化(8760小时/年)
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


class BaseStrategy:
    """策略基类"""

    name = "BaseStrategy"
    CAPITAL = 10000.0
    SYMBOL = "BTCUSDT"

    def run_backtest(self, df):
        """子类覆写此方法实现自定义回测逻辑
        返回: dict {
            'trades': list of trade dict,
            'equity_curve': list of (timestamp, balance),
            'stats': stats dict,
        }
        """
        raise NotImplementedError


def _make_figure(dpi=150):
    """创建深色主题 Figure"""
    fig, ax = plt.subplots(figsize=(14, 5), dpi=dpi)
    return fig, ax


def plot_equity_chart(equity_curve, initial_capital, output_path,
                       title="资金曲线 Equity Curve", trades=None):
    """用 matplotlib 绘制资金曲线图 (双色填充 + 本金线 + 买卖点标注)"""
    timestamps = [datetime.fromtimestamp(e[0] / 1000) for e in equity_curve]
    values = [float(e[1]) for e in equity_curve]

    fig, ax = _make_figure()

    # 双色填充: 盈利区绿色, 亏损区红色
    ax.fill_between(timestamps, values, initial_capital,
                    where=[v >= initial_capital for v in values],
                    color='#00d4aa', alpha=0.15, interpolate=True)
    ax.fill_between(timestamps, values, initial_capital,
                    where=[v < initial_capital for v in values],
                    color='#ff6b6b', alpha=0.2, interpolate=True)

    # 资金曲线主线
    ax.plot(timestamps, values, color='#f7931a', linewidth=1.8, zorder=5)

    # 本金参考线
    ax.axhline(y=initial_capital, color='#888', linestyle='--', linewidth=1,
               label=f'本金 {initial_capital:.0f}U', alpha=0.7)

    # ===== 在资金曲线上标注每笔交易的买入/卖出点 =====
    if trades:
        # 构建时间→权益的映射 (用于在交易时间点找到对应的权益值)
        ts_to_value = {}
        for e in equity_curve:
            ts_to_value[int(e[0])] = float(e[1])

        for idx, t in enumerate(trades):
            entry_ts = t.get("entry_time", 0)
            exit_ts = t.get("exit_time", 0)
            direction = t.get("direction", "")
            pnl = t.get("pnl", 0)

            entry_dt = datetime.fromtimestamp(entry_ts / 1000)
            exit_dt = datetime.fromtimestamp(exit_ts / 1000) if exit_ts else None

            entry_val = ts_to_value.get(int(entry_ts), None)
            exit_val = ts_to_value.get(int(exit_ts), None) if exit_ts else None

            # 买入标记: 绿色圆点(多) 或 红色倒三角(空)
            if entry_dt and entry_val is not None:
                marker = '^' if direction == "long" else 'v'
                buy_color = '#00d4aa' if direction == "long" else '#ff6b6b'
                ax.scatter([entry_dt], [entry_val], color=buy_color, s=45, zorder=12,
                           marker=marker, edgecolors='white', linewidths=0.8)
                label_text = f'{"多" if direction=="long" else "空"}开 {t["entry_price"]:.0f}'
                ax.annotate(label_text, (entry_dt, entry_val),
                            textcoords="offset points", xytext=(10, -18 if direction=="long" else 14),
                            color=buy_color, fontsize=7.5, fontweight='bold',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a24', edgecolor=buy_color, alpha=0.8))

            # 卖出标记: 根据盈亏着色
            if exit_dt and exit_val is not None:
                sell_color = '#00d4aa' if pnl >= 0 else '#ff6b6b'
                marker_sell = 'o'
                ax.scatter([exit_dt], [exit_val], color=sell_color, s=40, zorder=12,
                           marker=marker_sell, edgecolors='white', linewidths=0.8)
                reason_tag = t.get("reason", "")
                reason_label = {"TP": "止盈", "SL": "止损", "force_close": "强平"}.get(reason_tag, reason_tag)
                sell_label = f'{reason_label} {pnl:+.1f}U'
                ax.annotate(sell_label, (exit_dt, exit_val),
                            textcoords="offset points", xytext=(10, 10),
                            color=sell_color, fontsize=7.5, fontweight='bold',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a24', edgecolor=sell_color, alpha=0.8))

            # 画连线: 买入→卖出 (虚线竖线显示持仓期间)
            if entry_dt and exit_dt and entry_val is not None and exit_val is not None:
                line_color = '#00d4aa44' if pnl > 0 else '#ff6b6b44'
                line_style = '--' if pnl > 0 else ':'
                ax.plot([entry_dt, exit_dt], [entry_val, exit_val],
                        color=line_color[:7], linestyle=line_style, linewidth=1, zorder=3, alpha=0.5)

    # 标注最高点和最低点
    max_idx = int(np.argmax(values))
    min_idx = int(np.argmin(values))
    if values[max_idx] != values[min_idx]:
        ax.scatter([timestamps[max_idx]], [values[max_idx]], color='#00d4aa',
                   s=50, zorder=10, marker='^')
        ax.annotate(f'{values[max_idx]:.1f}U', (timestamps[max_idx], values[max_idx]),
                    textcoords="offset points", xytext=(8, 8),
                    color='#00d4aa', fontsize=9, fontweight='bold')
        ax.scatter([timestamps[min_idx]], [values[min_idx]], color='#ff6b6b',
                   s=50, zorder=10, marker='v')
        ax.annotate(f'{values[min_idx]:.1f}U', (timestamps[min_idx], values[min_idx]),
                    textcoords="offset points", xytext=(8, -12),
                    color='#ff6b6b', fontsize=9, fontweight='bold')

    # 格式
    ax.set_title(title, fontsize=16, fontweight='bold', color='#fff', pad=15)
    ax.set_ylabel('账户权益 (USDT)', fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.xticks(rotation=45, ha='right')
    ax.legend(loc='upper left', facecolor='#1a1a24', edgecolor='#333',
              labelcolor='#e0e0e0')
    ax.grid(True, alpha=0.3)
    ax.set_xlim(timestamps[0], timestamps[-1])

    # Y轴给一点边距
    y_min, y_max = min(values), max(values)
    margin = (y_max - y_min) * 0.08
    ax.set_ylim(max(0, y_min - margin), y_max + margin)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  📈 资金曲线已保存: {output_path}")
    return output_path


def plot_drawdown_chart(equity_curve, output_path,
                         title="回撤曲线 Drawdown"):
    """用 matplotlib 绘制回撤曲线"""
    timestamps = [datetime.fromtimestamp(e[0] / 1000) for e in equity_curve]
    values = [float(e[1]) for e in equity_curve]

    peak = values[0]
    drawdown_pct = []
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        drawdown_pct.append(dd)

    fig, ax = _make_figure()

    ax.fill_between(timestamps, drawdown_pct, 0, color='#ff6b6b', alpha=0.25)
    ax.plot(timestamps, drawdown_pct, color='#ff6b6b', linewidth=1.2)
    ax.axhline(y=0, color='#555', linewidth=0.5)

    # 标注最大回撤
    max_dd_idx = int(np.argmax(drawdown_pct))
    max_dd = drawdown_pct[max_dd_idx]
    if max_dd > 1:
        ax.scatter([timestamps[max_dd_idx]], [max_dd], color='#ff4444', s=60,
                   zorder=10, marker='v')
        ax.annotate(f'最大 {max_dd:.1f}%', (timestamps[max_dd_idx], max_dd),
                    textcoords="offset points", xytext=(8, -12),
                    color='#ff4444', fontsize=9, fontweight='bold')

    ax.set_title(title, fontsize=16, fontweight='bold', color='#fff', pad=15)
    ax.set_ylabel('回撤 (%)', fontsize=11, color='#ff6b6b')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.xticks(rotation=45, ha='right')
    ax.grid(True, alpha=0.3)
    ax.set_xlim(timestamps[0], timestamps[-1])
    ax.set_ylim(0, max(drawdown_pct) * 1.15 + 1)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  📉 回撤曲线已保存: {output_path}")
    return output_path


def plot_pnl_distribution(trades, output_path, title="盈亏分布 PnL Distribution"):
    """绘制盈亏直方图 + 累计盈亏阶梯图"""
    pnls = [t.get("pnl", 0) for t in trades]
    if not pnls:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 4), dpi=150)

    # 左: 盈亏直方图
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    bins = np.linspace(min(pnls), max(pnls), 30)
    colors = ['#00d4aa' if b >= np.mean(bins) else '#ff6b6b' for b in bins]
    # 用固定颜色区分正负
    pos_mask = bins >= 0
    neg_mask = bins < 0

    n_bins_out = []
    bin_edges_out = []
    for arr in [losses, wins]:
        if arr:
            n, edges = np.histogram(arr, bins=15)
            n_bins_out.append(n)
            bin_edges_out.append(edges)
        else:
            n_bins_out.append(np.array([]))
            bin_edges_out.append(np.array([]))

    if losses:
        ax1.hist(losses, bins=15, color='#ff6b6b', alpha=0.65, edgecolor='#ff4444',
                 label=f'亏损 {len(losses)}笔', zorder=3)
    if wins:
        ax1.hist(wins, bins=15, color='#00d4aa', alpha=0.65, edgecolor='#00aa88',
                 label=f'盈利 {len(wins)}笔', zorder=3)
    ax1.axvline(x=0, color='#888', linestyle='--', linewidth=1, alpha=0.7)
    mean_pnl = np.mean(pnls)
    ax1.axvline(x=mean_pnl, color='#f7931a', linestyle='-', linewidth=1.5,
                label=f'均值 {mean_pnl:+.2f}U')
    ax1.set_title('每笔盈亏分布', fontsize=13, fontweight='bold', color='#fff')
    ax1.set_xlabel('盈亏 (USDT)')
    ax1.set_ylabel('笔数')
    ax1.legend(facecolor='#1a1a24', edgecolor='#333', labelcolor='#e0e0e0')
    ax1.grid(alpha=0.3)

    # 右: 累计盈亏阶梯图
    cum_pnl = np.cumsum(pnls)
    trade_nums = list(range(1, len(pnls) + 1))

    # 阶梯填色: 正为绿, 负为红
    ax2.fill_between(trade_nums, cum_pnl, 0,
                     where=[c >= 0 for c in cum_pnl],
                     color='#00d4aa', alpha=0.2, step='pre')
    ax2.fill_between(trade_nums, cum_pnl, 0,
                     where=[c < 0 for c in cum_pnl],
                     color='#ff6b6b', alpha=0.25, step='pre')
    ax2.step(trade_nums, cum_pnl, where='pre', color='#f7931a', linewidth=1.5)
    ax2.axhline(y=0, color='#888', linestyle='--', linewidth=1, alpha=0.7)
    ax2.set_title('累计盈亏 (按交易顺序)', fontsize=13, fontweight='bold', color='#fff')
    ax2.set_xlabel('交易序号')
    ax2.set_ylabel('累计 PnL (USDT)')
    ax2.grid(alpha=0.3)

    # 标注终点
    ax2.scatter([len(pnls)], [cum_pnl[-1]], color='#f7931a', s=60, zorder=10)
    ax2.annotate(f'{cum_pnl[-1]:+.1f}U', (len(pnls), cum_pnl[-1]),
                 textcoords="offset points", xytext=(8, 0),
                 color='#f7931a', fontsize=10, fontweight='bold')

    fig.suptitle(title, fontsize=16, fontweight='bold', color='#fff', y=1.02)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  📊 盈亏分布已保存: {output_path}")
    return output_path


def plot_trade_klines(trades, kline_data, output_path,
                      title="交易K线图 Trade Candlesticks",
                      window_bars=20):
    """绘制每个买入点前后的K线子图 (candlestick + 买卖标注)

    Args:
        trades: 交易记录列表
        kline_data: 原始K线DataFrame, 需含 timestamp/open/high/low/close 列
        output_path: 输出图片路径
        window_bars: 每个子图显示的K线根数 (买入前后各一半)
    """
    if not trades or kline_data is None:
        return None

    n_trades = len(trades)
    max_show = min(n_trades, 12)
    cols = 3
    rows = (max_show + cols - 1) // cols

    fig, axes = plt.subplots(rows, cols, figsize=(18, 5 * rows), dpi=150)
    if max_show == 1:
        axes = np.array([axes])
    elif rows == 1:
        axes = axes.reshape(1, -1)
    axes = axes.flatten()

    ts_list = np.array(kline_data["timestamp"].values.astype(np.int64))
    opens = kline_data["open"].values.astype(float)
    highs = kline_data["high"].values.astype(float)
    lows = kline_data["low"].values.astype(float)
    closes = kline_data["close"].values.astype(float)

    ts_to_idx = {int(ts): i for i, ts in enumerate(ts_list)}
    half_win = window_bars // 2

    for idx in range(max_show):
        ax = axes[idx]
        t = trades[idx]

        entry_ts = int(t.get("entry_time", 0))
        exit_ts = int(t.get("exit_time", 0)) if t.get("exit_time") else None
        direction = t.get("direction", "")
        pnl = t.get("pnl", 0)

        entry_idx = ts_to_idx.get(entry_ts, None)
        if entry_idx is None:
            ax.text(0.5, 0.5, "无数据", transform=ax.transAxes,
                    ha='center', va='center', color='#888', fontsize=10)
            continue

        start_idx = max(0, entry_idx - half_win)
        end_idx = min(len(ts_list), entry_idx + half_win + 1)
        sub_len = end_idx - start_idx

        sub_opens = opens[start_idx:end_idx]
        sub_highs = highs[start_idx:end_idx]
        sub_lows = lows[start_idx:end_idx]
        sub_closes = closes[start_idx:end_idx]
        sub_dates = [datetime.fromtimestamp(ts_list[i] / 1000) for i in range(start_idx, end_idx)]

        for j in range(sub_len):
            o, h, l, c = sub_opens[j], sub_highs[j], sub_lows[j], sub_closes[j]
            color = '#00d4aa' if c >= o else '#ff6b6b'
            body_bottom = min(o, c)
            body_height = abs(c - o) if abs(c - o) > 0 else 0.5
            ax.bar(j, body_height, bottom=body_bottom, color=color, width=0.7,
                   edgecolor=color, linewidth=0.5, zorder=3)
            ax.vlines(j, l, h, color=color, linewidth=0.8, zorder=2)

        entry_rel_idx = entry_idx - start_idx

        # 标注买入点
        entry_price = t["entry_price"]
        buy_color = '#00d4aa' if direction == 'long' else '#ff6b6b'
        marker_buy = '^' if direction == 'long' else 'v'
        ax.scatter([entry_rel_idx], [entry_price], color=buy_color, s=80, zorder=15,
                   marker=marker_buy, edgecolors='white', linewidths=1.2)
        ax.axhline(y=entry_price, color=buy_color, linestyle='--', linewidth=0.8,
                   alpha=0.4, zorder=1)

        # 标注卖出点 (如果在窗口内)
        if exit_ts and exit_ts in ts_to_idx:
            exit_idx_local = ts_to_idx[exit_ts] - start_idx
            if 0 <= exit_idx_local < sub_len:
                exit_price = t["exit_price"]
                sell_color = '#00d4aa' if pnl > 0 else '#ff6b6b'
                ax.scatter([exit_idx_local], [exit_price], color=sell_color, s=70, zorder=15,
                           marker='o', edgecolors='white', linewidths=1.2)
                ax.axhline(y=exit_price, color=sell_color, linestyle=':', linewidth=0.8,
                           alpha=0.4, zorder=1)
                ax.plot([entry_rel_idx, exit_idx_local],
                        [entry_price, exit_price],
                        color='#f7931a' if pnl > 0 else '#888',
                        linestyle='-' if pnl > 0 else '--',
                        linewidth=1.5, zorder=4, alpha=0.7)

        # 子图标题
        reason_tag = t.get("reason", "")
        reason_label = {"TP": "止盈", "SL": "止损", "force_close": "强平"}.get(reason_tag, reason_tag)
        dir_label = {"long": "做多", "short": "做空"}.get(direction, direction)
        title_text = f"#{idx+1} {dir_label} 开{t['entry_price']:.0f}"
        if exit_ts:
            title_text += f" → {reason_label}{pnl:+.1f}U"
        ax.set_title(title_text, fontsize=11, fontweight='bold', color='#fff', pad=8)
        ax.set_facecolor('#141420')
        ax.tick_params(colors='#666', labelsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right')
        for spine in ax.spines.values():
            spine.set_color('#333')
        ax.grid(True, alpha=0.15, color='#333')

    for idx in range(max_show, len(axes)):
        axes[idx].set_visible(False)

    fig.suptitle(title, fontsize=18, fontweight='bold', color='#fff', y=1.01)
    fig.tight_layout(rect=[0, 0, 1, 0.98])
    fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='#0f0f14')
    plt.close(fig)
    print(f"  🕯️ K线图已保存: {output_path}")
    return output_path

def generate_html_report(stats, trades, equity_curve, symbol="ETHUSDT",
                          interval="1h", output_dir="reports", kline_df=None):
    """生成 HTML 回测报告 (图表用 matplotlib 预渲染为 PNG)"""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "charts"), exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{symbol}_{interval}_{ts}.html"
    filepath = os.path.join(output_dir, filename)
    chart_dir = "charts"

    # ===== 用 matplotlib 生成图表 PNG =====
    base_name = f"{symbol}_{ts}"
    eq_img = f"{chart_dir}/{base_name}_equity.png"
    dd_img = f"{chart_dir}/{base_name}_drawdown.png"
    pnl_img = f"{chart_dir}/{base_name}_pnl.png"
    kline_img = f"{chart_dir}/{base_name}_klines.png"

    initial_capital = stats.get('initial_capital', 150)

    plot_equity_chart(equity_curve, initial_capital,
                       os.path.join(output_dir, eq_img),
                       title=f"{symbol} RSI策略 - 资金曲线", trades=trades)
    plot_drawdown_chart(equity_curve,
                         os.path.join(output_dir, dd_img),
                         title=f"{symbol} RSI策略 - 回撤曲线")
    plot_pnl_distribution(trades,
                           os.path.join(output_dir, pnl_img),
                           title=f"{symbol} RSI策略 - 盈亏分析")
    # K线图 (每笔交易的买入点K线)
    kline_path = None
    if kline_df is not None and len(kline_df) > 0:
        kline_path = plot_trade_klines(trades, kline_df,
                                       os.path.join(output_dir, kline_img),
                                       title=f"{symbol} RSI策略 - 交易K线")

    # ===== 构建 HTML =====
    import json

    # 交易方向颜色
    trade_rows = ""
    for i, t in enumerate(trades[-50:]):  # 最近50笔
        direction = t.get("direction", "")
        color = "#e74c3c" if (direction == "long" and t.get("pnl", 0) >= 0) or \
                           (direction == "short" and t.get("pnl", 0) < 0) else "#27ae60"
        pnl_color = "#e74c3c" if t.get("pnl", 0) < 0 else "#27ae60"
        entry_t = datetime.fromtimestamp(t.get("entry_time", 0)/1000).strftime("%Y-%m-%d")
        exit_t = datetime.fromtimestamp(t.get("exit_time", 0)/1000).strftime("%Y-%m-%d") if t.get("exit_time") else "-"
        reason_tag = t.get("reason", "-")
        reason_badge = f'<span style="background:{"#00d4aa" if reason_tag=="TP" else "#ff6b6b"};color:#000;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:bold">{reason_tag}</span>'
        trade_rows += f"""
        <tr>
            <td>{i+1}</td><td>{entry_t}</td><td>{exit_t}</td><td style="color:{color};font-weight:bold">{direction.upper()}</td>
            <td>{t.get('level', '-')}</td><td>{t.get('entry_price', '-:.2f')}</td>
            <td>{t.get('exit_price', '-:.2f')}</td>
            <td style="color:{pnl_color};font-weight:bold">{t.get('pnl', 0):+.2f}</td>
            <td>{reason_badge}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETH RSI 杠杆策略回测报告</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; background:#0f0f14; color:#e0e0e0; padding:20px; }}
.header {{ text-align:center; padding:30px 20px; border-bottom:1px solid #2a2a3a; margin-bottom:30px; }}
.header h1 {{ font-size:28px; color:#f7931a; }} .header .sub {{ color:#888; margin-top:8px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:30px; }}
.card {{ background:#1a1a24; border-radius:12px; padding:20px; border:1px solid #2a2a3a; }}
.card .label {{ color:#888; font-size:12px; text-transform:uppercase; letter-spacing:1px; }}
.card .value {{ font-size:28px; font-weight:bold; margin-top:8px; }}
.card.positive .value {{ color:#00d4aa; }} .card.negative .value {{ color:#ff6b6b; }} .card.neutral .value {{ color:#f7931a; }}
.chart-section {{ background:#1a1a24; border-radius:12px; padding:24px; border:1px solid #2a2a3a; margin-bottom:20px; }}
.chart-section h2 {{ font-size:18px; color:#fff; margin-bottom:16px; border-left:3px solid #f7931a; padding-left:12px; }}
.chart-img {{ width:100%; border-radius:8px; display:block; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:#252532; color:#aaa; padding:10px 12px; text-align:left; position:sticky;top:0; }}
td {{ padding:8px 12px; border-bottom:1px solid #222; }}
tr:hover td {{ background:#252532; }}
</style>
</head>
<body>

<div class="header">
    <h1>📊 ETH RSI 分仓杠杆策略 回测报告</h1>
    <div class="sub">{symbol} | {interval} | 近2年数据 | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
</div>

<div class="grid">
    <div class="card {'positive' if stats['return_pct']>=0 else 'negative'}">
        <div class="label">收益率</div><div class="value">{stats['return_pct']:+.2f}%</div>
    </div>
    <div class="card neutral"><div class="label">总交易次数</div><div class="value">{stats['total_trades']}</div></div>
    <div class="card {'positive' if stats['win_rate']>=50 else 'negative'}">
        <div class="label">胜率</div><div class="value">{stats['win_rate']:.1f}%</div>
    </div>
    <div class="card {'positive' if stats['total_pnl']>=0 else 'negative'}">
        <div class="label">总盈亏 (USDT)</div><div class="value">{stats['total_pnl']:+.2f}</div>
    </div>
    <div class="card negative"><div class="label">最大回撤</div><div class="value">{stats['max_drawdown']:.2f}%</div></div>
    <div class="card {'positive' if stats['profit_factor']>=1 else 'negative'}">
        <div class="label">盈亏比</div><div class="value">{stats['profit_factor']:.2f}</div></div>
    <div class="card neutral"><div class="label">本金 → 最终</div>
        <div class="value" style="font-size:20px">{stats['initial_capital']:.0f} → {stats['final_capital']:.2f}</div></div>
    <div class="card neutral"><div class="label">Sharpe Ratio</div><div class="value">{stats['sharpe_ratio']:.2f}</div></div>
    <div class="card positive"><div class="label">平均盈利</div><div class="value">{stats['avg_win']:+.2f}</div></div>
    <div class="card negative"><div class="label">平均亏损</div><div class="value">{stats['avg_loss']:+.2f}</div></div>
    <div class="card positive"><div class="label">最佳单笔</div><div class="value">{stats['best_trade']:+.2f}</div></div>
    <div class="card negative"><div class="label">最差单笔</div><div class="value">{stats['worst_trade']:+.2f}</div></div>
</div>

<!-- 资金曲线 -->
<div class="chart-section">
    <h2>📈 资金曲线 Equity Curve</h2>
    <img class="chart-img" src="{eq_img}" alt="资金曲线">
</div>

<!-- 回撤曲线 -->
<div class="chart-section">
    <h2>📉 回撤曲线 Drawdown</h2>
    <img class="chart-img" src="{dd_img}" alt="回撤曲线">
</div>

<!-- 盈亏分布 -->
<div class="chart-section">
    <h2>📊 盈亏分析 PnL Analysis</h2>
    <img class="chart-img" src="{pnl_img}" alt="盈亏分析">
</div>"""

    # K线图 section (如果有)
    kline_section = ""
    if kline_path:
        kline_section = f"""
<!-- 交易K线图 -->
<div class="chart-section">
    <h2>🕯️ 交易K线图 Trade Candlesticks (每笔交易的买入点前后走势)</h2>
    <img class="chart-img" src="{kline_img}" alt="交易K线图">
</div>"""

    html += f"""
{kline_section}
<div class="chart-section">
    <h2>📋 最近交易记录 (最近{min(50,len(trades))}笔)</h2>
    <div style="overflow-x:auto;max-height:400px;overflow-y:auto;">
    <table>
    <tr><th>#</th><th>买入时间</th><th>卖出时间</th><th>方向</th><th>仓位</th><th>开仓价</th><th>平仓价</th><th>盈亏U</th><th>原因</th></tr>
    {trade_rows}
    </table></div>
</div>

</body></html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n📄 HTML 报告已生成: {filepath}")
    return filepath
