"""补拉完整文件接缝处的微小空洞并合并入 ETHUSDT-5m-full.csv

两个空洞 (本地时间):
  2023-08-13 00:05 ~ 00:15 (两文件接缝)
  2026-07-28 00:05 ~ 00:30 (补拉段尾部)
"""
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import fetch_data_contract as fdc

fdc.INTERVAL = "5m"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FULL = os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-full.csv")


def ms(s):
    return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))) * 1000


def fetch_span(start, end):
    klines = []
    cur = ms(start)
    e = ms(end)
    while cur < e:
        data = None
        for attempt in range(6):
            try:
                data = fdc.fetch_klines(cur, e)
                break
            except Exception as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                wait = 60 + attempt * 30 if code in (418, 429) else 3 + attempt * 5
                print(f"重试{attempt+1}/6 ({type(exc).__name__}{code or ''}), 等{wait}s...", flush=True)
                time.sleep(wait)
        if data is None:
            raise RuntimeError("重试耗尽")
        if not data:
            break
        klines.extend(data)
        cur = int(data[-1][0]) + 1
        if len(data) < 500:
            break
        time.sleep(2.0)
    return klines


def main():
    patches = fetch_span("2023-08-13 00:00:00", "2023-08-13 00:20:00")
    patches += fetch_span("2026-07-28 00:00:00", "2026-07-28 00:35:00")
    print(f"补拉 {len(patches)} 根", flush=True)
    if not patches:
        return

    with open(FULL, encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        rows = [line for line in r if line]
    merged = {int(row[0]): row for row in rows}
    for k in patches:
        merged[int(k[0])] = [int(k[0]), round(float(k[1]), 2), round(float(k[2]), 2),
                             round(float(k[3]), 2), round(float(k[4]), 2), round(float(k[5]), 6),
                             int(k[6]), round(float(k[7]), 2), int(k[8]),
                             round(float(k[9]), 6), round(float(k[10]), 6)]
    keys = sorted(merged)
    with open(FULL, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for ts in keys:
            w.writerow(merged[ts])
    print(f"合并后: {len(keys)} 根 → {FULL}", flush=True)


if __name__ == "__main__":
    main()
