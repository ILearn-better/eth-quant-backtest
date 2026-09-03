"""合并多个 5m 合约数据文件 → 新的完整文件 (按时间戳去重, 不动源文件)

输出: data/futures/ETHUSDT-5m-full.csv
"""
import csv
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCES = sys.argv[1:] if len(sys.argv) > 1 else [
    os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-2021-2023.csv"),
    os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m.csv"),
    os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-miss-2026.csv"),
]
OUT = os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-full.csv")


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        for line in r:
            if line:
                rows.append(line)
    return header, rows


def main():
    merged = {}
    header = None
    for path in SOURCES:
        h, rows = read_rows(path)
        if header is None:
            header = h
        assert header == h, f"表头不一致: {header} vs {h}"
        for r in rows:
            merged[int(r[0])] = r  # 同时间戳去重 (后面的文件覆盖)
        print(f"源 {os.path.basename(path)}: {len(rows)} 根")

    keys = sorted(merged)

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for ts in keys:
            w.writerow(merged[ts])

    import time as _t
    t0 = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(keys[0] / 1000))
    t1 = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(keys[-1] / 1000))
    days = (keys[-1] - keys[0]) / 86400000
    print(f"合并后: {len(keys)} 根")
    print(f"范围: {t0} ~ {t1} (~{days:.0f}天)")
    print(f"输出: {OUT}")


if __name__ == "__main__":
    main()
