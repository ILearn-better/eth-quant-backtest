# components — 通用工具组件库

将项目中频繁调用、多处重复的工具函数/类归纳至此，按 **量化计算 / 绘图报告 / 数据获取** 三类分目录存放。

> ⚠️ 本目录是「归纳总结」产物，**未改动任何既有文件**（`live_trader.py` / `base.py` / `fetch_data.py` / `strategies/` 等保持原样继续工作）。既有代码可逐步将内联工具函数替换为 `from components.xxx import ...`。

## 目录结构

```
components/
├── quant/                      # 量化计算工具
│   ├── indicators.py           #   Indicators: ROC / MA / ATR / RSI
│   ├── statistics.py           #   compute_stats: 回测统计(收益率/夏普/回撤/盈亏比)
│   └── strategy_base.py        #   BaseStrategy: 策略基类
├── plotting/                   # 绘图与报告工具
│   ├── charts.py               #   ChartPlotter: 资金曲线/回撤/盈亏分布/交易K线
│   └── report.py               #   HtmlReport: HTML 回测报告
└── data/                       # 数据获取工具
    ├── datasource.py           #   DataSource: 现货/合约 URL 配置
    ├── fetcher.py              #   KlineFetcher: 历史 K 线下载(现货/合约通用)
    ├── network.py              #   ProxyClient + WeChatNotifier: 代理/微信通知
    └── timefmt.py              #   ts_to_str/ts_to_short/now: 时间格式化
```

## 归纳来源（重复函数统计）

| 工具 | 归纳至 | 原先重复位置 | 重复次数 |
|------|--------|-------------|---------|
| `calc_roc` | `quant/indicators.py` | live_trader / live_trader_contract / dashboard_server / paper_trading / eth_roc_momentum_v12 / eth_roc_momentum_contract | 6 |
| `calc_ma` | `quant/indicators.py` | 上述4服务 + eth_dca_v10 / eth_ma_reversion_v9 / eth_rsi_leverage 等 | 9 |
| `calc_atr` | `quant/indicators.py` | live_trader_contract / eth_roc_momentum_contract | 2 |
| `calc_rsi` | `quant/indicators.py` | eth_ma_reversion_v9 / eth_rsi_dca_v11 / eth_rsi_leverage | 3 |
| `compute_stats` | `quant/statistics.py` | base.py（核心，未重复但应归档） | 1 |
| `BaseStrategy` | `quant/strategy_base.py` | base.py（所有策略继承） | 1 |
| `plot_equity_chart` 等4图 | `plotting/charts.py` | base.py | 1 |
| `generate_html_report` | `plotting/report.py` | base.py（所有 main 入口调用） | 1 |
| `SPOT`/`FUTURES` 配置 | `data/datasource.py` | datasource.py | 1 |
| `fetch_klines`/`download`/`save` | `data/fetcher.py` | fetch_data.py / fetch_data_contract.py（几乎相同） | 2 |
| `get_proxy_opener` | `data/network.py` | live_trader / live_trader_contract / dashboard_server / paper_trading | 4 |
| `wx_notify` | `data/network.py` | live_trader / live_trader_contract / paper_trading | 3 |
| `ts_to_str`/`ts_to_short`/`now` | `data/timefmt.py` | live_trader / live_trader_contract | 2 |

## 用法

每个模块同时提供 **类接口** 和 **模块级函数**（兼容旧调用习惯）两种方式。

### 量化计算

```python
from components.quant import Indicators, compute_stats, BaseStrategy

# 指标计算 (静态方法, 接受 array-like, 返回等长 ndarray)
roc = Indicators.roc(closes, period=8)        # ROC, 前8个为 nan
ma  = Indicators.ma(volumes, period=20)       # SMA
atr = Indicators.atr(highs, lows, closes, 14) # ATR
rsi = Indicators.rsi(closes, period=14)       # RSI (Wilder)

# 兼容旧习惯的模块级函数
from components.quant import calc_roc, calc_ma
roc = calc_roc(closes, 8)

# 回测统计
stats = compute_stats(trades, initial_capital=150.0)

# 策略基类
class MyStrategy(BaseStrategy):
    name = "我的策略"
    def run_backtest(self, df):
        ...  # 返回 {'trades':..., 'equity_curve':..., 'stats':...}
```

### 绘图与报告

```python
from components.plotting import ChartPlotter, HtmlReport

# 4 类图表 → PNG
ChartPlotter.plot_equity_chart(equity_curve, 150, "equity.png", trades=trades)
ChartPlotter.plot_drawdown_chart(equity_curve, "drawdown.png")
ChartPlotter.plot_pnl_distribution(trades, "pnl.png")
ChartPlotter.plot_trade_klines(trades, kline_df, "klines.png")

# 一键生成 HTML 报告 (内部自动调用上述绘图)
HtmlReport.generate(stats, trades, equity_curve,
                    symbol="ETHUSDT", kline_df=df,
                    market="合约", data_range="近5年数据")
```

### 数据获取

```python
from components.data import DataSource, KlineFetcher, WeChatNotifier, ProxyClient
from components.data import ts_to_str, now

# 数据源
url = DataSource.futures("rest_kline")        # 合约 REST URL
ws  = DataSource.spot("ws_kline")             # 现货 WS URL

# 历史 K 线下载 (现货/合约通用)
KlineFetcher(market="futures").download_and_save(years=5)  # → data/futures/ETHUSDT-1h.csv
KlineFetcher(market="spot").download_and_save(years=5)     # → data/ETHUSDT-1h.csv

# 微信通知 (Server酱)
WeChatNotifier.send("做多信号", "ETH=1915.33")             # 同步, key 从环境变量 WX_SENDKEY 读
WeChatNotifier("SCTxxxx").send_async("标题", "内容")        # 异步(独立线程, 信号场景用)

# 代理
opener  = ProxyClient.opener()                # urllib opener
proxies = ProxyClient.proxies()               # requests 风格 dict

# 时间格式化
ts_to_str(1786093200000)   # "2026-08-07 17:00:00"
ts_to_short(1786093200000) # "08-07 17:00"
now()                       # "17:01:23"
```

## 与既有文件的关系

| 既有文件 | 保留用途 | 对应 components 模块 |
|---------|---------|---------------------|
| `base.py` | 既有 main_*.py 仍 import 它；StrategyBase 优先复用它 | quant/strategy_base.py (转发) |
| `datasource.py` | 既有 fetch_data_*.py / live_trader_*.py 引用 | data/datasource.py (复制+封装) |
| `fetch_data.py` / `fetch_data_contract.py` | 现有启动脚本仍可用 | data/fetcher.py (统一现货/合约) |
| `live_trader*.py` 内联工具函数 | 服务继续运行不改动 | quant/indicators.py, data/network.py, data/timefmt.py |

**迁移建议**：新写策略/服务时直接用 `components.*`；既有文件可在下次大改时逐步替换内联函数为 components 导入，减少重复维护。
