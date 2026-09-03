"""Alpha 因子实验室 — 世坤(WorldQuant)风格因子回测平台 (8082)

独立服务, 与现货(8080)/合约(8081)交易服务解耦, 不污染实时交易代码.

    http://127.0.0.1:8082/alpha — 因子实验室页面

    /alpha/api/backtest    POST  表达式回测
    /alpha/api/factors     GET   因子列表
    /alpha/api/factors     POST  保存因子
    /alpha/api/factors/{name}  DELETE/GET
    /alpha/api/operators   GET   算子/字段说明
    /alpha/api/datafiles   GET   可用历史数据

启动:
    python alpha_lab.py
"""
import json
import os
import sys
import time
import urllib.request

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List

from alpha_lab.backtest import load_data, run_backtest, available_data_files
from alpha_lab.operators import OPERATORS, FIELDS
from alpha_lab import storage
from binance_testnet import TestnetClient
import daily_log  # 按天双写日志: logs/alpha/YYYY-MM-DD.log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "dashboard_static")
SERVER_PORT = 8082

# 数据缓存 (回测重复调用避免重复读盘)
_DATA_CACHE = {}


def _get_data(path):
    key = os.path.abspath(path)
    if key not in _DATA_CACHE:
        _DATA_CACHE[key] = load_data(key)
    return _DATA_CACHE[key]


# ---- 请求模型 ----
class BacktestRequest(BaseModel):
    expr: str
    file: str = "ETHUSDT-1h.csv"
    market: str = "现货"
    z_window: int = 60
    threshold: float = 1.0
    deadzone: float = 0.3
    fee_rate: float = 0.0005
    leverage: float = 1.0
    ppy: str = "1h"


class SaveFactorRequest(BaseModel):
    name: str
    expr: str
    params: dict
    metrics: dict


class TestnetOrderRequest(BaseModel):
    market: str               # spot / futures
    side: str                 # buy / sell
    order_type: str           # market / limit
    qty: float
    price: Optional[float] = None
    leverage: Optional[int] = None
    reduce_only: bool = False


class TestnetCloseRequest(BaseModel):
    market: str               # spot / futures


# ---- Testnet 客户端缓存 ----
_TESTNET_CACHE = {}


def _testnet_client(market):
    if market not in _TESTNET_CACHE:
        _TESTNET_CACHE[market] = TestnetClient(market)
    return _TESTNET_CACHE[market]


app = FastAPI(title="Alpha 因子实验室")


# ---- API ----
@app.get("/alpha/api/operators")
def api_operators():
    ops = [{"name": k, **v} for k, v in sorted(OPERATORS.items())]
    return {"operators": ops, "fields": FIELDS}


@app.get("/alpha/api/datafiles")
def api_datafiles():
    return {"files": available_data_files(BASE_DIR)}


