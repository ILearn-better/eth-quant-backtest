# ETHUSDT 量化交易框架

基于 Binance 数据源的 ETHUSDT 量化回测 + 实时交易系统。包含现货 / 合约双轨实时策略服务（内嵌 Web 仪表盘）、v8~v12 迭代策略回测，以及 Alpha 因子实验室。历史试验脚本统一收敛在 `test/`，调参工具在 `alpha_optimize/`，下载工具链在 `data/data_utils/`，主目录只保留核心运行文件。

## 策略表现总览

回测区间: 2024-05 ~ 2026-05 | 本金 150 USDT | 数据: ETHUSDT 1h (17520根)

| 版本 | 策略 | 杠杆 | 收益率 | 胜率 | 最大回撤 | 状态 |
|------|------|:---:|:---:|:---:|:---:|:---:|
| v8 | RSI双向 + MA150 (日线) | 3x | +10.01% | 58.3% | 13.74% | 正收益 |
| v9 | 月线均值回归 (1h) | 3x | -46.08% | 37.3% | 58.23% | 亏损中 |
| v10 | 月均线DCA抄底 (1h) | 3x | -14.78% | 0% | 14.78% | 亏损中 |
| v11 | RSI超卖定投 FIFO | 10x | +54.25% | 64.4% | 17.25% | 正收益 |
| **v12** | **双ROC动量 + 成交量** | **3x** | **+88.13%** | 37.8% | 25.86% | **最优** |

## 实时服务布局

| 服务 | 端口 | 数据 | 入口 | 一键启动 | 日志模块 |
|------|:---:|------|------|------|------|
| 现货实盘 | 8080 | `data-stream.binance.vision` | `live_trader.py` | `run_live.bat` | `spot` |
| 合约实盘 | 8081 | `fstream.binance.com` | `live_trader_contract.py` | `run_live_contract.bat` | `contract` |
| Alpha 实验室 | 8082 | 现货/自定义因子 | `alpha_lab.py` | `run_alpha.bat` | `alpha` |

每个服务内嵌 FastAPI 仪表盘（`/` 看板、`/api/data` 实时数据、`/health` 健康检查），并可通过 WebSocket 实时推送（3s 间隔，含最新重算指标）。实时策略运行需本地代理访问 Binance，默认 `127.0.0.1:7897`。

## 项目结构

```
├── live_trader.py            # 现货实时服务 (8080)
├── live_trader_contract.py   # 合约实时服务 (8081)
├── alpha_lab.py              # Alpha 因子实验室 (8082)
├── run_live.bat              # 现货一键启动
├── run_live_contract.bat     # 合约一键启动
├── run_alpha.bat             # Alpha 一键启动
├── base.py                   # 回测基类 + 统计 + HTML 报告
├── daily_log.py              # 按天日志工具 (logs/<模块>/<日期>.log)
├── datasource.py             # 数据源 URL 集中管理 (SPOT / FUTURES)
├── binance_testnet.py        # Testnet 接入 (alpha_lab 依赖)
├── main_v12.py               # 现货 1h 回测入口
├── main_v12_contract.py      # 合约 1h 回测入口
├── main_v12_contract_5m.py   # 合约 5m 回测入口
├── components/               # 可复用组件 (quant/plotting/data, 见其 README)
├── strategies/               # 策略实现 (v8~v12 + 合约专属策略)
├── dashboard_static/         # 前端仪表盘静态资源 (echarts)
├── test/                     # 历史/试验脚本 (回测旧版、paper_trading、工具)
├── alpha_optimize/           # 参数调优工具 (optimize_v12 / bt_compare_contract)
├── alpha_lab/                # Alpha 因子库包 (backtest/operators/parser/storage)
├── data/
│   ├── ETHUSDT-1h.csv        # 现货 1h 历史
│   ├── ETHUSDT-1d.csv        # 现货 1d 历史
│   ├── futures/              # 合约历史 (ETHUSDT-1h.csv 等)
│   ├── data_utils/           # 下载工具链 (fetch_data / dl_parallel 等)
│   ├── temp/                 # 下载断点续传中间产物 (dl_accum*.jsonl, 不入库)
│   └── alpha/                # Alpha 因子数据
├── logs/                     # 运行时日志 logs/<模块>/<YYYY-MM-DD>.log (不入库)
├── reports/                  # 回测 HTML 报告 (不入库)
├── requirements.txt          # Python 依赖
└── .gitignore
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动实时服务 (需本地代理, 默认 127.0.0.1:7897)
run_live.bat             # 现货 8080
run_live_contract.bat    # 合约 8081
run_alpha.bat            # Alpha 8082
# 或直接用 venv python 启动: python live_trader.py

# 3. 运行回测
python main_v12.py              # 现货 1h
python main_v12_contract.py     # 合约 1h
python main_v12_contract_5m.py  # 合约 5m
```

## 实时服务说明

- **K线推送**: 通过 Binance WebSocket 接收 K线，收盘时执行 v12 策略（双ROC动量 + 成交量确认 + 杠杆），信号/状态持久化到 `data/`，断线重启自动补拉最近 300 根历史。
- **WebSocket 广播**: 每 3s 推送最新行情与实时重算指标，ECharts K线按 `[open, close, low, high]` 顺序渲染以对齐交易所显示。
- **合约自愈**: 合约服务启动回补失败（空缓冲）时会每 30s 重试拉全量历史，可自愈恢复；现货无自愈逻辑，回补失败需重启服务。
- **日志**: 各服务入口 `daily_log.setup("模块名")` 开启按天日志，控制台 + `logs/<模块>/<YYYY-MM-DD>.log` 双写，每个文件只记录当天。

## 数据下载

下载工具集中在 `data/data_utils/`（数据源 URL 见根目录 `datasource.py`）。注意 Binance 权重限流：一律串行下载 + 长退避 + 断点续传，不要并发拉取，否则会触发 418 IP 封禁。

## 策略开发说明

每个策略继承 `BaseStrategy` 并实现 `run_backtest(df)`:

```python
class MyStrategy(BaseStrategy):
    name = "我的策略"
    CAPITAL = 150.0
    LEVERAGE = 3

    def run_backtest(self, df):
        # 1. 计算指标 (ROC / MA / 成交量...)
        # 2. 遍历K线, 维护持仓状态, 判断开平仓
        # 3. 记录 trades 和 equity_curve
        return {"trades": trades, "equity_curve": equity_curve, "stats": stats}
```

回测结果自动生成 HTML 报告（资金曲线 + 回撤曲线 + 盈亏分布 + 交易K线）到 `reports/` 目录（合约在 `reports/contract/`）。

## 升级记录

历次功能升级与问题排查记录见 [upgrade.md](upgrade.md)。

## 免责声明

本项目仅供量化交易学习研究使用，不构成任何投资建议。加密货币交易风险极高，请谨慎对待。实时交易为模拟/策略盘，不代表真实交易收益，实际交易还需考虑滑点、资金费率、流动性等因素。
