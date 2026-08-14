# 大资金流向综合看板(独立页面 + 路由切换)

## Context

当前监控面板只有 K线 + ROC 指标 + 信号,看不到主力资金动向。用户希望增加"大资金流向"辅助判断主力意图,与 ROC 信号配合决策。已确认:
- **数据类型**:综合看板 = 实时大单成交滚动 + 净买卖统计 + (合约)大户多空比
- **市场**:现货(8080)+ 合约(8081)都加
- **前端形式**:独立页面 + 路由切换(不在现有 sidebar 内嵌),大资金流向信息量大需全屏展示

## 数据源

### 1. aggTrade 流(现货 + 合约)
- 现货:`wss://stream.binance.com:9443/ws/ethusdt@aggTrade`
- 合约:`wss://fstream.binance.com/ws/ethusdt@aggTrade`
- 每笔字段:`p`(price) `q`(qty) `m`(is buyer maker) `T`(trade time)
- 方向判定:`m=false` → 主动买(taker buy),`m=true` → 主动卖(taker sell)
- 成交额 = `float(p) × float(q)`

### 2. 大户多空比(仅合约,REST 轮询)
- `https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=ETHUSDT&period=5m`(大户持仓多空比)
- `https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=ETHUSDT&period=5m`(账户多空比)
- 币安无 WS,每 5 分钟轮询;若接口返回 401/403(需 key)则 `_rest_get` 返回 None,降级跳过

## 默认参数(文件顶部可调)

```python
LARGE_ORDER_USDT = 100000   # 单笔成交额 ≥ 10万U 算大单
FLOW_WINDOWS = [5, 15, 60]  # 净买卖统计窗口(分钟)
LARGE_ORDER_KEEP = 50       # 后端保留大单条数(deque maxlen)
LSR_POLL_INTERVAL = 300     # 多空比轮询间隔(秒, 5分钟)
```

## 后端实现(两文件模式相同,合约多一个多空比轮询)

### A. aggTrade WS 订阅(复用现有 `_ws_connect`)
- 加常量 `WS_AGGTRADE_URL`(现货/合约各自 URL)
- 加方法 `on_aggtrade_message(self, ws, msg)`:
  ```python
  d = json.loads(msg)
  price = float(d["p"]); qty = float(d["q"]); usdt = price * qty
  if usdt >= LARGE_ORDER_USDT:
      side = "sell" if d.get("m") else "buy"   # m=true=主动卖
      order = {"ts": d["T"], "price": round(price,2), "usdt": round(usdt,0), "side": side}
      self.large_orders.append(order)           # deque 自动滚动
      self.flow_window.append(order)            # 统计窗口
  ```
  高频回调只做筛选 + append(deque 线程安全),不持锁不重算
- 加方法 `connect_aggtrade(self)` → `self._ws_connect(WS_AGGTRADE_URL, self.on_aggtrade_message, "Aggtrade")`
- `start()` 启动第三个 WS 线程,复用 [_ws_connect 现货 L597-621](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L597-L621) / [合约 L717-741](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L717-L741)

### B. `__init__` 加状态字段
```python
from collections import deque
self.large_orders = deque(maxlen=LARGE_ORDER_KEEP)
self.flow_window = deque(maxlen=5000)
self.long_short_ratio = None   # 仅合约会填充
```

### C. `get_chart_data()` 加字段(随 broadcast 自动推送)
[现货 L670-746](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L670-L746) / [合约 L848-875](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L848-L875) 返回 dict 末尾加:
```python
"large_orders": list(self.large_orders)[-30:],   # 前端展示最近30条
"flow_stats": self._calc_flow_stats(),
"long_short_ratio": self.long_short_ratio,         # 现货恒为 None
```
`_calc_flow_stats()` 计算 5/15/60 分钟窗口净额 + 买卖比:
```python
def _calc_flow_stats(self):
    now_ms = time.time() * 1000
    stats = []
    for mins in FLOW_WINDOWS:
        cutoff = now_ms - mins * 60 * 1000
        buys = sum(o["usdt"] for o in self.flow_window if o["ts"] >= cutoff and o["side"]=="buy")
        sells = sum(o["usdt"] for o in self.flow_window if o["ts"] >= cutoff and o["side"]=="sell")
        stats.append({"window": mins, "buy": round(buys,0), "sell": round(sells,0),
                      "net": round(buys-sells,0), "ratio": round(buys/sells,2) if sells else 0})
    return stats
```

