"""检查合并后 CSV 的时间戳空洞 (相邻 K线间隔 > 10min 的位置)"""
import csv
import sys

path = sys.argv[1]
prev = None
gaps = []
count = 0
with open(path, encoding="utf-8") as f:
    r = csv.reader(f)
    next(r)
    for line in r:
        if not line:
            continue
        ts = int(line[0])
        count += 1
        if prev is not None:
            d = ts - prev
            if d > 600000:  # >10min
                gaps.append((prev, ts, d))
        prev = ts
print(f"{path}: {count} 根, 空洞数: {len(gaps)}")
for g, t, d in gaps:
    import time
    g1 = time.strftime("%Y-%m-%d %H:%M", time.localtime(g / 1000))
    g2 = time.strftime("%Y-%m-%d %H:%M", time.localtime(t / 1000))
    print(f"  空洞: {g1} ~ {g2} (缺 {d/86400000:.1f} 天)")
