"""matplotlib 图表绘制工具

归纳自 base.py 的 4 个绘图函数 + 深色主题配置:
    plot_equity_chart      资金曲线 (双色填充 + 买卖点标注)
    plot_drawdown_chart    回撤曲线
    plot_pnl_distribution  盈亏分布 (直方图 + 累计阶梯图)
    plot_trade_klines      交易K线子图 (candlestick + 买卖标注)

封装为 ChartPlotter 类, 逻辑与原 base.py 完全一致, 仅改写为方法形式.
本文件不改 base.py, 供新代码引用; 既有 main_*.py 仍用 base.py 的原函数.

用法:
    from components.plotting.charts import ChartPlotter
    ChartPlotter.plot_equity_chart(equity_curve, 150, "out.png", trades=trades)
"""
import numpy as np
import matplotlib
matplotlib.use('Agg')  # 无GUI后端, 必须在最前
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

# ===== 全局深色主题中文字体配置 (与 base.py 保持一致) =====
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


class ChartPlotter:
        """回测图表绘制器 (静态方法, 无状态)

        4 类图表均保存为 PNG, 返回输出路径. 颜色风格:
            盈利/多 #00d4aa (绿)   亏损/空 #ff6b6b (红)   主线 #f7931a (橙)
        """

        @staticmethod
        def _make_figure(dpi=150):
            """创建深色主题 Figure"""
            fig, ax = plt.subplots(figsize=(14, 5), dpi=dpi)
            return fig, ax

        @staticmethod
        def plot_equity_chart(equity_curve, initial_capital, output_path,
                               title="资金曲线 Equity Curve", trades=None):
                """资金曲线图 (双色填充 + 本金线 + 买卖点标注)

                Args:
                    equity_curve: [(ts_ms, balance), ...]
                    initial_capital: 本金, 用于参考线与盈亏填色分界
                    output_path: PNG 输出路径
                    trades: 可选, 交易记录列表, 提供时在曲线上标注开/平仓点
                """
                timestamps = [datetime.fromtimestamp(e[0] / 1000) for e in equity_curve]
                values = [float(e[1]) for e in equity_curve]

                fig, ax = ChartPlotter._make_figure()

                # 双色填充: 盈利区绿色, 亏损区红色
                ax.fill_between(timestamps, values, initial_capital,
                                where=[v >= initial_capital for v in values],
                                color='#00d4aa', alpha=0.15, interpolate=True)
                ax.fill_between(timestamps, values, initial_capital,
                                where=[v < initial_capital for v in values],
                                color='#ff6b6b', alpha=0.2, interpolate=True)

                ax.plot(timestamps, values, color='#f7931a', linewidth=1.8, zorder=5)
                ax.axhline(y=initial_capital, color='#888', linestyle='--', linewidth=1,
                           label=f'本金 {initial_capital:.0f}U', alpha=0.7)

                # ===== 在资金曲线上标注每笔交易的买入/卖出点 =====
                if trades:
                        ts_to_value = {int(e[0]): float(e[1]) for e in equity_curve}
                        for idx, t in enumerate(trades):
                                entry_ts = t.get("entry_time", 0)
                                exit_ts = t.get("exit_time", 0)
                                direction = t.get("direction", "")
                                pnl = t.get("pnl", 0)

                                entry_dt = datetime.fromtimestamp(entry_ts / 1000)
                                exit_dt = datetime.fromtimestamp(exit_ts / 1000) if exit_ts else None
                                entry_val = ts_to_value.get(int(entry_ts), None)
                                exit_val = ts_to_value.get(int(exit_ts), None) if exit_ts else None

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

                                if exit_dt and exit_val is not None:
                                        sell_color = '#00d4aa' if pnl >= 0 else '#ff6b6b'
                                        ax.scatter([exit_dt], [exit_val], color=sell_color, s=40, zorder=12,
                                                   marker='o', edgecolors='white', linewidths=0.8)
                                        reason_tag = t.get("reason", "")
                                        reason_label = {"TP": "止盈", "SL": "止损", "force_close": "强平"}.get(reason_tag, reason_tag)
                                        sell_label = f'{reason_label} {pnl:+.1f}U'
                                        ax.annotate(sell_label, (exit_dt, exit_val),
                                                    textcoords="offset points", xytext=(10, 10),
                                                    color=sell_color, fontsize=7.5, fontweight='bold',
                                                    bbox=dict(boxstyle='round,pad=0.2', facecolor='#1a1a24', edgecolor=sell_color, alpha=0.8))

                                if entry_dt and exit_dt and entry_val is not None and exit_val is not None:
                                        line_color = '#00d4aa44' if pnl > 0 else '#ff6b6b44'
                                        line_style = '--' if pnl > 0 else ':'
                                        ax.plot([entry_dt, exit_dt], [entry_val, exit_val],
                                                color=line_color[:7], linestyle=line_style, linewidth=1, zorder=3, alpha=0.5)

                # 标注最高点和最低点
                max_idx = int(np.argmax(values))
                min_idx = int(np.argmin(values))
                if values[max_idx] != values[min_idx]:
                        ax.scatter([timestamps[max_idx]], [values[max_idx]], color='#00d4aa', s=50, zorder=10, marker='^')
                        ax.annotate(f'{values[max_idx]:.1f}U', (timestamps[max_idx], values[max_idx]),
                                    textcoords="offset points", xytext=(8, 8), color='#00d4aa', fontsize=9, fontweight='bold')
                        ax.scatter([timestamps[min_idx]], [values[min_idx]], color='#ff6b6b', s=50, zorder=10, marker='v')
                        ax.annotate(f'{values[min_idx]:.1f}U', (timestamps[min_idx], values[min_idx]),
                                    textcoords="offset points", xytext=(8, -12), color='#ff6b6b', fontsize=9, fontweight='bold')

                ax.set_title(title, fontsize=16, fontweight='bold', color='#fff', pad=15)
                ax.set_ylabel('账户权益 (USDT)', fontsize=11)
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
                plt.xticks(rotation=45, ha='right')
                ax.legend(loc='upper left', facecolor='#1a1a24', edgecolor='#333', labelcolor='#e0e0e0')
                ax.grid(True, alpha=0.3)
                ax.set_xlim(timestamps[0], timestamps[-1])
                y_min, y_max = min(values), max(values)
                margin = (y_max - y_min) * 0.08
                ax.set_ylim(max(0, y_min - margin), y_max + margin)

                fig.tight_layout()
                fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
                plt.close(fig)
                print(f"  📈 资金曲线已保存: {output_path}")
                return output_path

        @staticmethod
        def plot_drawdown_chart(equity_curve, output_path, title="回撤曲线 Drawdown"):
                """回撤曲线图 (红色填充 + 最大回撤标注)"""
                timestamps = [datetime.fromtimestamp(e[0] / 1000) for e in equity_curve]
                values = [float(e[1]) for e in equity_curve]

                peak = values[0]
                drawdown_pct = []
                for v in values:
                        if v > peak:
                                peak = v
                        dd = (peak - v) / peak * 100 if peak > 0 else 0
                        drawdown_pct.append(dd)

                fig, ax = ChartPlotter._make_figure()
                ax.fill_between(timestamps, drawdown_pct, 0, color='#ff6b6b', alpha=0.25)
                ax.plot(timestamps, drawdown_pct, color='#ff6b6b', linewidth=1.2)
                ax.axhline(y=0, color='#555', linewidth=0.5)

                max_dd_idx = int(np.argmax(drawdown_pct))
                max_dd = drawdown_pct[max_dd_idx]
                if max_dd > 1:
                        ax.scatter([timestamps[max_dd_idx]], [max_dd], color='#ff4444', s=60, zorder=10, marker='v')
                        ax.annotate(f'最大 {max_dd:.1f}%', (timestamps[max_dd_idx], max_dd),
                                    textcoords="offset points", xytext=(8, -12), color='#ff4444', fontsize=9, fontweight='bold')

                ax.set_title(title, fontsize=16, fontweight='bold', color='#fff', pad=15)
                ax.set_ylabel('回撤 (%)', fontsize=11, color='#ff6b6b')
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
                plt.xticks(rotation=45, ha='right')
                ax.grid(True, alpha=0.3)
                ax.set_xlim(timestamps[0], timestamps[-1])
                ax.set_ylim(0, max(drawdown_pct) * 1.15 + 1)

                fig.tight_layout()
                fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
                plt.close(fig)
                print(f"  📉 回撤曲线已保存: {output_path}")
                return output_path

        @staticmethod
        def plot_pnl_distribution(trades, output_path, title="盈亏分布 PnL Distribution"):
                """盈亏分布图 (左: 直方图; 右: 累计盈亏阶梯图)"""
                pnls = [t.get("pnl", 0) for t in trades]
                if not pnls:
                        return None

                fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 4), dpi=150)
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p <= 0]

                if losses:
                        ax1.hist(losses, bins=15, color='#ff6b6b', alpha=0.65, edgecolor='#ff4444', label=f'亏损 {len(losses)}笔', zorder=3)
                if wins:
                        ax1.hist(wins, bins=15, color='#00d4aa', alpha=0.65, edgecolor='#00aa88', label=f'盈利 {len(wins)}笔', zorder=3)
                ax1.axvline(x=0, color='#888', linestyle='--', linewidth=1, alpha=0.7)
                mean_pnl = np.mean(pnls)
                ax1.axvline(x=mean_pnl, color='#f7931a', linestyle='-', linewidth=1.5, label=f'均值 {mean_pnl:+.2f}U')
                ax1.set_title('每笔盈亏分布', fontsize=13, fontweight='bold', color='#fff')
                ax1.set_xlabel('盈亏 (USDT)'); ax1.set_ylabel('笔数')
                ax1.legend(facecolor='#1a1a24', edgecolor='#333', labelcolor='#e0e0e0')
                ax1.grid(alpha=0.3)

                cum_pnl = np.cumsum(pnls)
                trade_nums = list(range(1, len(pnls) + 1))
                ax2.fill_between(trade_nums, cum_pnl, 0, where=[c >= 0 for c in cum_pnl], color='#00d4aa', alpha=0.2, step='pre')
                ax2.fill_between(trade_nums, cum_pnl, 0, where=[c < 0 for c in cum_pnl], color='#ff6b6b', alpha=0.25, step='pre')
                ax2.step(trade_nums, cum_pnl, where='pre', color='#f7931a', linewidth=1.5)
                ax2.axhline(y=0, color='#888', linestyle='--', linewidth=1, alpha=0.7)
                ax2.set_title('累计盈亏 (按交易顺序)', fontsize=13, fontweight='bold', color='#fff')
                ax2.set_xlabel('交易序号'); ax2.set_ylabel('累计 PnL (USDT)')
                ax2.grid(alpha=0.3)
                ax2.scatter([len(pnls)], [cum_pnl[-1]], color='#f7931a', s=60, zorder=10)
                ax2.annotate(f'{cum_pnl[-1]:+.1f}U', (len(pnls), cum_pnl[-1]),
                             textcoords="offset points", xytext=(8, 0), color='#f7931a', fontsize=10, fontweight='bold')

                fig.suptitle(title, fontsize=16, fontweight='bold', color='#fff', y=1.02)
                fig.tight_layout()
                fig.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
                plt.close(fig)
                print(f"  📊 盈亏分布已保存: {output_path}")
                return output_path

        @staticmethod
        def plot_trade_klines(trades, kline_data, output_path,
                              title="交易K线图 Trade Candlesticks", window_bars=20):
                """每个买入点前后的K线子图 (candlestick + 买卖标注)

                Args:
                    trades: 交易记录列表
                    kline_data: 原始K线 DataFrame, 需含 timestamp/open/high/low/close 列
                    output_path: PNG 输出路径
                    window_bars: 每个子图K线根数 (买入前后各一半)
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
                                ax.text(0.5, 0.5, "无数据", transform=ax.transAxes, ha='center', va='center', color='#888', fontsize=10)
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
                                ax.bar(j, body_height, bottom=body_bottom, color=color, width=0.7, edgecolor=color, linewidth=0.5, zorder=3)
                                ax.vlines(j, l, h, color=color, linewidth=0.8, zorder=2)

                        entry_rel_idx = entry_idx - start_idx
                        entry_price = t["entry_price"]
                        buy_color = '#00d4aa' if direction == 'long' else '#ff6b6b'
                        marker_buy = '^' if direction == 'long' else 'v'
                        ax.scatter([entry_rel_idx], [entry_price], color=buy_color, s=80, zorder=15, marker=marker_buy, edgecolors='white', linewidths=1.2)
                        ax.axhline(y=entry_price, color=buy_color, linestyle='--', linewidth=0.8, alpha=0.4, zorder=1)

                        if exit_ts and exit_ts in ts_to_idx:
                                exit_idx_local = ts_to_idx[exit_ts] - start_idx
                                if 0 <= exit_idx_local < sub_len:
                                        exit_price = t["exit_price"]
                                        sell_color = '#00d4aa' if pnl > 0 else '#ff6b6b'
                                        ax.scatter([exit_idx_local], [exit_price], color=sell_color, s=70, zorder=15, marker='o', edgecolors='white', linewidths=1.2)
                                        ax.axhline(y=exit_price, color=sell_color, linestyle=':', linewidth=0.8, alpha=0.4, zorder=1)
                                        ax.plot([entry_rel_idx, exit_idx_local], [entry_price, exit_price],
                                                color='#f7931a' if pnl > 0 else '#888', linestyle='-' if pnl > 0 else '--', linewidth=1.5, zorder=4, alpha=0.7)

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


# ============ 模块级接口函数 (兼容旧调用习惯) ============
plot_equity_chart = ChartPlotter.plot_equity_chart
plot_drawdown_chart = ChartPlotter.plot_drawdown_chart
plot_pnl_distribution = ChartPlotter.plot_pnl_distribution
plot_trade_klines = ChartPlotter.plot_trade_klines
