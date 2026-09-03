"""模拟盘日报生成器 — 读取 paper_state 汇总当日收益, 生成 HTML 报告

用法:
  python paper_report.py            # 生成报告并打印摘要
  python paper_report.py --days 7   # 汇总最近N天
"""
import json
import os
import sys
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "paper_state", "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "paper_state", "trades.jsonl")
SNAPSHOT_FILE = os.path.join(BASE_DIR, "paper_state", "daily_snapshots.jsonl")
REPORT_DIR = os.path.join(BASE_DIR, "paper_state", "reports")


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    trades = []
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def load_snapshots():
    if not os.path.exists(SNAPSHOT_FILE):
        return []
    snaps = []
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                snaps.append(json.loads(line))
    return snaps


def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def generate_report(days=0):
    state = load_state()
    trades = load_trades()
    snapshots = load_snapshots()

    os.makedirs(REPORT_DIR, exist_ok=True)

    now = datetime.datetime.now()
    today_str = now.strftime("%Y%m%d")
    report_path = os.path.join(REPORT_DIR, f"paper_report_{today_str}.html")

    if state is None:
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>模拟盘日报</title></head><body>
<h1>模拟盘尚未启动</h1>
<p>请先运行: python paper_trading.py 或双击 start_paper_trading.bat</p>
</body></html>"""
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        print("⚠️ 模拟盘尚未启动, 请先运行 paper_trading.py")
        return report_path

    # ---- 统计 ----
    balance = state.get("balance", 150.0)
    peak = state.get("peak_balance", 150.0)
    initial = 150.0
    return_pct = (balance - initial) / initial * 100
    position = state.get("position")

    # 今日交易 (按 exit_time 过滤到最近 days 天, 或全部)
    today_trades = trades
    if days > 0:
        cutoff = (now - datetime.timedelta(days=days)).timestamp() * 1000
        today_trades = [t for t in trades if t.get("exit_time", 0) >= cutoff]

    today_pnl = sum(t.get("pnl", 0) for t in today_trades)
    wins = [t for t in today_trades if t.get("pnl", 0) > 0]
    losses = [t for t in today_trades if t.get("pnl", 0) <= 0]

    # 最近30天交易 (如果days>0用days, 否则默认30)
    lookback = days if days > 0 else 30
    cutoff30 = (now - datetime.timedelta(days=lookback)).timestamp() * 1000
    recent = [t for t in trades if t.get("exit_time", 0) >= cutoff30]

    # ---- 交易明细行 ----
    trade_rows = ""
    for i, t in enumerate(reversed(today_trades[-50:])):
        direction = t.get("direction", "")
        pnl = t.get("pnl", 0)
        pnl_color = "#e74c3c" if pnl >= 0 else "#27ae60"  # 中国习惯: 红涨绿跌
        reason_map = {"momentum_death": "动量衰竭", "SL": "止损", "timeout": "超时",
                      "force_close": "期末强平", "SL(catchup)": "止损(补跑)", "timeout(catchup)": "超时(补跑)"}
        reason = reason_map.get(t.get("reason", ""), t.get("reason", ""))
        dir_label = "做多" if direction == "long" else "做空"
        trade_rows += f"""<tr>
            <td>{len(today_trades)-i}</td>
            <td>{fmt_ts(t.get('entry_time', 0))}</td>
            <td>{fmt_ts(t.get('exit_time', 0))}</td>
            <td>{dir_label}</td>
            <td>{t.get('entry_price', '-')}</td>
            <td>{t.get('exit_price', '-')}</td>
            <td style="color:{pnl_color};font-weight:bold">{pnl:+.2f}</td>
            <td>{reason}</td>
            <td>{t.get('held_bars', '-')}根</td>
        </tr>"""

    win_rate_txt = f"{len(wins)/len(today_trades)*100:.0f}%" if today_trades else "-"

    # ---- 快照曲线数据 (JSON for JS chart) ----
    snap_json = json.dumps(snapshots, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETH v12 模拟盘日报 {now.strftime('%Y-%m-%d')}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; background:#0f0f14; color:#e0e0e0; padding:20px; }}
.header {{ text-align:center; padding:25px 20px; border-bottom:1px solid #2a2a3a; margin-bottom:25px; }}
.header h1 {{ font-size:26px; color:#f7931a; }} .header .sub {{ color:#888; margin-top:8px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:25px; }}
.card {{ background:#1a1a24; border-radius:12px; padding:18px; border:1px solid #2a2a3a; }}
.card .label {{ color:#888; font-size:12px; text-transform:uppercase; letter-spacing:1px; }}
.card .value {{ font-size:26px; font-weight:bold; margin-top:8px; }}
.card.positive .value {{ color:#e74c3c; }} .card.negative .value {{ color:#27ae60; }} .card.neutral .value {{ color:#f7931a; }}
.chart-section {{ background:#1a1a24; border-radius:12px; padding:24px; border:1px solid #2a2a3a; margin-bottom:20px; }}
.chart-section h2 {{ font-size:17px; color:#fff; margin-bottom:14px; border-left:3px solid #f7931a; padding-left:12px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:#252532; color:#aaa; padding:9px 10px; text-align:left; position:sticky;top:0; }}
td {{ padding:7px 10px; border-bottom:1px solid #222; }}
tr:hover td {{ background:#252532; }}
.pos-badge {{ display:inline-block; padding:2px 10px; border-radius:10px; font-size:12px; font-weight:bold; }}
.pos-long {{ background:#e74c3c22; color:#e74c3c; border:1px solid #e74c3c55; }}
.pos-short {{ background:#27ae6022; color:#27ae60; border:1px solid #27ae6055; }}
.pos-none {{ background:#555; color:#ccc; }}
</style>
</head>
<body>

<div class="header">
    <h1>📊 ETH v12 双ROC动量策略 — 模拟盘日报</h1>
    <div class="sub">生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')} | 启动于: {state.get('started_at', '-')} | 初始资金: 150 USDT (3x)</div>
</div>

<div class="grid">
    <div class="card {'positive' if return_pct>=0 else 'negative'}">
        <div class="label">当前权益</div><div class="value">{balance:.2f}U</div></div>
    <div class="card {'positive' if return_pct>=0 else 'negative'}">
        <div class="label">累计收益率</div><div class="value">{return_pct:+.2f}%</div></div>
    <div class="card neutral"><div class="label">累计盈亏</div><div class="value">{balance-initial:+.2f}U</div></div>
    <div class="card neutral"><div class="label">总交易数</div><div class="value">{len(trades)}</div></div>
    <div class="card {'positive' if today_pnl>=0 else 'negative'}">
        <div class="label">{'近'+str(days)+'天' if days>0 else '全部'}盈亏</div><div class="value">{today_pnl:+.2f}U</div></div>
    <div class="card neutral"><div class="label">胜率({len(today_trades)}笔)</div>
        <div class="value">{win_rate_txt}</div></div>
    <div class="card neutral"><div class="label">当前持仓</div>
        <div class="value" style="font-size:16px">
        {'<span class="pos-badge pos-long">做多</span>' if position and position.get('direction')=='long'
         else '<span class="pos-badge pos-short">做空</span>' if position
         else '<span class="pos-badge pos-none">空仓</span>'}
        </div></div>
</div>

<div class="chart-section">
    <h2>📈 每日权益曲线</h2>
    <canvas id="equityChart" height="100"></canvas>
</div>

<div class="chart-section">
    <h2>📋 最近{min(50,len(today_trades))}笔交易</h2>
    <div style="overflow-x:auto;max-height:380px;overflow-y:auto;">
    <table>
    <tr><th>#</th><th>开仓时间</th><th>平仓时间</th><th>方向</th><th>开仓价</th><th>平仓价</th><th>盈亏U</th><th>原因</th><th>持仓</th></tr>
    {trade_rows if trade_rows else '<tr><td colspan="9" style="text-align:center;color:#666">暂无交易记录</td></tr>'}
    </table></div>
</div>

<script>
const snaps = {snap_json};
const labels = snaps.map(s => s.date);
const values = snaps.map(s => s.balance);
const ctx = document.getElementById('equityChart');
new Chart(ctx, {{
    type: 'line',
    data: {{
        labels: labels,
        datasets: [{{
            label: '权益 (USDT)',
            data: values,
            borderColor: '#f7931a',
            backgroundColor: 'rgba(247,147,26,0.15)',
            fill: true,
            tension: 0.3,
            pointRadius: 4
        }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ labels: {{ color: '#e0e0e0' }} }} }},
        scales: {{
            x: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#222' }} }},
            y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#222' }} }}
        }}
    }}
}});
</script>

</body></html>"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    # ---- 控制台摘要 ----
    print("=" * 55)
    print("📊 模拟盘日报摘要")
    print("=" * 55)
    print(f"  当前权益:   {balance:.2f} USDT")
    print(f"  累计收益率: {return_pct:+.2f}%  ({balance-initial:+.2f}U)")
    print(f"  累计交易:   {len(trades)} 笔")
    period_label = f"近{days}天" if days > 0 else "全部"
    win_rate_console = f"{len(wins)/len(today_trades)*100:.0f}%" if today_trades else "-"
    print(f"  {period_label}盈亏: {today_pnl:+.2f}U ({len(today_trades)}笔, 胜率{win_rate_console})")
    if position:
        dir_label = "做多" if position.get("direction") == "long" else "做空"
        print(f"  当前持仓:   {dir_label} @ {position.get('entry_price')}")
    else:
        print(f"  当前持仓:   空仓")
    print(f"\n📄 报告: {report_path}")
    return report_path


if __name__ == "__main__":
    days = 0
    args = sys.argv[1:]
    if args and args[0] == "--days" and len(args) > 1:
        days = int(args[1])
    generate_report(days)
