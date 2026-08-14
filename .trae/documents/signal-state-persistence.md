# 信号与持仓状态持久化 + 通知时间显示优化

## Context

当前 `live_trader.py`(现货 8080)和 `live_trader_contract.py`(合约 8081)的所有运行态都只活在内存里:

- `self.signal_log`(内存 list,截断 50 条)→ **调整参数/重启即丢失历史信号**,无法回验策略
- `self.position / bar_count / last_close_bar`(现货)/ `self.position / balance / peak_balance / trade_history / bar_counter`(合约)→ **重启即丢失持仓状态**,导致平仓信号断链:重启后 `self.position=None`,即使上一根 K 线还持着仓,系统也会当成空仓直接发新 BUY,平仓信号永远不再触发

另有两个相关问题:
1. **合约平仓信号丢失**:[live_trader_contract.py:321](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L321) 平仓事件只进了 `trade_history`,没进 `signal_log`,前端 `get_chart_data` 推的是 `signal_log`,所以**合约前端看不到任何平仓历史**(既有 bug)
2. **通知时间误导**:微信通知里只显示 `ts_to_str(signal['ts'])`,而 `ts` 是 K线开盘时间([live_trader.py:235](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L235))。1h K线 14:00 开盘、15:00 收盘,信号在 15:00 收盘后才发,但通知写"时间 14:00",让用户误以为信号迟到了 1 小时

**目标**:信号和持仓状态落盘 + 重启自动恢复持仓 + 通知同时显示"K线开盘时间"和"信号触发时间"消除歧义。现货合约都改。

## 用户已确认的决策

1. **持仓恢复**:自动恢复 + 启动时打印警告("检测到未平仓位 X,如已手动平仓请删除 data/state_spot.json")
2. **信号上限**:JSONL 文件无限追加(便于回测验证),内存和前端加载最近 1000 条
3. **改动范围**:现货 + 合约都改

## 持久化文件布局

```
data/
  ETHUSDT-1h.csv              # 既有现货K线
  signals_spot.jsonl          # 新:现货信号流,每行一条JSON,无限追加
  state_spot.json             # 新:现货持仓状态(position/bar_count/last_close_bar)
data/futures/
  signals_contract.jsonl      # 新:合约信号流
  state_contract.json          # 新:合约持仓状态(position/balance/peak_balance/bar_counter/trade_history)
```

遵循"现货存 data/、合约存 data/futures/"约定。目录已存在(data/futures/ 当前为空)。

## 实现方案

### 1. 持久化辅助函数(各文件内联,约 25 行)

按"新写功能是否纳入 components/ 留待阶段总结"约定,辅助函数直接写在各自 `live_trader*.py` 里,不引入 components 改动。每个文件加 4 个小函数:

- `_signal_file()` / `_state_file()` — 返回上面布局的路径(用 `BASE_DIR` 拼接,现货 `data/`、合约 `data/futures/`)
- `_append_signal_jsonl(signal)` — `open(path, 'a', encoding='utf-8')` 追加一行 `json.dumps(signal, ensure_ascii=False)`,失败只 print 警告不抛(避免影响信号链路)
- `_save_state()` — 把 position 等字段 dump 到临时文件再 `os.replace` 原子替换(避免写一半崩溃)
- `_load_state()` — 启动时读 state.json;读 JSONL 最近 1000 条到 `signal_log`

### 2. 信号持久化(JSONL append)

**现货** [live_trader.py:376-378](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L376-L378):
```python
if signal:
    signal["received_at"] = now()              # 本地时间字符串,排查延迟用
    self.signal_log.append(signal)
    self.signal_log = self.signal_log[-1000:]  # 50 → 1000
    _append_signal_jsonl(signal)               # 新增:落盘
```
BUY/SELL/CLOSE 三种信号都走这个分支(现货平仓信号在 [L324](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L324) 生成,也会到 L376),所以一改全覆盖。

**合约** [live_trader_contract.py:468-469](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L468-L469):入场信号同样加 `received_at` + 落盘 + 截断改 1000。

**合约平仓信号修复**(顺带修既有 bug):在 [`_close_position` L299-327](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L299-L327) 末尾,把 `trade` 转成 CLOSE 信号并 append 到 `signal_log` + 落盘:
```python
close_signal = {
    "type": "CLOSE", "direction": pos["direction"],
    "price": round(float(exit_price), 2), "ts": int(ts),
    "reason": reason, "pnl": round(net_pnl, 4),
    "entry_price": pos["entry_price"], "held_bars": ...,
    "received_at": now(),
}
self.signal_log.append(close_signal)
self.signal_log = self.signal_log[-1000:]
_append_signal_jsonl(close_signal)
return trade   # 返回值不变,不破坏 check_signal 契约
```
这样合约前端也能看到平仓历史。

### 3. 持仓状态持久化(JSON)

**现货**:
- 开仓后([L354 long / L368 short](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L354))调用 `_save_state()`
- 平仓后([L337 `self.position = None`](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L337))调用 `_save_state()`(position=None 也会写入,等于清空持仓)
- state 字段:`{position, bar_count, last_close_bar}`

**合约**:
- 开仓后([`_open_position` L284](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L284) 末尾)调用 `_save_state()`
- 平仓后([`_close_position` L326](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L326))调用 `_save_state()`
- state 字段:`{position, balance, peak_balance, bar_counter, trade_history}`

