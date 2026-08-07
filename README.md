# ETHUSDT 量化交易框架

基于 Binance 现货 K线数据的 ETHUSDT 量化回测 + 实盘模拟盘系统。包含从 v8 到 v12 共 5 个迭代策略版本，以及一个可实时运行的模拟盘（Paper Trading）。

## 策略表现总览

回测区间: 2024-05 ~ 2026-05 | 本金 150 USDT | 数据: ETHUSDT 1h (17520根)

| 版本 | 策略 | 杠杆 | 收益率 | 胜率 | 最大回撤 | 状态 |
|------|------|:---:|:---:|:---:|:---:|:---:|
| v8 | RSI双向 + MA150 (日线) | 3x | +10.01% | 58.3% | 13.74% | 正收益 |
| v9 | 月线均值回归 (1h) | 3x | -46.08% | 37.3% | 58.23% | 亏损中 |
| v10 | 月均线DCA抄底 (1h) | 3x | -14.78% | 0% | 14.78% | 亏损中 |
| v11 | RSI超卖定投 FIFO | 10x | +54.25% | 64.4% | 17.25% | 正收益 |
| **v12** | **双ROC动量 + 成交量** | **3x** | **+88.13%** | 37.8% | 25.86% | **最优** |

## 项目结构

```
├── base.py                  # 回测基类 + 统计计算 + HTML报告/图表
├── main.py                  # v10 回测入口
├── main_v11.py              # v11 回测入口
├── main_v12.py              # v12 回测入口
├── optimize.py              # 参数扫描工具
├── fetch_data.py            # K线数据下载工具
├── paper_trading.py         # 实盘模拟盘 (v12, WebSocket常驻)
├── paper_report.py          # 模拟盘日报生成器
├── start_paper_trading.bat  # 模拟盘一键启动 (Windows)
├── data/                    # K线数据 (ETHUSDT-1h.csv / ETHUSDT-1d.csv)
├── strategies/              # 策略实现 (v8~v12) + 策略文档
└── requirements.txt         # Python 依赖
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行回测 (以 v12 为例)
python main_v12.py

# 3. 运行实盘模拟盘 (需要代理访问 Binance 数据源)
python paper_trading.py
# 或 Windows 下双击 start_paper_trading.bat

# 4. 查看模拟盘日报
python paper_report.py
```

## 模拟盘说明

模拟盘通过 Binance WebSocket (`wss://data-stream.binance.vision/ws/ethusdt@kline_1h`) 实时接收 1h K线，在每根K线收盘时执行 v12 策略逻辑（双ROC动量 + 成交量确认 + 3x杠杆）。

- **状态持久化**: 运行状态保存到 `paper_state/`，进程重启自动恢复并补跑错过的K线
- **断线补跑**: 重启时拉取最近300根历史K线，从上次处理位置逐根补跑完整策略逻辑
- **数据源**: 使用 Binance 公共市场数据端点 (`data-stream.binance.vision`)，避免主域名地区限制
- **日报自动化**: 可配置每日定时生成 HTML 日报 (权益曲线 + 交易明细)

## 策略开发说明

每个策略继承 `BaseStrategy` 并实现 `run_backtest(df)`:

```python
class MyStrategy(BaseStrategy):
    name = "我的策略"
    CAPITAL = 150.0
    LEVERAGE = 3

    def run_backtest(self, df):
        # 1. 计算指标 (RSI / MA / ROC / 成交量...)
        # 2. 遍历K线, 维护持仓状态, 判断开平仓
        # 3. 记录 trades 和 equity_curve
        return {"trades": trades, "equity_curve": equity_curve, "stats": stats}
```

回测结果自动生成 HTML 报告 (资金曲线 + 回撤曲线 + 盈亏分布 + 交易K线) 到 `reports/` 目录。

## 免责声明

本项目仅供量化交易学习研究使用，不构成任何投资建议。加密货币交易风险极高，请谨慎对待。模拟盘结果不代表真实交易收益，实际交易还需考虑滑点、资金费率、流动性等因素。
