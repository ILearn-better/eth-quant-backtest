"""绘图与报告工具 (matplotlib 图表 / HTML 报告)"""
from components.plotting.charts import (ChartPlotter, plot_equity_chart,
        plot_drawdown_chart, plot_pnl_distribution, plot_trade_klines)
from components.plotting.report import HtmlReport, generate_html_report

__all__ = ["ChartPlotter", "plot_equity_chart", "plot_drawdown_chart",
           "plot_pnl_distribution", "plot_trade_klines",
           "HtmlReport", "generate_html_report"]