### D. 合约多空比轮询线程(仅 live_trader_contract.py)
复用 [_rest_get L263-279](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py#L263-L279):
```python
def _poll_long_short_ratio(self):
    while self.running:
        top = self._rest_get("https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol=ETHUSDT&period=5m")
        acct = self._rest_get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=ETHUSDT&period=5m")
        if top and acct:
            t, a = top[-1], acct[-1]
            self.long_short_ratio = {
                "top_ratio": float(t["longShortRatio"]),
                "top_long": float(t["longAccount"]), "top_short": float(t["shortAccount"]),
                "acct_ratio": float(a["longShortRatio"]),
                "acct_long": float(a["longAccount"]), "acct_short": float(a["shortAccount"]),
                "ts": int(t["timestamp"]),
            }
        time.sleep(LSR_POLL_INTERVAL)
```
`start()` 启动第四个线程。

## 前端:独立页面 + 路由切换

采用独立页面方案:新增 `/flow` 路由返回大资金专属 HTML 页面,与现有信号面板通过导航栏互跳。两页面**共用同一个 `/ws` broadcast**(数据已含 large_orders/flow_stats/long_short_ratio),各自只渲染需要的字段。

### 路由
- `http://127.0.0.1:8080/` → 现货信号面板(现有,不变)
- `http://127.0.0.1:8080/flow` → 现货大资金面板(新)
- `http://127.0.0.1:8081/` → 合约信号面板(现有,不变)
- `http://127.0.0.1:8081/flow` → 合约大资金面板(新)

### 后端新路由(两文件各加一个)
[现货 @app.get("/") L1083-1085](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L1083-L1085) 旁加:
```python
@app.get("/flow")
async def flow_page():
    return HTMLResponse(HTML_PAGE_FLOW)
```

### 现有信号面板改动(极小,只加导航链接)
[现货 header nav L810-813](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L810-L813) 在现货/合约链接前加页内路由切换:
```html
<div class="nav">
  <a href="/" class="active">📊 信号</a>
  <a href="/flow">🐋 大资金</a>
  <a href="http://127.0.0.1:8080">现货</a>
  <a href="http://127.0.0.1:8081">合约</a>
</div>
```
(大资金页 `HTML_PAGE_FLOW` 的 nav 反过来:`🐋 大资金` 加 active)

### 新页面 HTML_PAGE_FLOW(全屏,信息量大)
独立模板,结构:
- **顶部导航栏**:📊信号(`/`)| 🐋大资金(current,active)| 现货↔合约跨服务
- **统计卡片区**(3 个大卡片横排):5min / 15min / 60min 净买卖额
  - 卡片内容:窗口标签 + 净额(万U,绿=净买/红=净卖)+ 买入额/卖出额 + 买卖比
- **大户多空比区**(仅合约,现货币板此区不渲染):
  - 大户持仓多空比 + 多/空账户占比
  - 账户多空比 + 多/空账户占比
  - 解读提示(>1 偏多, <1 偏空)
- **大单滚动列表**(全宽, max-height 滚动):每条 = 方向emoji + 额(万U)+ 价格 + 时间
  - 买绿左条 / 卖红左条,新单顶部插入
- **可选增强**:ECharts 实时大单柱状图(按分钟聚合,买绿柱/卖红柱,叠加净额折线)

页面连同一 `/ws`,JS 收消息后只渲染大资金字段:
```javascript
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'init' || msg.type === 'update') {
    renderFlowStats(msg.data.flow_stats);
    renderLargeOrders(msg.data.large_orders);
    if (msg.data.long_short_ratio) renderLSR(msg.data.long_short_ratio);
  }
};
```

### 导航样式
复用现有 `.nav` / `.nav a` / `.nav a.active` 样式([现货 L767-770](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py#L767-L770)),两个页面各自给当前页路由加 `.active` 高亮。

## 关键文件改动

| 文件 | 改动 |
|------|------|
| [live_trader.py](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader.py) | 加 WS_AGGTRADE_URL + LARGE_ORDER_USDT 常量;__init__ 加 large_orders/flow_window deque;on_aggtrade_message + connect_aggtrade + start 第三线程;_calc_flow_stats;get_chart_data 加 large_orders/flow_stats;**@app.get("/flow") 路由 + HTML_PAGE_FLOW 新模板**;现有 HTML nav 加"🐋 大资金"链接 |
| [live_trader_contract.py](file:///q:/Program%20Files/workbuddy/2026-08-06-14-46-22/eth-quant-backtest/live_trader_contract.py) | 同上 + 多空比轮询线程 _poll_long_short_ratio + start 第四线程 + long_short_ratio 字段 + HTML_PAGE_FLOW 含多空比区 |

## 不破坏的接口

- `check_signal` / `get_chart_data` 现有字段不变,只**新增** large_orders/flow_stats/long_short_ratio
- 现有 `/` 信号面板渲染逻辑不变,只在 nav 加一个链接
- aggTrade WS 独立线程,失败不影响 K线/价格信号链路
- `/ws` broadcast 不变,两页面共用

## 线程安全

- aggTrade 回调高频,只做筛选 + `deque.append`(CPython deque append 线程安全)
- `_calc_flow_stats` 在 broadcast 线程读 deque,遍历时 append 可能少算一两条,统计可接受
- 不加锁,避免高频回调持锁阻塞

## 复用现有

- `_ws_connect`(连接 + 代理 + 重连)直接复用加第三个流
- `_rest_get`(合约)复用做多空比轮询
- 前端 `.nav` / `.signal-list` / `.indicator-row` CSS 风格复用
- `/ws` broadcast 数据流两页面共用

## 验证方式

1. **现货信号面板**:启动 `run_live.bat`,访问 `http://127.0.0.1:8080/`,确认导航栏出现"📊信号 | 🐋大资金"两个链接,现有 K线/信号不受影响
2. **现货大资金页面**:点"🐋 大资金"(或直接访问 `http://127.0.0.1:8080/flow`),确认新页面渲染:统计卡片(5/15/60min)+ 大单滚动列表;观察 1-2 分钟应有大单出现(ETH 10万U 大单约几分钟一次)
3. **路由切换**:在 `/` 和 `/flow` 之间点导航切换,确认两边都正常更新数据(共用 WS)
4. **合约大资金**:启动 `run_live_contract.bat`,访问 `http://127.0.0.1:8081/flow`,确认多空比区显示数值(若接口受限则隐藏,不报错)
5. **断网容错**:断代理,确认 aggTrade WS 重连不崩,K线/信号链路不受影响
6. **性能**:确认 aggTrade 高频回调不拖慢 broadcast(两页面仍每 3 秒刷新)
