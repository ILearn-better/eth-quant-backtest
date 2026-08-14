"""
插针(wick)幅度分布分析脚本

统计 ETHUSDT 永续合约 5年 1h K线的插针特征:
1. 上影线/下影线幅度分布
2. 不同阈值下的插针出现频率
3. 插针后价格回归率 (决定挂单策略是否赚钱的关键)
4. 模拟挂单策略: 在 open*0.9 挂多 / open*1.1 挂空, 统计成交与回归

用法: python analyze_wick.py
"""
import csv
import statistics
from collections import Counter


def load_klines(path):
    """加载K线数据, 返回 list[dict]"""
    bars = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append({
                "ts": int(row["timestamp"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
    return bars


def analyze_wick_distribution(bars):
    """统计影线幅度分布"""
    lower_wicks = []   # 下影线幅度 = (min(open,close) - low) / open
    upper_wicks = []   # 上影线幅度 = (high - max(open,close)) / open
    ranges = []        # 整根K线振幅 = (high - low) / open

    for b in bars:
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        lower_wick = (min(o, c) - l) / o * 100  # 百分比
        upper_wick = (h - max(o, c)) / o * 100
        full_range = (h - l) / o * 100
        lower_wicks.append(lower_wick)
        upper_wicks.append(upper_wick)
        ranges.append(full_range)

    return lower_wicks, upper_wicks, ranges


def percentile(data, p):
    """计算百分位数"""
    sorted_data = sorted(data)
    n = len(sorted_data)
    k = (n - 1) * p / 100
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def print_distribution(name, data):
    """打印分布统计"""
    print(f"\n{'='*60}")
    print(f"  {name} 幅度分布 (n={len(data)})")
    print(f"{'='*60}")
    print(f"  均值:   {statistics.mean(data):.3f}%")
    print(f"  中位数: {statistics.median(data):.3f}%")
    print(f"  标准差: {statistics.stdev(data):.3f}%")
    print(f"  最大值: {max(data):.3f}%")
    print(f"  最小值: {min(data):.3f}%")
    print()
    print(f"  百分位分布:")
    for p in [50, 75, 90, 95, 99, 99.5, 99.9]:
        val = percentile(data, p)
        print(f"    P{p:<5}: {val:>7.3f}%")
    print()
    print(f"  阈值频率 (>= X% 的占比):")
    for thresh in [1, 2, 3, 5, 8, 10, 15]:
        count = sum(1 for x in data if x >= thresh)
        pct = count / len(data) * 100
        avg_per_day = count / (len(data) / 24)
        print(f"    >= {thresh:>2}%: {count:>5} 根 ({pct:>5.2f}%)  ≈ {avg_per_day:.2f} 次/天")


def analyze_reversion(bars, thresholds):
    """
    分析插针后的回归率
    回归定义: 收盘价回到开盘价附近 (|close-open|/open < 0.5%)
    """
    print(f"\n{'='*60}")
    print(f"  插针后价格回归分析")
    print(f"{'='*60}")

    for thresh in thresholds:
        # 下插针: 下影线 >= thresh%
        lower_spikes = []
        for b in bars:
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            lower_wick = (min(o, c) - l) / o * 100
            if lower_wick >= thresh:
                # 回归幅度: 从最低点回升了多少
                reversion = (c - l) / (o - l) * 100 if o != l else 100
                # 收盘是否 >= 开盘 (多头回归成功)
                close_above_open = c >= o
                # 收盘相对开盘的变化
                close_change = (c - o) / o * 100
                lower_spikes.append({
                    "reversion": reversion,
                    "close_above_open": close_above_open,
                    "close_change": close_change,
                    "low_drop": (l - o) / o * 100,  # 最低点相对开盘跌了多少
                })

        # 上插针: 上影线 >= thresh%
        upper_spikes = []
        for b in bars:
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            upper_wick = (h - max(o, c)) / o * 100
            if upper_wick >= thresh:
                reversion = (h - c) / (h - o) * 100 if h != o else 100
                close_below_open = c <= o
                close_change = (c - o) / o * 100
                upper_spikes.append({
                    "reversion": reversion,
                    "close_below_open": close_below_open,
                    "close_change": close_change,
                    "high_surge": (h - o) / o * 100,
                })

        print(f"\n  --- 阈值: {thresh}% ---")

        if lower_spikes:
            rev_list = [s["reversion"] for s in lower_spikes]
            above_count = sum(1 for s in lower_spikes if s["close_above_open"])
            close_changes = [s["close_change"] for s in lower_spikes]
            print(f"  下插针 (下影线>={thresh}%): {len(lower_spikes)} 根")
            print(f"    回归率(收盘>=开盘): {above_count}/{len(lower_spikes)} = {above_count/len(lower_spikes)*100:.1f}%")
            print(f"    平均回归幅度(从最低回升): {statistics.mean(rev_list):.1f}%")
            print(f"    回归中位数: {statistics.median(rev_list):.1f}%")
            print(f"    收盘相对开盘 平均变化: {statistics.mean(close_changes):+.3f}%")
            print(f"    收盘相对开盘 中位数:   {statistics.median(close_changes):+.3f}%")

        if upper_spikes:
            rev_list = [s["reversion"] for s in upper_spikes]
            below_count = sum(1 for s in upper_spikes if s["close_below_open"])
            close_changes = [s["close_change"] for s in upper_spikes]
            print(f"  上插针 (上影线>={thresh}%): {len(upper_spikes)} 根")
            print(f"    回归率(收盘<=开盘): {below_count}/{len(upper_spikes)} = {below_count/len(upper_spikes)*100:.1f}%")
            print(f"    平均回归幅度(从最高回落): {statistics.mean(rev_list):.1f}%")
            print(f"    回归中位数: {statistics.median(rev_list):.1f}%")
            print(f"    收盘相对开盘 平均变化: {statistics.mean(close_changes):+.3f}%")
            print(f"    收盘相对开盘 中位数:   {statistics.median(close_changes):+.3f}%")


def simulate_pending_orders(bars, buy_pct, sell_pct):
    """
    模拟挂单策略
    每根K线开盘时, 在 open*(1-buy_pct) 挂多单, open*(1+sell_pct) 挂空单
    成交条件: low <= 挂单价(多) 或 high >= 挂单价(空)
    止盈: 收盘价回到 open 附近 (|close-open|/open < 0.5%)
    止损: 收盘价相对挂单价继续偏离 3%
    """
    buy_thresh = buy_pct / 100   # 如 10 表示 10%
    sell_thresh = sell_pct / 100

    print(f"\n{'='*60}")
    print(f"  挂单策略模拟: 多单挂 open*{1-buy_thresh:.2f}, 空单挂 open*{1+sell_thresh:.2f}")
    print(f"{'='*60}")

    # 多单统计
    long_triggers = 0     # 触发次数
    long_win = 0          # 止盈(回归)
    long_loss = 0         # 止损(继续跌)
    long_hold = 0         # 持仓未平(收盘在中间)
    long_pnl = []         # 每笔盈亏百分比(相对挂单价)

    # 空单统计
    short_triggers = 0
    short_win = 0
    short_loss = 0
    short_hold = 0
    short_pnl = []

    for b in bars:
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        buy_price = o * (1 - buy_thresh)
        sell_price = o * (1 + sell_thresh)

        # 多单: low 触及挂单价
        if l <= buy_price:
            long_triggers += 1
            # 成交价 = buy_price
            # 收盘盈亏 = (close - buy_price) / buy_price
            pnl = (c - buy_price) / buy_price * 100
            long_pnl.append(pnl)
            if c >= o * 0.995:        # 回归到开盘附近, 止盈
                long_win += 1
            elif c <= buy_price * 0.97:  # 继续跌3%, 止损
                long_loss += 1
            else:
                long_hold += 1

        # 空单: high 触及挂单价
        if h >= sell_price:
            short_triggers += 1
            pnl = (sell_price - c) / sell_price * 100
            short_pnl.append(pnl)
            if c <= o * 1.005:            # 回归到开盘附近, 止盈
                short_win += 1
            elif c >= sell_price * 1.03:   # 继续涨3%, 止损
                short_loss += 1
            else:
                short_hold += 1

    total_days = len(bars) / 24

    print(f"\n  数据范围: {len(bars)} 根K线 ≈ {total_days:.0f} 天 ≈ {total_days/365:.1f} 年")
    print()
    print(f"  【多单】挂单幅度 {buy_pct}% (买价=open*{1-buy_thresh:.2f}):")
    print(f"    触发次数:  {long_triggers}")
    print(f"    触发频率:  {long_triggers/total_days:.2f} 次/天  ({long_triggers/len(bars)*100:.2f}% 的K线)")
    if long_triggers > 0:
        print(f"    止盈(回归): {long_win} ({long_win/long_triggers*100:.1f}%)")
        print(f"    止损(续跌): {long_loss} ({long_loss/long_triggers*100:.1f}%)")
        print(f"    持仓未平:   {long_hold} ({long_hold/long_triggers*100:.1f}%)")
        print(f"    平均盈亏:   {statistics.mean(long_pnl):+.3f}%")
        print(f"    中位数:     {statistics.median(long_pnl):+.3f}%")
        print(f"    最大盈利:   {max(long_pnl):+.3f}%")
        print(f"    最大亏损:   {min(long_pnl):+.3f}%")
        wins = [p for p in long_pnl if p > 0]
        losses = [p for p in long_pnl if p < 0]
        if wins and losses:
            print(f"    盈亏比:     {statistics.mean(wins)/abs(statistics.mean(losses)):.2f}")
        total_pnl = sum(long_pnl)
        print(f"    累计盈亏:   {total_pnl:+.2f}% (简单累加, 非复利)")

    print()
    print(f"  【空单】挂单幅度 {sell_pct}% (卖价=open*{1+sell_thresh:.2f}):")
    print(f"    触发次数:  {short_triggers}")
    print(f"    触发频率:  {short_triggers/total_days:.2f} 次/天  ({short_triggers/len(bars)*100:.2f}% 的K线)")
    if short_triggers > 0:
        print(f"    止盈(回归): {short_win} ({short_win/short_triggers*100:.1f}%)")
        print(f"    止损(续涨): {short_loss} ({short_loss/short_triggers*100:.1f}%)")
        print(f"    持仓未平:   {short_hold} ({short_hold/short_triggers*100:.1f}%)")
        print(f"    平均盈亏:   {statistics.mean(short_pnl):+.3f}%")
        print(f"    中位数:     {statistics.median(short_pnl):+.3f}%")
        print(f"    最大盈利:   {max(short_pnl):+.3f}%")
        print(f"    最大亏损:   {min(short_pnl):+.3f}%")
        wins = [p for p in short_pnl if p > 0]
        losses = [p for p in short_pnl if p < 0]
        if wins and losses:
            print(f"    盈亏比:     {statistics.mean(wins)/abs(statistics.mean(losses)):.2f}")
        total_pnl = sum(short_pnl)
        print(f"    累计盈亏:   {total_pnl:+.2f}% (简单累加, 非复利)")

    if long_triggers > 0 and short_triggers > 0:
        combined_pnl = sum(long_pnl) + sum(short_pnl)
        print(f"\n  【双向合计】")
        print(f"    总成交:     {long_triggers + short_triggers}")
        print(f"    累计盈亏:   {combined_pnl:+.2f}% (简单累加)")


def main():
    data_file = "data/futures/ETHUSDT-1h.csv"
    print(f"加载合约数据: {data_file}")
    bars = load_klines(data_file)
    print(f"共 {len(bars)} 根K线")
    days = len(bars) / 24
    print(f"约 {days:.0f} 天 ≈ {days/365:.1f} 年")

    # 1. 影线分布
    lower, upper, ranges = analyze_wick_distribution(bars)
    print_distribution("下影线 (下插针深度)", lower)
    print_distribution("上影线 (上插针深度)", upper)
    print_distribution("整根K线振幅 (high-low)", ranges)

    # 2. 插针后回归分析
    analyze_reversion(bars, [2, 3, 5, 8, 10])

    # 3. 挂单策略模拟 (不同阈值)
    for pct in [3, 5, 8, 10]:
        simulate_pending_orders(bars, pct, pct)

    print(f"\n{'='*60}")
    print("  分析完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
