"""HTML 回测报告生成工具

归纳自 base.py:480 的 `generate_html_report`. 该函数被所有 main_*.py 回测入口
调用, 内部会先调用 ChartPlotter 生成 4 张 PNG, 再拼装 HTML.

封装为 HtmlReport 类, 逻辑与原 base.py 一致; 支持 market/data_range 可选参数
(合约传 market="合约", data_range="近5年数据" 在标题/子标题/图表标注).

用法:
    from components.plotting.report import HtmlReport
    HtmlReport.generate(stats, trades, equity_curve, symbol="ETHUSDT",
                        kline_df=df, market="合约", data_range="近5年数据")
"""
import os
import json
from datetime import datetime

from components.plotting.charts import ChartPlotter


class HtmlReport:
        """回测 HTML 报告生成器 (静态方法)

        输出: {output_dir}/backtest_{symbol}_{interval}_{timestamp}.html
              {output_dir}/charts/*.png  (4 张图表)
        """

        @staticmethod
        def generate(stats, trades, equity_curve, symbol="ETHUSDT", interval="1h",
                      output_dir="reports", kline_df=None, market="", data_range="近2年数据"):
                """生成 HTML 回测报告

                Args:
                    stats:         compute_stats 返回的统计 dict
                    trades:        交易记录列表
                    equity_curve:  [(ts_ms, balance), ...]
                    symbol:        交易对
                    interval:      K线周期
                    output_dir:    报告输出目录
                    kline_df:      原始K线 DataFrame, 提供时绘制交易K线图
                    market:        市场标签(如 "合约"), 非空时在标题/子标题/图表追加标注, 默认空=现货
                    data_range:    数据范围描述, 默认 "近2年数据"
                """
                os.makedirs(output_dir, exist_ok=True)
                os.makedirs(os.path.join(output_dir, "charts"), exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                market_tag = f" · {market}" if market else ""
                market_field = f" | {market}" if market else ""
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

                ChartPlotter.plot_equity_chart(equity_curve, initial_capital,
                       os.path.join(output_dir, eq_img),
                       title=f"{symbol} RSI策略 - 资金曲线{market_tag}", trades=trades)
                ChartPlotter.plot_drawdown_chart(equity_curve,
                         os.path.join(output_dir, dd_img),
                         title=f"{symbol} RSI策略 - 回撤曲线{market_tag}")
                ChartPlotter.plot_pnl_distribution(trades,
                           os.path.join(output_dir, pnl_img),
                           title=f"{symbol} RSI策略 - 盈亏分析{market_tag}")
                kline_path = None
                if kline_df is not None and len(kline_df) > 0:
                        kline_path = ChartPlotter.plot_trade_klines(trades, kline_df,
                                       os.path.join(output_dir, kline_img),
                                       title=f"{symbol} RSI策略 - 交易K线{market_tag}")

                # ===== 构建 HTML =====
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
<title>ETH RSI 杠杆策略回测报告{market_tag}</title>
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
    <h1>📊 ETH RSI 分仓杠杆策略 回测报告{market_tag}</h1>
    <div class="sub">{symbol} | {interval} | {data_range}{market_field} | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
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


# ============ 模块级接口函数 (兼容旧调用习惯) ============
generate_html_report = HtmlReport.generate