def _fetch_json(url, timeout=6):
    """代理拉取现货(8080)/合约(8081)的 /api/data, 失败返回 None (服务离线)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _pick_info(data):
    """从实时服务 /api/data 中挑选信息页所需字段 (丢弃 300 根 K线等大字段)"""
    if not data:
        return None
    return {
        "price": data.get("last_price"),
        "last_ts": data.get("last_ts"),
        "indicator_state": data.get("indicator_state"),
        "signal_readiness": data.get("signal_readiness"),
        "position": data.get("position"),
        "balance": data.get("balance"),
        "peak_balance": data.get("peak_balance"),
        "initial_capital": data.get("initial_capital"),
        "return_pct": data.get("return_pct"),
        "drawdown_pct": data.get("drawdown_pct"),
        "trade_history": data.get("trade_history"),
        "flow_stats": data.get("flow_stats"),
        "large_orders": data.get("large_orders"),
        "long_short_ratio": data.get("long_short_ratio"),
        "params": data.get("params"),
    }


@app.get("/alpha/api/info")
def api_info():
    """信息聚合页数据: 现货(8080) + 合约(8081) 信号/资金流向一屏汇总"""
    return {
        "spot": _pick_info(_fetch_json("http://127.0.0.1:8080/api/data")),
        "futures": _pick_info(_fetch_json("http://127.0.0.1:8081/api/data")),
        "server_time": time.strftime("%H:%M:%S"),
    }


@app.get("/alpha/api/reports")
def api_reports():
    """扫描 reports/ 目录, 收集全部回测 HTML 报告 (排除 charts 图片目录)"""
    root = os.path.join(BASE_DIR, "reports")
    reports = []
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.basename(dirpath) == "charts":
                continue
            for fn in filenames:
                if fn.endswith(".html"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
                    reports.append(rel)
    reports.sort()
    return {"reports": reports, "root": "reports"}


@app.get("/alpha/api/testnet")
def api_testnet_status():
    """Testnet 账户状态: 现货 + 合约 的 Key 就绪状态/价格/余额/持仓"""
    out = {}
    for m in ("spot", "futures"):
        c = _testnet_client(m)
        item = {"market": m, "ready": c.ready, "price": None, "balance": [], "positions": [], "error": None}
        try:
            # 价格走公开接口, 无需 Key, 用于验证网络连通
            item["price"] = c.get_price()
        except Exception as e:
            item["error"] = str(e)
        if c.ready:
            try:
                item["balance"] = c.get_balance()
                item["positions"] = c.get_position()
            except Exception as e:
                item["error"] = str(e)
        out[m] = item
    return {"server_time": time.strftime("%H:%M:%S"), "markets": out}


@app.post("/alpha/api/testnet/order")
def api_testnet_order(req: TestnetOrderRequest):
    """Testnet 真实下单: body = {market, side, order_type, qty, price?, leverage?, reduce_only?}"""
    if req.market not in ("spot", "futures"):
        return JSONResponse({"ok": False, "message": "market 必须为 spot 或 futures"}, status_code=400)
    if req.side not in ("buy", "sell"):
        return JSONResponse({"ok": False, "message": "side 必须为 buy 或 sell"}, status_code=400)
    if req.order_type not in ("market", "limit"):
        return JSONResponse({"ok": False, "message": "order_type 必须为 market 或 limit"}, status_code=400)
    c = _testnet_client(req.market)
    if not c.ready:
        return JSONResponse(
            {"ok": False, "message": f"{req.market} 未配置 Testnet Key, 请填写 config_testnet.json"},
            status_code=400)
    try:
        if req.market == "futures" and req.leverage:
            c.set_leverage(req.leverage)
        r = c.place_order(req.side, req.order_type, req.qty,
                          price=req.price, reduce_only=req.reduce_only)
        return {"ok": True, "order": r}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


@app.post("/alpha/api/testnet/close")
def api_testnet_close(req: TestnetCloseRequest):
    """一键平仓: 合约对所有非零持仓市价反向 reduceOnly; 现货市价卖出全部 ETH"""
    if req.market not in ("spot", "futures"):
        return JSONResponse({"ok": False, "message": "market 必须为 spot 或 futures"}, status_code=400)
    c = _testnet_client(req.market)
    if not c.ready:
        return JSONResponse(
            {"ok": False, "message": f"{req.market} 未配置 Testnet Key, 请填写 config_testnet.json"},
            status_code=400)
    try:
        closes = []
        if req.market == "futures":
            for p in c.get_position():
                side = "SELL" if p["positionAmt"] > 0 else "BUY"
                r = c.place_order(side, "market", abs(p["positionAmt"]), reduce_only=True)
                closes.append({"symbol": p["symbol"], "side": side,
                               "qty": abs(p["positionAmt"]), "orderId": r.get("orderId")})
        else:
            for p in c.get_position():      # 现货持仓 = ETH 余额
                r = c.place_order("SELL", "market", p["balance"])
                closes.append({"symbol": "ETH", "side": "SELL",
                               "qty": p["balance"], "orderId": r.get("orderId")})
        if not closes:
            return {"ok": True, "closes": [], "message": "当前无持仓, 无需平仓"}
        return {"ok": True, "closes": closes}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


@app.post("/alpha/api/backtest")
def api_backtest(req: BacktestRequest):
    try:
        files = available_data_files(BASE_DIR)
        match = next(
            (f for f in files if f["name"] == req.file and f["market"] == req.market), None)
        if match is None:
            return JSONResponse({"error": f"数据文件不存在: {req.market}/{req.file}"}, status_code=404)
        df, data = _get_data(match["path"])
        result = run_backtest(
            req.expr, data, df,
            z_window=req.z_window, threshold=req.threshold,
            deadzone=req.deadzone, fee_rate=req.fee_rate,
            leverage=req.leverage, ppy=req.ppy,
        )
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"回测异常: {e}"}, status_code=500)


@app.get("/alpha/api/factors")
def api_list_factors():
    return {"factors": storage.list_factors(BASE_DIR)}


@app.post("/alpha/api/factors")
def api_save_factor(req: SaveFactorRequest):
    if not req.name.strip():
        return JSONResponse({"error": "因子名称不能为空"}, status_code=400)
    item = storage.save_factor(BASE_DIR, req.name.strip(), req.expr, req.params, req.metrics)
    return {"ok": True, "factor": item}


@app.delete("/alpha/api/factors/{name}")
def api_delete_factor(name: str):
    storage.delete_factor(BASE_DIR, name)
    return {"ok": True}


@app.get("/alpha/api/factors/{name}")
def api_get_factor(name: str):
    item = storage.get_factor(BASE_DIR, name)
    if item is None:
        return JSONResponse({"error": "因子不存在"}, status_code=404)
    return item


# ---- 页面 ----
HTML_ALPHA = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🧪 Alpha 因子实验室</title>
<script src="/static/echarts.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; }
.header { display: flex; align-items: center; gap: 14px; padding: 10px 20px; background: #161b22; border-bottom: 1px solid #30363d; height: 50px; }
.header h1 { font-size: 16px; font-weight: 600; color: #f0f6fc; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: #3fb950; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.nav { display: flex; gap: 4px; }
.nav a { padding: 5px 12px; color: #8b949e; text-decoration: none; border-radius: 4px; font-size: 13px; }
.nav a:hover { background: #21262d; color: #f0f6fc; }
.nav a.active { background: #1f6feb; color: #fff; }
.layout { display: flex; gap: 16px; padding: 16px 20px; align-items: flex-start; }
.left { width: 430px; flex-shrink: 0; }
.right { flex: 1; min-width: 0; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
.card h3 { font-size: 13px; color: #8b949e; margin-bottom: 10px; border-bottom: 1px solid #30363d; padding-bottom: 6px; }
textarea.expr { width: 100%; height: 96px; background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 10px; font-family: "Consolas", "Courier New", monospace; font-size: 13px; resize: vertical; }
textarea.expr:focus { outline: none; border-color: #58a6ff; }
.hint { font-size: 12px; color: #8b949e; margin: 6px 0; line-height: 1.6; }
.hint code { background: #21262d; padding: 1px 5px; border-radius: 3px; color: #58a6ff; }
.param-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.param-item label { display: block; font-size: 11px; color: #8b949e; margin-bottom: 3px; }
.param-item input, .param-item select { width: 100%; background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 4px; padding: 5px 8px; font-size: 12px; }
.param-item input:focus, .param-item select:focus { outline: none; border-color: #58a6ff; }
.btn { background: #1f6feb; color: #fff; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; font-weight: 600; cursor: pointer; width: 100%; }
.btn:hover { background: #388bfd; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-save { background: #238636; margin-top: 8px; }
.btn-save:hover { background: #2ea043; }
.err-box { background: #3a1a1a; color: #f85149; border: 1px solid #f85149; border-radius: 6px; padding: 8px 10px; font-size: 12px; margin-top: 10px; display: none; }
.metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }
.metric { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px; text-align: center; }
.metric .m-label { font-size: 11px; color: #8b949e; }
.metric .m-val { font-size: 20px; font-weight: 700; margin-top: 4px; }
.metric .m-val.pos { color: #3fb950; }
.metric .m-val.neg { color: #f85149; }
.metric .m-val.neutral { color: #58a6ff; }
.chart { width: 100%; height: 300px; }
.chart-sm { width: 100%; height: 220px; }
.factor-list { max-height: 320px; overflow-y: auto; }
.factor-row { display: flex; align-items: center; gap: 8px; padding: 7px 6px; margin: 4px 0; border-radius: 5px; background: #0d1117; border: 1px solid #30363d; font-size: 12px; cursor: pointer; }
.factor-row:hover { border-color: #58a6ff; }
.factor-row.selected { border-color: #f0883e; background: #1a1a2e; }
.factor-row .f-name { font-weight: 600; color: #58a6ff; width: 110px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.factor-row .f-expr { flex: 1; color: #8b949e; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.factor-row .f-sharpe { width: 60px; text-align: right; font-weight: 700; }
.factor-row .f-ret { width: 60px; text-align: right; }
.factor-row .f-actions { display: flex; gap: 4px; }
.factor-row button { background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 4px; padding: 2px 7px; font-size: 11px; cursor: pointer; }
.factor-row button:hover { color: #f0f6fc; border-color: #58a6ff; }
.factor-row button.del:hover { color: #f85149; border-color: #f85149; }
.tabs { display: flex; gap: 4px; margin-bottom: 10px; }
.tabs button { background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 5px; padding: 5px 12px; font-size: 12px; cursor: pointer; }
.tabs button.active { background: #1f6feb; color: #fff; }
#chart-title { font-size: 12px; color: #8b949e; margin: 6px 0; }
.loading { color: #8b949e; font-size: 12px; text-align: center; padding: 20px; }
/* ---- 统一侧栏 ---- */
.app-shell{display:flex;min-height:100vh}
.app-sidebar{width:190px;flex-shrink:0;background:#161b22;border-right:2px solid #30363d;display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;height:100vh;overflow-y:auto}
.app-sidebar .logo{font-size:15px;font-weight:700;color:#58a6ff;padding:0 18px 14px;border-bottom:1px solid #30363d;white-space:nowrap}
.app-sidebar .nav-group{margin-top:14px}
.app-sidebar .group-title{font-size:11px;color:#8b949e;padding:0 18px;margin-bottom:4px;letter-spacing:.5px}
.app-sidebar a{display:block;padding:8px 18px;color:#8b949e;text-decoration:none;font-size:13px;border-left:3px solid transparent;white-space:nowrap}
.app-sidebar a:hover{color:#f0f6fc;background:#21262d}
.app-sidebar a.active{color:#58a6ff;background:#1f6feb22;border-left-color:#1f6feb}
.app-main{flex:1;min-width:0}
</style>
</head>
<body>
<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha" class="active">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports">📊 策略及回测</a>
      <a href="http://127.0.0.1:8082/testnet">🔌 Testnet</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1><span class="live-dot"></span>🧪 Alpha 因子实验室 — 世坤风格因子回测</h1>
    </div>

<div class="layout">
  <div class="left">
    <!-- 表达式编辑器 -->
    <div class="card">
      <h3>📝 因子表达式</h3>
      <textarea class="expr" id="expr-input" spellcheck="false">rank(ts_delta(close, 5)) - rank(ts_delta(close, 10))</textarea>
      <div class="hint">
        字段: <code>open</code> <code>high</code> <code>low</code> <code>close</code> <code>volume</code> <code>returns</code><br>
        示例: <code>rank(ts_delta(close,5)) - rank(ts_delta(close,10))</code><br>
        <code>zscore(ts_delta(close,8), 60)</code> · <code>-1*ts_corr(rank(close),rank(volume),10)</code>
      </div>
    </div>

    <!-- 回测参数 -->
    <div class="card">
      <h3>⚙️ 回测参数</h3>
      <div class="param-grid">
        <div class="param-item"><label>数据文件</label><select id="p-file"></select></div>
        <div class="param-item"><label>频率</label><select id="p-ppy">
          <option value="1h">1小时</option>
          <option value="1d">1天</option>
          <option value="4h">4小时</option>
        </select></div>
        <div class="param-item"><label>z-score 窗口</label><input id="p-zwin" type="number" value="60"></div>
        <div class="param-item"><label>仓位阈值</label><input id="p-thr" type="number" step="0.1" value="1.0"></div>
        <div class="param-item"><label>死区(空仓区)</label><input id="p-dz" type="number" step="0.1" value="0.3"></div>
        <div class="param-item"><label>手续费率</label><input id="p-fee" type="number" step="0.0001" value="0.0005"></div>
        <div class="param-item"><label>杠杆</label><input id="p-lev" type="number" step="0.5" value="1.0"></div>
      </div>
      <button class="btn" id="btn-run" onclick="runBacktest()">▶ 运行回测</button>
      <div class="err-box" id="err-box"></div>
    </div>

    <!-- 因子库 -->
    <div class="card">
      <h3>📚 因子库</h3>
      <div class="factor-list" id="factor-list"><div class="loading">加载中...</div></div>
      <button class="btn btn-save" onclick="saveFactor()">💾 保存当前因子</button>
    </div>
  </div>

  <div class="right">
    <!-- 指标 -->
    <div class="card">
      <h3>📊 回测指标</h3>
      <div class="metrics" id="metrics"><div class="loading">运行回测后展示</div></div>
    </div>

    <!-- 图表 -->
    <div class="card">
      <div class="tabs">
        <button class="active" onclick="showTab('eq', this)">净值曲线</button>
        <button onclick="showTab('factor', this)">因子值</button>
        <button onclick="showTab('pos', this)">持仓</button>
      </div>
      <div id="chart-title"></div>
      <div id="chart-eq" class="chart"></div>
      <div id="chart-factor" class="chart-sm" style="display:none"></div>
      <div id="chart-pos" class="chart-sm" style="display:none"></div>
    </div>
  </div>
</div>
  </div>
</div>

<script>
let eqChart, factorChart, posChart;
let factors = [];
let lastResult = null;

function fmtPct(v) { return (v>=0?'+':'') + v.toFixed(2) + '%'; }

function fmtTime(ts) {
  const d = new Date(ts);
  return d.toLocaleDateString('zh-CN', {month:'short',day:'numeric'}) + ' ' +
         d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
}

function loadDataFiles() {
  fetch('/alpha/api/datafiles').then(r=>r.json()).then(d => {
    const sel = document.getElementById('p-file');
    sel.innerHTML = '';
    d.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.name;
      opt.textContent = '[' + f.market + '] ' + f.name;
      opt.dataset.market = f.market;
      sel.appendChild(opt);
    });
  });
}

async function runBacktest() {
  const expr = document.getElementById('expr-input').value.trim();
  const sel = document.getElementById('p-file');
  const file = sel.value;
  const market = sel.selectedOptions[0]?.dataset.market || '现货';
  const errBox = document.getElementById('err-box');
  errBox.style.display = 'none';
  const btn = document.getElementById('btn-run');
  btn.disabled = true; btn.textContent = '⏳ 回测中...';
  try {
    const resp = await fetch('/alpha/api/backtest', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        expr: expr, file: file, market: market,
        z_window: +document.getElementById('p-zwin').value,
        threshold: +document.getElementById('p-thr').value,
        deadzone: +document.getElementById('p-dz').value,
        fee_rate: +document.getElementById('p-fee').value,
        leverage: +document.getElementById('p-lev').value,
        ppy: document.getElementById('p-ppy').value,
      })
    });
    const data = await resp.json();
    if (data.error) { errBox.textContent = '❌ ' + data.error; errBox.style.display = 'block'; return; }
    lastResult = data;
    renderMetrics(data.metrics);
    renderCharts(data);
  } catch (e) {
    errBox.textContent = '❌ 请求失败: ' + e.message; errBox.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = '▶ 运行回测';
  }
}

function renderMetrics(m) {
  const items = [
    ['年化收益', fmtPct(m.annual_return), m.annual_return>=0?'pos':'neg'],
    ['累计收益', fmtPct(m.total_return), m.total_return>=0?'pos':'neg'],
    ['夏普比率', m.sharpe, m.sharpe>=0?'pos':'neg'],
    ['最大回撤', m.max_drawdown+'%', 'neg'],
    ['胜率', m.win_rate+'%', 'neutral'],
    ['换手率', m.turnover, 'neutral'],
    ['IC (Pearson)', m.ic, m.ic>=0?'pos':'neg'],
    ['IC (Spearman)', m.ic_spearman, m.ic_spearman>=0?'pos':'neg'],
    ['Fitness', m.fitness, m.fitness>=0?'pos':'neg'],
    ['K线数', m.periods, 'neutral'],
  ];
  document.getElementById('metrics').innerHTML = items.map(it =>
    `<div class="metric"><div class="m-label">${it[0]}</div><div class="m-val ${it[2]}">${it[1]}</div></div>`).join('');
}

function renderCharts(data) {
  const times = data.series.times.map(fmtTime);
  // 净值
  const eqOpt = {
    grid:{left:'5%',right:'3%',top:'5%',bottom:'12%'},
    tooltip:{trigger:'axis', formatter: p => p[0].name + '<br>' + (p[0].value*100).toFixed(2) + '%'},
    xAxis:{type:'category',data:times,axisLabel:{fontSize:10}},
    yAxis:{type:'value',name:'净值',axisLabel:{formatter: v => (v*100).toFixed(0)+'%'}},
    series:[{name:'策略净值',type:'line',data:data.series.equity,symbol:'none',lineStyle:{color:'#58a6ff',width:1.5}}],
    dataZoom:[{type:'inside',start:40,end:100}],
  };
  // 因子
  const fOpt = {
    grid:{left:'5%',right:'3%',top:'5%',bottom:'12%'},
    tooltip:{trigger:'axis'},
    xAxis:{type:'category',data:times,axisLabel:{fontSize:10}},
    yAxis:{type:'value',name:'因子值'},
    series:[{name:'因子',type:'line',data:data.series.factor,symbol:'none',lineStyle:{color:'#f0883e',width:1}}],
    dataZoom:[{type:'inside',start:40,end:100}],
  };
  // 持仓
  const posOpt = {
    grid:{left:'5%',right:'3%',top:'5%',bottom:'12%'},
    tooltip:{trigger:'axis'},
    xAxis:{type:'category',data:times,axisLabel:{fontSize:10}},
    yAxis:{type:'value',name:'仓位',min:-1.2,max:1.2},
    series:[{name:'持仓',type:'line',data:data.series.position,symbol:'none',lineStyle:{color:'#3fb950',width:1.5},areaStyle:{color:'rgba(63,185,80,0.15)'}}],
    dataZoom:[{type:'inside',start:40,end:100}],
  };
  if (!eqChart) {
    eqChart = echarts.init(document.getElementById('chart-eq'));
    factorChart = echarts.init(document.getElementById('chart-factor'));
    posChart = echarts.init(document.getElementById('chart-pos'));
    window.onresize = () => { eqChart.resize(); factorChart.resize(); posChart.resize(); };
  }
  eqChart.setOption(eqOpt); factorChart.setOption(fOpt); posChart.setOption(posOpt);
  document.getElementById('chart-title').textContent =
    '共 ' + data.metrics.periods + ' 根K线 · 因子: ' + document.getElementById('expr-input').value;
}

function showTab(tab, btn) {
  document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  ['eq','factor','pos'].forEach(t => {
    document.getElementById('chart-' + t).style.display = t===tab ? 'block' : 'none';
  });
  if (tab==='eq') eqChart?.resize(); else if (tab==='factor') factorChart?.resize(); else posChart?.resize();
}

async function loadFactors() {
  const div = document.getElementById('factor-list');
  try {
    const d = await (await fetch('/alpha/api/factors')).json();
    factors = d.factors || [];
    renderFactors();
  } catch (e) { div.innerHTML = '<div class="loading">加载失败</div>'; }
}

function renderFactors() {
  const div = document.getElementById('factor-list');
  if (!factors.length) { div.innerHTML = '<div class="loading">暂无保存的因子</div>'; return; }
  div.innerHTML = factors.slice().reverse().map(f => `
    <div class="factor-row" onclick="loadFactor('${f.name.replace(/'/g, "\\'")}')">
      <span class="f-name">${f.name}</span>
      <span class="f-expr">${f.expr}</span>
      <span class="f-sharpe ${(f.metrics?.sharpe||0)>=0?'pos':'neg'}">Sharpe ${f.metrics?.sharpe??'-'}</span>
      <span class="f-ret ${(f.metrics?.annual_return||0)>=0?'pos':'neg'}">${f.metrics?fmtPct(f.metrics.annual_return):''}</span>
      <span class="f-actions"><button class="del" onclick="event.stopPropagation();delFactor('${f.name.replace(/'/g, "\\'")}')">删除</button></span>
    </div>`).join('');
}

async function loadFactor(name) {
  const f = factors.find(x => x.name === name);
  if (!f) return;
  document.getElementById('expr-input').value = f.expr;
  if (f.params) {
      if (f.params.z_window) document.getElementById('p-zwin').value = f.params.z_window;
      if (f.params.threshold) document.getElementById('p-thr').value = f.params.threshold;
      if (f.params.deadzone) document.getElementById('p-dz').value = f.params.deadzone;
      if (f.params.fee_rate) document.getElementById('p-fee').value = f.params.fee_rate;
      if (f.params.leverage) document.getElementById('p-lev').value = f.params.leverage;
      if (f.params.ppy) document.getElementById('p-ppy').value = f.params.ppy;
    }
  await runBacktest();
}

async function saveFactor() {
  if (!lastResult) { alert('请先运行回测再保存'); return; }
  const name = prompt('因子名称:', 'alpha_' + Date.now().toString().slice(-6));
  if (!name) return;
  const params = {
    z_window: +document.getElementById('p-zwin').value,
    threshold: +document.getElementById('p-thr').value,
    deadzone: +document.getElementById('p-dz').value,
    fee_rate: +document.getElementById('p-fee').value,
    leverage: +document.getElementById('p-lev').value,
    ppy: document.getElementById('p-ppy').value,
    file: document.getElementById('p-file').value,
  };
  const resp = await fetch('/alpha/api/factors', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, expr: document.getElementById('expr-input').value.trim(), params, metrics: lastResult.metrics})
  });
  const d = await resp.json();
  if (d.error) { alert('保存失败: ' + d.error); return; }
  loadFactors();
}

async function delFactor(name) {
  if (!confirm('删除因子 ' + name + '?')) return;
  await fetch('/alpha/api/factors/' + encodeURIComponent(name), {method:'DELETE'});
  loadFactors();
}

window.onload = () => { loadDataFiles(); loadFactors(); };
</script>
</body>
</html>"""


