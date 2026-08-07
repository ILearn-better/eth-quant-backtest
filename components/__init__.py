"""components — 通用工具组件库

将项目中频繁调用、多处重复的工具函数/类归纳至此, 按三类分目录存放:

    components/quant/      量化计算工具 (指标/统计/策略基类)
    components/plotting/   绘图与报告工具 (matplotlib 图表/HTML 报告)
    components/data/       数据获取工具 (数据源/历史下载/网络/通知/时间)

说明:
    - 本目录是「归纳总结」产物, 未改动任何既有文件 (live_trader.py / base.py /
      fetch_data.py / strategies/ 等保持原样继续工作).
    - 既有代码可逐步将内联的工具函数替换为 `from components.xxx import ...`.
    - 每个模块同时提供「类接口」和「模块级函数」两种调用方式, 后者兼容旧习惯.

快速导入:
    from components.quant import Indicators, compute_stats, BaseStrategy
    from components.plotting import ChartPlotter, HtmlReport
    from components.data import DataSource, KlineFetcher, WeChatNotifier
"""