### 4. 启动加载与持仓恢复

在两个文件 `__init__` 末尾([现货 L155](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L155) / [合约 L194](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L194) 之后)调用 `_load_state()`:

```python
def _load_state(self):
    # 1. 加载历史信号(最近1000条)到 signal_log
    try:
        with open(self._signal_file(), encoding="utf-8") as f:
            lines = f.readlines()
        self.signal_log = [json.loads(l) for l in lines[-1000:] if l.strip()]
        print(f"  📂 已加载 {len(self.signal_log)} 条历史信号")
    except FileNotFoundError:
        pass
    # 2. 恢复持仓状态
    try:
        with open(self._state_file(), encoding="utf-8") as f:
            st = json.load(f)
        if st.get("position"):
            self.position = st["position"]
            self.bar_count = st.get("bar_count", 0)        # 现货
            self.last_close_bar = st.get("last_close_bar", -999)
            # 合约额外恢复 balance/peak_balance/bar_counter/trade_history
            print(f"  ⚠️ 检测到未平仓位: {self.position['direction']} "
                  f"@ {self.position['entry_price']} USDT")
            print(f"  ⚠️ 已自动恢复持仓跟踪。如已手动平仓,请删除 {self._state_file()} 后重启")
    except FileNotFoundError:
        pass
```

注意:`bar_count`/`bar_counter` 恢复后,冷却期和持仓时长计算能延续;但 K线缓冲 `self.bars` 仍靠 REST 回补(既有逻辑),重启后第一根 K 线收盘时 `check_signal` 用的是回补后的完整 bars,指标连续性不受影响。

### 5. 通知时间显示优化

把入场和平仓通知里的单行 `时间` 改成两行,同时显示开盘时间和触发时间:

**现货** [_print_signal_with_broadcast](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L1109-L1167):
- 入场通知 [L1164](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L1164):
  ```python
  f"- **K线时间(开盘)**: {ts_to_str(signal['ts'])}\n"
  f"- **信号触发(收盘)**: {now()}\n"
  ```
  (替换原 `f"- **时间**: {ts_to_str(signal['ts'])}\n"`)
- 平仓通知 [L1120-1129](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L1120-L1129):同样把单行 `时间` 改成两行

**合约** [_print_signal_with_broadcast L1313-1336](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L1313-L1336) 入场通知 [L1329](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L1329):同样改;平仓通知在 `_print_close_with_broadcast`([L1338+](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L1338))同样改。

`now()` 和 `ts_to_str()` 两文件都已有本地定义(合约可能在 components/data/timefmt.py 也有,但按"既有文件保持原样"用本地的即可)。

### 6. 不破坏的接口

- `check_signal` 返回值不变(现货返回 signal dict,合约返回 events list)
- `get_chart_data` 推送不变(`signals` 字段),前端无需改
- `wx_notify(title, content)` 签名不变,只改 content 内容
- `_close_position` 返回值不变(仍返回 trade),只在内部多 append 一个 CLOSE 信号

## 关键文件

| 文件 | 改动 |
|------|------|
| [live_trader.py](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py) | 加 4 个持久化辅助函数;L376-378 落盘+截断1000+received_at;L354/368/L337 调 _save_state;__init__ 末尾 _load_state;L1109-1167 通知时间双行显示 |
| [live_trader_contract.py](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py) | 同上 4 个辅助函数;L468-469 落盘;_close_position 内补 CLOSE 信号 append(修平仓不进 signal_log);L284/L326 调 _save_state;__init__ 末尾 _load_state;L1313-1336+平仓通知时间双行 |

## 复用的现有工具

- `ts_to_str / now` — 两文件已有本地定义(与 [components/data/timefmt.py](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/components/data/timefmt.py) 一致)
- `BASE_DIR` — 两文件已有([live_trader.py:34](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L34))
- 不引入 components/ 改动(遵循"新写功能纳入 components 留待阶段总结"约定)

## 验证方式

1. **启动现货**:`run_live.bat`,确认终端打印"已加载 N 条历史信号"(首次为 0),`data/signals_spot.jsonl` 和 `data/state_spot.json` 文件生成
2. **前端历史**:浏览器开 `http://127.0.0.1:8080`,信号列表能显示历史信号(首次启动后等下一根 K 线收盘产生新信号)
3. **持仓落盘**:等一个 BUY/SELL 信号触发后,检查 `data/state_spot.json` 有 position 字段;重启 `live_trader.py`,确认终端打印"检测到未平仓位..."警告 + signal_log 加载历史条数
4. **平仓链路**:持仓状态下等 ROC 反转触发 CLOSE,确认 `signals_spot.jsonl` 新增 CLOSE 行 + `state_spot.json` 的 position 清空
5. **合约平仓修复**:启动 `run_live_contract.bat`,等一次开仓→平仓,确认前端信号列表能看到 CLOSE 信号(此前看不到)
6. **通知时间**:触发信号时,微信通知应同时显示"K线时间(开盘)"和"信号触发(收盘)"两行,两者相差约 1 个 K 线周期属正常
7. **回测验证**:用 `data/signals_spot.jsonl` 的历史信号对照后续行情,检验策略出入场时机是否合理(这是用户保留信号的初衷)
