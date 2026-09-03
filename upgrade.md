# 升级与问题记录 (upgrade.md)

> 本文档记录 ETHUSDT 量化交易框架历次功能升级、目录重构与问题排查，随重大变更同步更新。
> 最近一轮：2026-08 ~ 2026-09-03（实时服务保障 + 日志体系 + 目录重构 + 仓库同步）。

## 目录

1. [目录结构与仓库同步](#1-目录重构与仓库同步)
2. [日志体系规范化](#2-日志体系规范化-daily_logpy)
3. [问题排查与修复](#3-问题排查与修复)
4. [服务启动与健康验证](#4-服务启动与健康验证)
5. [git 提交记录](#5-git-提交记录)
6. [遗留风险与待办](#6-遗留风险与待办)

---

## 1. 目录重构与仓库同步

### 1.1 整理原则

主目录只保留**核心运行文件**：三个实时服务入口、启动脚本、回测入口、基础库与数据源管理。历史/试验/工具类脚本全部移入子目录，下载链工具集中到 `data/data_utils/`，便于维护与检索。

### 1.2 移动清单

| 原位置 | 目标 | 内容 |
|--------|------|------|
| 根目录 | `test/` | main.py(v10) / main_v11.py / main_extreme_contract.py / main_low_pyramid_contract.py / bt_run_contract.py / compare_all.py / optimize.py / dashboard_server.py / health_check.py / net_test.py / paper_trading.py / paper_report.py / analyze_wick.py / test_api.py / start_paper_trading.bat |
| 根目录 | `alpha_optimize/` | optimize_v12.py / bt_compare_contract.py（v12 参数调优、现货/合约回测对比） |
| 根目录 | `data/data_utils/` | fetch_data.py / fetch_data_contract.py / dl_gap.py / dl_parallel.py / dl_merge.py / dl_patch_gaps.py / dl_check_gap.py |
| 根目录 | `data/temp/` | dl_accum.jsonl / dl_accum_gap.jsonl / dl_accum_miss.jsonl（下载断点续传中间产物，不入库） |

### 1.3 保留在根目录的说明

- `dashboard_server.py` 经验证已被 `live_trader.py` / `live_trader_contract.py` 内嵌 FastAPI 仪表盘完全取代（同端口互斥、无启动脚本引用）→ 移入 `test/` 备查。
- `binance_testnet.py` 被 `alpha_lab.py`（运行中，8082）import 依赖，移走会导致服务崩溃 → 保留根目录。

### 1.4 批量修补

共修补 24 个被移动脚本：内部 `os.path.dirname(os.path.abspath(__file__))` 目录基准按移动后的目录深度上提（嵌套 dirname），并注入 `sys.path` 指向项目根。9 个代表性脚本冒烟验证通过（其中 bt_compare_contract.py 完整跑通一次回测并落盘报告）。

## 2. 日志体系规范化 (daily_log.py)

新增根目录 `daily_log.py`（纯标准库、零第三方依赖），控制台 + 文件**双写**、按天**轮转**：

- 入口模块 `__main__` 顶部调用 `daily_log.setup("模块名")`（import 无副作用）；
- 日志落盘 `logs/<模块>/<YYYY-MM-DD>.log`，每个文件只记录当天，跨天自动建新文件；
- 每行实时 flush，进程退出自动收尾；uvicorn/fastapi 自身 logging 不受影响。

已接入模块：`live_trader.py` → `logs/spot/`，`live_trader_contract.py` → `logs/contract/`，`alpha_lab.py` → `logs/alpha/`。新增常驻服务必须接入（模块名用英文语义词）。

`logs/`、`_*.txt`、`dl_accum*.jsonl`、运行状态 jsonl、5m 大体积历史文件等均已加入 `.gitignore` 不入库。

## 3. 问题排查与修复

### 3.1 数据断更 = 本地代理 (7897) 节点失效（重要，两次）

- **现象**：服务进程存活（`/health` 能响应）但 K线/大单/WebSocket 全部停更（如现货K线停在 8-21 04:00，前端数据停在 8-20/8-21）；9-03 再次复现。
- **根因**：Clash 本地代理 `127.0.0.1:7897` 出网节点失效，走代理的 Binance 请求 SSL 握手即断（`UNEXPECTED_EOF` / `WinError 10061`）。
- **判断标准**：看**服务进程自己**能否恢复数据，不能只看沙箱终端测试（沙箱网络本来就受限，易误判）。
- **恢复流程**：用户修复代理节点后，合约靠 30s 自愈自动回补恢复；现货无自愈逻辑，需重启（启动时 fetch_history 拉满）或等 WebSocket 恢复后慢慢积累。
- **教训**：数据断更排查第一步看 `netstat` 确认服务进程是否走了代理、代理是否真的通；前端价格停在旧时间点而进程活着 = 网络出口故障。

### 3.2 合约K线只有 2 根 / 指标全 NaN（空缓冲）

- **现象**：前端K线只显示 2 根蜡烛、ROC/VolMA 等指标全 null；但 REST ticker / 大单 / 多空比均正常。
- **根因**：启动时 `fetch_history()`（limit=300）瞬时失败 → 以空缓冲启动；`_poll_kline_rest` 固定只拉 limit=2 只能维持最新 2 根，**永不自愈**。
- **修复**：在 `_poll_kline_rest` 每循环开头加自愈逻辑——缓冲长度不足 `HISTORY_BARS` 时每 30s 重试 `fetch_history()` 回补全量。已验证：代理恢复后合约自动回补 300 根、指标正常。
- **排查要点**：指标数组全 null + klines 数量过少 = 启动回补失败且无恢复路径。

### 3.3 现货"最近没信号"

- **结论：非故障**。v12 策略处于缩量观望状态（`vol_ratio` 成交量确认不达标），属策略正常行为，而非推送/服务问题。

### 3.4 附：历史关键修复速查（早期已完成）

- **整点K线收盘卡死（08-07）**：`with self.lock` 内调用 `check_signal→compute_indicators` 二次拿锁，`threading.Lock` 不可重入 → 同线程死锁，前端 HTTP/WS 全部阻塞但价格日志照常。修复：信号检查/打印移到锁外执行（仅 bars 更新在锁内）。
- **前端指标滞后 1 小时**：WS 广播使用了缓存指标。修复：广播前实时重算 ROC / VolMA。
- **前端K线与交易所不一致**：K线数组顺序错误。修复：统一按 `[open, close, low, high]` 输出以对齐 Binance 显示。
- **沙箱网络误判（08-17）**：IDE 沙箱终端对 fapi 外网 SSL 异常，曾误判为服务故障。正确做法：以服务 `/health` `/api/data` 实际数据为准，不在终端裸测外网下结论。

## 4. 服务启动与健康验证

三服务独立常驻（现货/合约/Alpha 各自独立进程、独立端口、互不修改对方代码）：

| 服务 | 端口 | 入口 | 启动 | 日志 |
|------|:---:|------|------|------|
| 现货 | 8080 | live_trader.py | run_live.bat | logs/spot/ |
| 合约 | 8081 | live_trader_contract.py | run_live_contract.bat | logs/contract/ |
| Alpha | 8082 | alpha_lab.py | run_alpha.bat | logs/alpha/ |

健康检查均 200。注：Alpha 服务控制台横幅在部分终端显示 GBK 乱码，属控制台编码问题，`daily_log` 落盘内容 UTF-8 正常，不影响使用。

## 5. git 提交记录

```
32df424  init: ETH量化回测+实盘监控项目
3953483  merge: 合并 main 分支至 master (保留历史)
0104a56  feat: Testnet实盘接入、Alpha因子实验室、合约共振策略、手动下单与大资金流
ce37dec  chore: gitignore 补充忽略合约/现货日志与模拟盘状态文件
47e4006  refactor: 目录整理 + daily_log 按天日志体系   ← 本轮目录重构
(待提交) docs: 更新 README + 新增 upgrade.md + 清理遗留临时文件
```

## 6. 遗留风险与待办

1. **WX_SENDKEY 安全风险**：`run_live.bat` / `run_live_contract.bat` 中硬编码的 ServerChan 微信推送密钥已进入远程 git 历史。若仓库公开，应**重置密钥**并改为从 `.env` 读取（`.env` 已在 `.gitignore`）。
2. **现货无自愈逻辑**：启动时 fetch_history 失败则 bars 一直为 0（已知短板）。建议后续给现货侧也加与合约一致的定时回补自愈。
3. **下载限流红线**：Binance 权重限流 2400/min，下载脚本一律串行（WORKERS=1）+ 每批间隔 ≥2s + 418/429 长退避 + 断点续传；严禁并发/毫秒级间隔拉取，否则 418 IP 封禁持续数小时且探测会刷新封禁。
4. **接口契约约定**：涉及参数/数据格式等接口级大改时不动现有接口，新功能独立新写；新写功能是否并入 `components/` 留待阶段总结再判断。