# ---- 信息聚合页 (现货/合约信号 + 资金流向一屏汇总) ----
HTML_ALPHA_INFO = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🏠 信息 — 现货 / 合约策略信号</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; }
.app-shell{display:flex;min-height:100vh}
.app-sidebar{width:190px;flex-shrink:0;background:#161b22;border-right:2px solid #30363d;display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;height:100vh;overflow-y:auto}
.app-sidebar .logo{font-size:15px;font-weight:700;color:#58a6ff;padding:0 18px 14px;border-bottom:1px solid #30363d;white-space:nowrap}
.app-sidebar .nav-group{margin-top:14px}
.app-sidebar .group-title{font-size:11px;color:#8b949e;padding:0 18px;margin-bottom:4px;letter-spacing:.5px}
.app-sidebar a{display:block;padding:8px 18px;color:#8b949e;text-decoration:none;font-size:13px;border-left:3px solid transparent;white-space:nowrap}
.app-sidebar a:hover{color:#f0f6fc;background:#21262d}
.app-sidebar a.active{color:#58a6ff;background:#1f6feb22;border-left-color:#1f6feb}
.app-main{flex:1;min-width:0;padding:0 0 20px}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;height:52px}
.header h1{font-size:16px;font-weight:600;color:#f0f6fc}
.header .sub{font-size:12px;color:#8b949e}
.container{padding:16px 20px}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media (max-width:1200px){.info-grid{grid-template-columns:1fr}}
.info-card{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.info-card .card-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #30363d}
.info-card .card-head .m-title{font-size:14px;font-weight:600;color:#f0f6fc}
.info-card .card-head .m-price{font-size:20px;font-weight:700;color:#58a6ff}
.info-card .card-body{padding:12px 14px}
.info-card.offline .card-body{color:#6e7681;text-align:center;padding:28px 14px}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600}
.badge.long{background:#1a3a1a;color:#3fb950;border:1px solid #3fb950}
.badge.short{background:#3a1a1a;color:#f85149;border:1px solid #f85149}
.badge.wait{background:#1a1a2e;color:#8b949e;border:1px solid #30363d}
.sect{margin-top:12px}
.sect:first-child{margin-top:0}
.sect-title{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;border-bottom:1px solid #21262d;padding-bottom:4px}
.row{display:flex;justify-content:space-between;padding:3px 0;font-size:12.5px}
.row .k{color:#8b949e}
.row .v{font-weight:600}
.pos-card{border-radius:5px;padding:8px 10px;font-size:12.5px;margin-top:4px}
.pos-card.long{background:#0d2818;border:1px solid #3fb950}
.pos-card.short{background:#2a0d0d;border:1px solid #f85149}
.pos-card.empty{background:#1a1a2e;border:1px solid #30363d;color:#8b949e;text-align:center;padding:12px}
.flow-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #21262d;font-size:12.5px}
.flow-row .net.pos{color:#3fb950}.flow-row .net.neg{color:#f85149}
.order-line{display:flex;justify-content:space-between;padding:3px 0;font-size:12px;color:#c9d1d9;border-bottom:1px solid #1c2128}
.order-line .side.buy{color:#3fb950;font-weight:600}.order-line .side.sell{color:#f85149;font-weight:600}
.trade-line{display:flex;justify-content:space-between;padding:3px 0;font-size:12px;color:#c9d1d9;border-bottom:1px solid #1c2128}
.trade-line .pnl.pos{color:#3fb950}.trade-line .pnl.neg{color:#f85149}
.pnl-positive{color:#3fb950}.pnl-negative{color:#f85149}
.small{font-size:11px;color:#6e7681}
a.jump{color:#58a6ff;text-decoration:none;font-size:12px}
a.jump:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info" class="active">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports">📊 策略及回测</a>
      <a href="http://127.0.0.1:8082/testnet">🔌 Testnet</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1>🏠 信息 — 现货 / 合约策略信号与资金流向</h1>
      <span class="sub" id="refresh-time">--</span>
    </div>
    <div class="container">
      <div class="info-grid">
        <div class="info-card" id="card-spot"><div class="card-body">加载中...</div></div>
        <div class="info-card" id="card-futures"><div class="card-body">加载中...</div></div>
      </div>
    </div>
  </div>
</div>

<script>
const REFRESH_MS = 8000;

function fmtPrice(p){ return p ? '$' + Number(p).toFixed(2) : '--'; }
function fmtUsdt(v){ return (v>=0?'+':'') + Number(v).toFixed(2) + 'U'; }

function timeShort(ts){
  if (!ts) return '--';
  const d = new Date(ts);
  return d.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}

function badgeHTML(rdy){
  if (!rdy) return '<span class="badge wait">--</span>';
  if (rdy.long_ready) return '<span class="badge long">🟢 做多就绪</span>';
  if (rdy.short_ready) return '<span class="badge short">🔴 做空就绪</span>';
  return '<span class="badge wait">⚪ 观望</span>';
}

function posHTML(pos, lev){
  if (!pos) return '<div class="pos-card empty">💤 空仓等待</div>';
  const isLong = pos.direction === 'long';
  const cls = isLong ? 'long' : 'short';
  const dir = isLong ? '🟢 做多' : '🔴 做空';
  const up = pos.unrealized_pnl;
  const pnlCls = (up == null) ? '' : (up >= 0 ? 'pnl-positive' : 'pnl-negative');
  const pnlStr = (up == null) ? '--' : fmtUsdt(up) + ' (' + (pos.unrealized_pct>=0?'+':'') + pos.unrealized_pct + '%)';
  return '<div class="pos-card ' + cls + '">' +
    '<div class="row"><span class="k">方向</span><span class="v">' + dir + '</span></div>' +
    '<div class="row"><span class="k">开仓 / 现价</span><span class="v">' + pos.entry_price + ' / ' + (pos.current_price || '--') + '</span></div>' +
    '<div class="row"><span class="k">浮盈</span><span class="v ' + pnlCls + '">' + pnlStr + '</span></div>' +
    '<div class="row"><span class="k">仓位(名义)</span><span class="v">' + (pos.size_usdt != null ? pos.size_usdt.toFixed(1) + 'U' : '--') + (lev ? ' @ ' + lev + 'x' : '') + '</span></div>' +
    '<div class="row"><span class="k">止损 / 持仓</span><span class="v">' + (pos.sl_price || '--') + ' / ' + (pos.held_bars != null ? pos.held_bars : '--') + '根</span></div>' +
    '</div>';
}

function flowHTML(stats){
  if (!stats || !stats.length) return '<div class="small">暂无大单数据</div>';
  return stats.map(s => {
    const cls = s.net >= 0 ? 'pos' : 'neg';
    const sign = s.net >= 0 ? '+' : '';
    return '<div class="flow-row"><span>' + s.window + '分钟净买卖</span>' +
      '<span class="net ' + cls + '">' + sign + (s.net/10000).toFixed(1) + '万U (买' + (s.buy/10000).toFixed(1) + ' 卖' + (s.sell/10000).toFixed(1) + ')</span></div>';
  }).join('');
}

function ordersHTML(orders){
  if (!orders || !orders.length) return '<div class="small">暂无大单 (≥10万U)</div>';
  return orders.slice().reverse().slice(0,5).map(o => {
    const dt = new Date(o.ts);
    const t = dt.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return '<div class="order-line"><span class="side ' + o.side + '">' + (o.side==='buy'?'🟢买':'🔴卖') + '</span>' +
      '<span>' + (o.usdt/10000).toFixed(1) + '万U</span><span class="small">$' + o.price + '</span><span class="small">' + t + '</span></div>';
  }).join('');
}

function lsrHTML(r){
  if (!r) return '';
  return '<div class="row"><span class="k">大户多空比</span><span class="v">' + r.top_ratio + ' (多' + (r.top_long*100).toFixed(0) + '%/空' + (r.top_short*100).toFixed(0) + '%)</span></div>' +
    '<div class="row"><span class="k">账户多空比</span><span class="v">' + r.acct_ratio + ' (多' + (r.acct_long*100).toFixed(0) + '%/空' + (r.acct_short*100).toFixed(0) + '%)</span></div>';
}

function tradesHTML(list){
  if (!list || !list.length) return '<div class="small">暂无交易</div>';
  const reasonMap = {momentum_death:'动量', SL:'止损', TP:'止盈', timeout:'超时', force_close:'强平'};
  return list.slice().reverse().slice(0,3).map(t => {
    const cls = t.pnl >= 0 ? 'pos' : 'neg';
    const dir = t.direction === 'long' ? '多' : '空';
    return '<div class="trade-line"><span>' + dir + ' ' + t.entry_price + '→' + t.exit_price + '</span>' +
      '<span class="pnl ' + cls + '">' + fmtUsdt(t.pnl) + '</span><span class="small">' + (reasonMap[t.reason]||t.reason) + '</span></div>';
  }).join('');
}

function renderCard(el, d, title, homeUrl){
  if (!d) {
    el.className = 'info-card offline';
    el.innerHTML = '<div class="card-head"><span class="m-title">' + title + '</span></div>' +
      '<div class="card-body">🔌 服务未启动<br><a class="jump" href="' + homeUrl + '">' + homeUrl + '</a></div>';
    return;
  }
  el.className = 'info-card';
  const st = d.indicator_state || {};
  const rdy = d.signal_readiness || {};
  const prm = d.params || {};
  el.innerHTML =
    '<div class="card-head"><span class="m-title">' + title + '</span><span class="m-price">' + fmtPrice(d.price) + '</span></div>' +
    '<div class="card-body">' +
      '<div class="row"><span class="k">信号状态</span><span class="v">' + badgeHTML(rdy) + '</span></div>' +
      '<div class="row"><span class="k">K线时间</span><span class="v">' + timeShort(d.last_ts) + '</span></div>' +
      '<div class="sect"><div class="sect-title">📊 指标</div>' +
        '<div class="row"><span class="k">ROC(8) / ROC(20) / ROC(50)</span><span class="v">' + st.roc5 + ' / ' + st.roc20 + ' / ' + st.roc50 + '</span></div>' +
        '<div class="row"><span class="k">量比 / ATR</span><span class="v">' + (st.vol_ratio||0) + 'x / ' + st.atr + '</span></div></div>' +
      '<div class="sect"><div class="sect-title">💰 账户</div>' +
        '<div class="row"><span class="k">余额 / 收益率</span><span class="v">' + d.balance + 'U / <span class="' + (d.return_pct>=0?'pnl-positive':'pnl-negative') + '">' + (d.return_pct>=0?'+':'') + d.return_pct + '%</span></span></div>' +
        '<div class="row"><span class="k">峰值 / 回撤</span><span class="v">' + d.peak_balance + 'U / ' + d.drawdown_pct + '%</span></div>' +
        '<div class="row"><span class="k">杠杆 / 本金</span><span class="v">' + (prm.leverage ? prm.leverage + 'x' : '--') + ' / ' + (d.initial_capital != null ? d.initial_capital + 'U' : '--') + '</span></div>' +
        (prm.fraction_base ? '<div class="row"><span class="k">仓位比例</span><span class="v">' + (prm.fraction_base*100) + '%</span></div>' : '') +
        '</div>' +
      '<div class="sect"><div class="sect-title">📂 持仓</div>' + posHTML(d.position, prm.leverage) + '</div>' +
      '<div class="sect"><div class="sect-title">🐋 资金流向 (大单净买卖)</div>' + flowHTML(d.flow_stats) + '</div>' +
      '<div class="sect"><div class="sect-title">📋 大单滚动</div>' + ordersHTML(d.large_orders) + '</div>' +
      (d.long_short_ratio ? '<div class="sect"><div class="sect-title">👥 多空比</div>' + lsrHTML(d.long_short_ratio) + '</div>' : '') +
      '<div class="sect"><div class="sect-title">📜 最近交易</div>' + tradesHTML(d.trade_history) + '</div>' +
      '<div style="margin-top:10px;text-align:right"><a class="jump" href="' + homeUrl + '" target="_blank">打开完整面板 →</a></div>' +
    '</div>';
}

async function refresh(){
  try {
    const r = await (await fetch('/alpha/api/info')).json();
    renderCard(document.getElementById('card-spot'), r.spot, '现货 · 8080', 'http://127.0.0.1:8080/');
    renderCard(document.getElementById('card-futures'), r.futures, '合约 · 8081', 'http://127.0.0.1:8081/');
    document.getElementById('refresh-time').textContent = '🔄 刷新于 ' + r.server_time;
  } catch(e) {
    document.getElementById('refresh-time').textContent = '⚠️ 数据加载失败';
  }
}

window.onload = () => { refresh(); setInterval(refresh, REFRESH_MS); };
</script>
</body>
</html>"""


# ---- 策略及回测结果页 ----
HTML_ALPHA_REPORTS = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📊 策略及回测结果</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; }
.app-shell{display:flex;min-height:100vh}
.app-sidebar{width:190px;flex-shrink:0;background:#161b22;border-right:2px solid #30363d;display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;height:100vh;overflow-y:auto}
.app-sidebar .logo{font-size:15px;font-weight:700;color:#58a6ff;padding:0 18px 14px;border-bottom:1px solid #30363d;white-space:nowrap}
.app-sidebar .nav-group{margin-top:14px}
.app-sidebar .group-title{font-size:11px;color:#8b949e;padding:0 18px;margin-bottom:4px;letter-spacing:.5px}
.app-sidebar a{display:block;padding:8px 18px;color:#8b949e;text-decoration:none;font-size:13px;border-left:3px solid transparent;white-space:nowrap}
.app-sidebar a:hover{color:#f0f6fc;background:#21262d}
.app-sidebar a.active{color:#58a6ff;background:#1f6feb22;border-left-color:#1f6feb}
.app-main{flex:1;min-width:0;padding:0 0 20px}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;height:52px}
.header h1{font-size:16px;font-weight:600;color:#f0f6fc}
.header .sub{font-size:12px;color:#8b949e}
.container{padding:16px 20px;max-width:1100px}
.group-card{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:14px;overflow:hidden}
.group-card .g-head{padding:10px 14px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between}
.group-card .g-title{font-size:14px;font-weight:600;color:#58a6ff}
.group-card .g-count{font-size:12px;color:#8b949e}
.report-row{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid #1c2128;font-size:13px}
.report-row:last-child{border-bottom:none}
.report-row .r-name{flex:1;color:#c9d1d9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.report-row .r-time{color:#6e7681;font-size:12px;width:150px;text-align:right}
.report-row a.open{color:#58a6ff;text-decoration:none;font-size:12px;border:1px solid #30363d;padding:3px 10px;border-radius:4px}
.report-row a.open:hover{background:#1f6feb22;border-color:#58a6ff}
.empty{color:#8b949e;text-align:center;padding:30px}
</style>
</head>
<body>
<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports" class="active">📊 策略及回测</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1>📊 策略及回测结果</h1>
      <span class="sub" id="stat-line">--</span>
    </div>
    <div class="container" id="report-list"><div class="empty">加载中...</div></div>
  </div>
</div>

<script>
function describe(path){
  const market = path.includes('contract') ? '合约' : '现货';
  const iv = path.includes('5m') ? '5m' : '1h';
  let param = 'ROC 8/20/50 (原参数)';
  if (path.includes('roc40-100-250')) param = 'ROC 40/100/250 (放大参数)';
  return market + ' · ' + iv + ' · ' + param;
}

function tsFromName(fn){
  const m = fn.match(/_(\d{8})_(\d{6})/);
  if (!m) return '--';
  return m[1].slice(0,4)+'-'+m[1].slice(4,6)+'-'+m[1].slice(6,8)+' '+m[2].slice(0,2)+':'+m[2].slice(2,4)+':'+m[2].slice(4,6);
}

async function load(){
  try {
    const r = await (await fetch('/alpha/api/reports')).json();
    const list = r.reports || [];
    document.getElementById('stat-line').textContent = '共 ' + list.length + ' 份回测报告';
    if (!list.length) { document.getElementById('report-list').innerHTML = '<div class="empty">暂无回测报告</div>'; return; }

    // 按 市场·周期·参数 分组 (最新在前)
    const groups = {};
    list.slice().reverse().forEach(p => {
      const key = describe(p);
      if (!groups[key]) groups[key] = [];
      groups[key].push(p);
    });

    const keys = Object.keys(groups).sort((a,b) => a.localeCompare(b, 'zh'));
    document.getElementById('report-list').innerHTML = keys.map(key =>
      '<div class="group-card"><div class="g-head"><span class="g-title">' + key + '</span>' +
      '<span class="g-count">' + groups[key].length + ' 份</span></div>' +
      groups[key].map(p => {
        const fn = p.split('/').pop();
        return '<div class="report-row"><span class="r-name">' + p + '</span>' +
          '<span class="r-time">' + tsFromName(fn) + '</span>' +
          '<a class="open" href="/reports/' + p + '" target="_blank">打开 ↗</a></div>';
      }).join('') + '</div>'
    ).join('');
  } catch(e) {
    document.getElementById('report-list').innerHTML = '<div class="empty">⚠️ 加载失败: ' + e.message + '</div>';
  }
}

window.onload = load;
</script>
</body>
</html>"""


HTML_TESTNET = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔌 Testnet — 现货 / 合约真实下单</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; }
.app-shell { display: flex; height: 100vh; overflow: hidden; }
.app-sidebar { width: 200px; flex-shrink: 0; background: #161b22; border-right: 1px solid #30363d; padding: 16px 12px; overflow-y: auto; }
.app-sidebar .logo { font-size: 15px; font-weight: 700; color: #f0f6fc; margin-bottom: 6px; }
.app-sidebar .nav-group { margin-top: 14px; }
.app-sidebar .group-title { font-size: 11px; color: #8b949e; margin-bottom: 6px; letter-spacing: 1px; }
.app-sidebar a { display: block; padding: 6px 8px; color: #8b949e; text-decoration: none; border-radius: 5px; font-size: 13px; }
.app-sidebar a:hover { background: #21262d; color: #f0f6fc; }
.app-sidebar a.active { background: #1f6feb; color: #fff; }
.app-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.header { display: flex; align-items: center; gap: 12px; padding: 10px 20px; background: #161b22; border-bottom: 1px solid #30363d; height: 50px; }
.header h1 { font-size: 15px; font-weight: 600; color: #f0f6fc; }
.sub { font-size: 12px; color: #8b949e; }
.btn-refresh { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; font-size: 12px; cursor: pointer; }
.container { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.cards { display: flex; gap: 14px; align-items: flex-start; }
.card { flex: 1; min-width: 0; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
.card h3 { font-size: 14px; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; }
.badge.ok { background: #1f3d24; color: #3fb950; }
.badge.no { background: #3a1a1a; color: #f85149; }
.kv { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; border-bottom: 1px dashed #21262d; }
.kv .k { color: #8b949e; }
.kv .v { font-weight: 600; color: #e6edf3; }
table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
th, td { text-align: left; padding: 5px 6px; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 500; }
.pos { color: #3fb950; }
.neg { color: #f85149; }
.form { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; border-top: 1px solid #30363d; padding-top: 10px; }
.f-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.f-label { font-size: 12px; color: #8b949e; white-space: nowrap; }
.f-side { display: flex; gap: 6px; }
.f-btn { padding: 4px 12px; border: 1px solid #30363d; border-radius: 6px; background: #161b22; color: #8b949e; cursor: pointer; font-size: 12px; }
.f-btn.buy.active { background: #238636; color: #fff; border-color: #238636; }
.f-btn.sell.active { background: #da3633; color: #fff; border-color: #da3633; }
.f-form select, .f-form input[type=number] { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; padding: 4px 6px; font-size: 12px; width: 110px; }
.f-submit { width: 100%; padding: 8px; border: none; border-radius: 6px; background: #2f81f7; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
.f-submit:hover { background: #388bfd; }
.f-msg { font-size: 12px; color: #8b949e; word-break: break-all; }
.f-msg.ok { color: #3fb950; }
.f-msg.err { color: #f85149; }
.tip { font-size: 12px; color: #8b949e; line-height: 1.7; margin-top: 8px; padding: 10px 12px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
.tip code { background: #21262d; padding: 1px 5px; border-radius: 3px; color: #58a6ff; }
.tip a { color: #58a6ff; }
.empty { color: #8b949e; font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
<div class="app-shell">
  <div class="app-sidebar">
    <div class="logo">📈 ETH 量化平台</div>
    <nav class="nav-group">
      <div class="group-title">导航</div>
      <a href="http://127.0.0.1:8082/info">🏠 信息</a>
      <a href="http://127.0.0.1:8082/alpha">🧪 Alpha回测</a>
      <a href="http://127.0.0.1:8082/reports">📊 策略及回测</a>
      <a href="http://127.0.0.1:8082/testnet" class="active">🔌 Testnet</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">现货 · 8080</div>
      <a href="http://127.0.0.1:8080/">📊 信号</a>
      <a href="http://127.0.0.1:8080/flow">🐋 大资金</a>
    </nav>
    <nav class="nav-group">
      <div class="group-title">合约 · 8081</div>
      <a href="http://127.0.0.1:8081/">📊 信号</a>
      <a href="http://127.0.0.1:8081/flow">🐋 大资金</a>
    </nav>
  </div>
  <div class="app-main">
    <div class="header">
      <h1>🔌 Binance Testnet</h1>
      <span class="sub" id="stat-line">--</span>
      <button class="btn-refresh" onclick="load()">🔄 刷新</button>
    </div>
    <div class="container">
      <div class="cards">
        <div class="card">
          <h3>💵 现货 Testnet <span class="badge no" id="badge-spot">未配置</span></h3>
          <div id="body-spot"><div class="empty">加载中...</div></div>
        </div>
        <div class="card">
          <h3>📈 合约 Testnet <span class="badge no" id="badge-futures">未配置</span></h3>
          <div id="body-futures"><div class="empty">加载中...</div></div>
        </div>
      </div>
      <div class="tip">🔑 未配置 Key 时显示红色"未配置": 在项目根目录 <code>config_testnet.json</code> 填入对应市场的
        api_key / api_secret 后点"🔄 刷新"即可. Testnet Key 需在
        <a href="https://testnet.binance.vision" target="_blank">testnet.binance.vision</a>(现货) /
        <a href="https://testnet.binancefuture.com" target="_blank">testnet.binancefuture.com</a>(合约) 分别注册创建,
        与主网 Key 不通用. 下单使用币安免费测试资金, 不影响真实资产.</div>
    </div>
  </div>
</div>

<script>
const META = {
  spot:    { name: '现货', side: { buy: '买入', sell: '卖出' } },
  futures: { name: '合约', side: { buy: '买入多', sell: '卖出空' } }
};
window['_side_spot'] = 'buy';
window['_side_futures'] = 'buy';

async function load(){
  try {
    const r = await (await fetch('/alpha/api/testnet?t=' + Date.now())).json();
    document.getElementById('stat-line').textContent = '更新于 ' + (r.server_time || '--');
    render('spot', r.markets && r.markets.spot);
    render('futures', r.markets && r.markets.futures);
  } catch(e) {
    document.getElementById('stat-line').textContent = '⚠️ 加载失败: ' + e.message;
  }
}

function render(mk, d){
  const body = document.getElementById('body-' + mk);
  if (!d) return;
  const badge = document.getElementById('badge-' + mk);
  if (d.ready) { badge.className = 'badge ok'; badge.textContent = '✅ 已配置'; }
  else { badge.className = 'badge no'; badge.textContent = '⚠️ 未配置Key'; }
  if (!d.ready) {
    body.innerHTML = '<div class="kv"><span class="k">ETH 价格</span><span class="v">$' +
      (d.price == null ? '--' : d.price.toFixed(2)) + '</span></div>' +
      '<div class="empty">✅ 网络已连通 · 请先填写 config_testnet.json 中的 ' + META[mk].name + ' Key 才能下单</div>';
    return;
  }
  if (d.error) {
    body.innerHTML = '<div class="empty neg">⚠️ ' + d.error + '</div>';
    return;
  }
  let html = '<div class="kv"><span class="k">ETH 价格</span><span class="v">$' +
    (d.price == null ? '--' : d.price.toFixed(2)) + '</span></div>';
  const bal = (d.balance || []);
  // 只显示主要资产(USDT/ETH/BTC/BNB 优先, 最多 8 项), 其余折叠计数
  const top = ['USDT','ETH','BTC','BNB'];
  const main = bal.filter(b => top.indexOf(b.asset) >= 0);
  const rest = bal.filter(b => top.indexOf(b.asset) < 0);
  const shown = main.concat(rest).slice(0, 8);
  const more = bal.length - shown.length;
  html += '<div class="kv"><span class="k">账户资产</span><span class="v">' +
    (bal.length ? shown.map(b => b.asset + ' ' + b.balance.toFixed(4) + ' (' + b.available.toFixed(4) + ')').join(' / ') +
      (more > 0 ? ' … 等' + more + '项' : '') : '--') +
    '</span></div>';
  const pos = (d.positions || []);
  if (pos.length) {
    if (mk === 'spot') {
      // 现货: 持仓即余额结构 {asset, balance, available}
      html += '<table><tr><th>资产</th><th>余额</th><th>可用</th></tr>' +
        pos.map(p => '<tr><td>' + p.asset + '</td><td>' + p.balance.toFixed(4) +
          '</td><td>' + p.available.toFixed(4) + '</td></tr>').join('') + '</table>';
    } else {
      html += '<table><tr><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>未实现盈亏</th><th>杠杆</th></tr>' +
        pos.map(p => {
          const amt = p.positionAmt || 0;
          const dir = amt > 0 ? '<span class="pos">多</span>' : '<span class="neg">空</span>';
          const lev = p.leverage ? p.leverage + 'x' : '--';
          return '<tr><td>' + dir + '</td><td>' + Math.abs(amt) + '</td><td>' + p.entryPrice +
            '</td><td>' + p.markPrice + '</td><td class="' + (p.unRealizedProfit >= 0 ? 'pos' : 'neg') + '">' +
            p.unRealizedProfit.toFixed(4) + '</td><td>' + lev + '</td></tr>';
        }).join('') + '</table>';
    }
  } else {
    html += '<div class="kv"><span class="k">持仓</span><span class="v">💤 空仓</span></div>';
  }
  // 平仓按钮始终显示(空仓点击会提示无持仓)
  html += '<button type="button" class="f-submit" style="margin-top:10px;background:#da3633" onclick="closePosition(\\'' + mk + '\\')">' +
    (mk === 'spot' ? '📤 市价卖出全部 ETH' : '📤 市价平仓') + '</button>';
  html += '<div class="form" id="form-' + mk + '">' +
    '<div class="f-row"><span class="f-label">方向</span><div class="f-side">' +
      '<button type="button" class="f-btn buy active" onclick="setSide(\\'' + mk + '\\',\\'buy\\')">' + META[mk].side.buy + '</button>' +
      '<button type="button" class="f-btn sell" onclick="setSide(\\'' + mk + '\\',\\'sell\\')">' + META[mk].side.sell + '</button>' +
    '</div></div>' +
    '<div class="f-row"><span class="f-label">类型</span><select onchange="togglePrice(\\'' + mk + '\\')">' +
      '<option value="market">市价单</option><option value="limit">限价单</option></select></div>' +
    '<div class="f-row f-price-row" style="display:none"><span class="f-label">限价</span>' +
      '<input type="number" class="price-inp" step="0.01" placeholder="0.00"></div>' +
    '<div class="f-row"><span class="f-label">数量 ETH</span>' +
      '<input type="number" class="qty-inp" step="0.001" min="0.001" value="0.01"></div>' +
    (mk === 'futures' ? '<div class="f-row"><span class="f-label">杠杆</span><select class="lev-inp">' +
      [1,2,3,5,10,20,50].map(l => '<option value="' + l + '"' + (l === 3 ? ' selected' : '') + '>' + l + 'x</option>').join('') +
      '</select></div>' : '') +
    '<button type="button" class="f-submit" onclick="submitOrder(\\'' + mk + '\\')">🚀 下单</button>' +
    '<div class="f-msg" id="msg-' + mk + '"></div></div>';
  body.innerHTML = html;
}

function setSide(mk, side){
  window['_side_' + mk] = side;
  const f = document.getElementById('form-' + mk);
  if (!f) return;
  const btns = f.querySelectorAll('.f-btn');
  btns[0].classList.toggle('active', side === 'buy');
  btns[1].classList.toggle('active', side === 'sell');
}

function togglePrice(mk){
  const f = document.getElementById('form-' + mk);
  if (!f) return;
  const isLimit = f.querySelector('select').value === 'limit';
  f.querySelector('.f-price-row').style.display = isLimit ? '' : 'none';
}

async function submitOrder(mk){
  const f = document.getElementById('form-' + mk);
  const msg = document.getElementById('msg-' + mk);
  const qty = parseFloat(f.querySelector('.qty-inp').value);
  if (!qty || qty <= 0) { msg.className = 'f-msg err'; msg.textContent = '请填写数量(ETH)'; return; }
  const body = { market: mk, side: window['_side_' + mk] || 'buy', order_type: f.querySelector('select').value, qty };
  if (body.order_type === 'limit') {
    body.price = parseFloat(f.querySelector('.price-inp').value);
    if (!body.price || body.price <= 0) { msg.className = 'f-msg err'; msg.textContent = '请填写有效限价'; return; }
  }
  if (mk === 'futures') {
    const lev = f.querySelector('.lev-inp');
    if (lev) body.leverage = parseInt(lev.value);
  }
  msg.className = 'f-msg'; msg.textContent = '下单中...';
  try {
    const r = await (await fetch('/alpha/api/testnet/order', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    })).json();
    if (r.ok) {
      const o = r.order || {};
      msg.className = 'f-msg ok';
      msg.textContent = '✅ 下单成功 订单#' + (o.orderId ?? '') + ' 状态:' + (o.status ?? '') +
        ' 成交:' + (o.filledQty ?? qty) + ' ETH';
      setTimeout(load, 500);
    } else {
      msg.className = 'f-msg err';
      msg.textContent = '❌ ' + (r.message || '下单失败');
    }
  } catch(e) {
    msg.className = 'f-msg err'; msg.textContent = '请求失败: ' + e.message;
  }
}

async function closePosition(mk){
  const msg = document.getElementById('msg-' + mk);
  msg.className = 'f-msg'; msg.textContent = '平仓中...';
  try {
    const r = await (await fetch('/alpha/api/testnet/close', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({market: mk})
    })).json();
    if (r.ok) {
      const cs = (r.closes || []);
      msg.className = 'f-msg ok';
      msg.textContent = cs.length
        ? '✅ 平仓成功: ' + cs.map(o => o.symbol + ' ' + o.side + ' ' + o.qty + ' 单#' + o.orderId).join(' / ')
        : (r.message || '✅ 平仓成功');
      setTimeout(load, 800);
    } else {
      msg.className = 'f-msg err';
      msg.textContent = '❌ ' + (r.message || '平仓失败');
    }
  } catch(e) {
    msg.className = 'f-msg err'; msg.textContent = '平仓请求失败: ' + e.message;
  }
}

load();
setInterval(load, 8000);
</script>
</body>
</html>"""


@app.get("/alpha", response_class=HTMLResponse)
def alpha_page():
    return HTML_ALPHA


@app.get("/info", response_class=HTMLResponse)
def info_page():
    return HTML_ALPHA_INFO


@app.get("/reports", response_class=HTMLResponse)
def reports_page():
    return HTML_ALPHA_REPORTS


@app.get("/testnet", response_class=HTMLResponse)
def testnet_page():
    return HTML_TESTNET


@app.get("/alpha/api/health")
def health():
    return {"status": "ok", "port": SERVER_PORT}


# ---- 静态资源 ----
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# 回测报告静态目录 (报告页 /reports 点击打开用)
app.mount("/reports", StaticFiles(directory=os.path.join(BASE_DIR, "reports")), name="reports")


if __name__ == "__main__":
    import uvicorn
    daily_log.setup("alpha")  # 控制台 + logs/alpha/<当天日期>.log
    print("=" * 60)
    print("  🧪 Alpha 因子实验室: http://127.0.0.1:8082/alpha")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")
