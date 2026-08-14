"""单独拉取缺失的 2021-08~2023-08 两年 5m 合约数据 (与已有 3 年数据互不干扰)

设计 (按用户要求: 单独拉取 → 每步保存为新数据文件 → 最后合并):
  - 固定时间窗: 2021-08-12 00:00 ~ 2023-08-13 00:00 (与已有 3 年数据在边界可重叠, 合并时去重)
  - 增量落盘 dl_accum_gap.jsonl (进程被杀/中断不丢已拉段)
  - 重跑自动断点续传 (段内抽样判定是否已覆盖)
  - 完成后排序去重写入 data/futures/ETHUSDT-5m-2021-2023.csv (新文件, 不动已有 CSV)
  - 限流: 2 并发 + 每批间隔 2s (≈5权重/s, 远低于 2400/min; 8 并发毫秒级会触发 418 封禁)
"""
import concurrent.futures as cf
import json
import os
import sys
import threading
import time

_argv = sys.argv[1:]      # 先保存 dl_gap 自己的参数
sys.argv = [sys.argv[0]]  # 屏蔽参数, 避免 fetch_data_contract import 时把日期当 INTERVAL/YEARS 解析
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data_contract as fdc  # 复用 fetch_klines (Session 复用, 直连优先, 代理回退)

fdc.INTERVAL = "5m"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WORKERS = 1          # 并发请求数 (用户要求: 拉数据不要并发, 串行拉取控制流量)
SEGMENT_DAYS = 90    # 每段天数 (90天=25920根 ≈ 18批)
BATCH_SLEEP = 2.0    # 每批间隔 (防 418/限流)
# 可传参覆盖时间窗/输出/累积文件: python dl_gap.py <start> <end> [out_csv] [accum]
# (日期用 YYYY-MM-DD 即可, 避免 Start-Process 拼接参数时空格被拆分; 默认 2021-08~2023-08)


def _parse_date(s, default):
    s = s if s else default
    if len(s) == 10:
        s += " 00:00:00"
    return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))) * 1000


START_MS = _parse_date(_argv[0] if len(_argv) > 0 else "", "2021-08-12 00:00:00")
END_MS = _parse_date(_argv[1] if len(_argv) > 1 else "", "2023-08-13 00:00:00")
OUT_CSV = _argv[2] if len(_argv) > 2 else os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-2021-2023.csv")
ACCUM = _argv[3] if len(_argv) > 3 else os.path.join(BASE_DIR, "dl_accum_gap.jsonl")
PROGRESS = os.path.join(BASE_DIR, "dl_gap_progress.txt")
ACCUM_LOCK = threading.Lock()
MUTEX = None


def check_single_instance():
    """单实例守卫 (Windows 命名互斥量, Local+Global 都查): 沙箱偶尔把一条命令拉起两个进程,
    双实例=双倍流量+并发写同一文件。
    注意: 必须用 use_last_error=True + get_last_error(), 直接调 GetLastError() 会被 ctypes
    内部调用覆盖, 导致互斥量冲突检测失效"""
    global MUTEX
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateMutexW = k32.CreateMutexW
    CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    CreateMutexW.restype = wintypes.HANDLE
    for name in ("Local\\dl_gap_single", "Global\\dl_gap_single"):
        h = CreateMutexW(None, True, name)
        if not h:
            continue
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            log(f"已有 dl_gap 实例运行 ({name}), 本实例退出")
            sys.exit(0)
        MUTEX = h  # 保持引用, 防止被 GC 释放
        return


def log(msg):
    print(msg, flush=True)
    try:
        with open(PROGRESS, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def save_segment(klines):
    """每段数据立即追加落盘 (进程中断不丢)"""
    with ACCUM_LOCK:
        with open(ACCUM, "a", encoding="utf-8") as f:
            for k in klines:
                f.write(json.dumps(k, ensure_ascii=False) + "\n")


def fetch_segment(start_ms, end_ms, ts_set=None):
    """拉取一段, 每批数据立即追加落盘 (进程被杀只丢当前批, 重跑自动跳过已有部分)
    ts_set: 已有时间戳集合, 用于段内续传 (从已覆盖部分的末尾继续)"""
    klines = []
    current = start_ms
    if ts_set:
        # 段内续传: 从已覆盖的连续前缀末尾继续, 避免整段重拉
        while current < end_ms and current in ts_set:
            current += 300000
        if current > start_ms:
            log(f"    段{time.strftime('%m-%d', time.localtime(start_ms/1000))} 已有 {int((current-start_ms)/300000)} 根, 从 {time.strftime('%Y-%m-%d %H:%M', time.localtime(current/1000))} 续传")
    while current < end_ms:
        data = None
        last_exc = None
        for attempt in range(6):
            try:
                data = fdc.fetch_klines(current, end_ms)
                break
            except Exception as exc:
                last_exc = exc
                code = getattr(getattr(exc, "response", None), "status_code", None)
                wait = 60 + attempt * 30 if code in (418, 429) else 3 + attempt * 5
                if attempt < 5:
                    log(f"    段{time.strftime('%m-%d', time.localtime(start_ms/1000))} 重试{attempt+1}/6 ({type(last_exc).__name__}{code or ''}), 等{wait}s...")
                    time.sleep(wait)
        if data is None:
            raise RuntimeError(f"段重试耗尽: {last_exc}")
        if not data:
            break
        klines.extend(data)
        save_segment(data)  # 每批立即落盘
        current = int(data[-1][0]) + 1
        if len(data) < 500:
            break
        time.sleep(BATCH_SLEEP)
    return klines


def load_existing():
    """读取已累积数据: 返回 (ts集合, 最小时间戳)"""
    ts_set = set()
    min_ts = None
    if os.path.exists(ACCUM):
        with open(ACCUM, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        k = json.loads(line)
                        ts = int(k[0])
                        ts_set.add(ts)
                        if min_ts is None or ts < min_ts:
                            min_ts = ts
                    except Exception:
                        pass
    return ts_set, min_ts


def segment_covered(s, e, ts_set):
    """段内抽样判定是否已完整拉取 (5m 边界对齐)。
    必须同时满足: 段尾 K线在 + 头/中/尾采样在, 防止只拉大半就被误判为已覆盖"""
    n = 5
    for i in range(n):
        ts = int(s + (e - s) * (i + 1) / (n + 1))
        ts5 = ts - ts % 300000
        if ts5 not in ts_set:
            return False
    # 段尾必须覆盖: 段内最后一个 5m 对齐点
    tail = e - 300000 - (e - 300000) % 300000
    if tail not in ts_set:
        return False
    return True


def wait_for_unban():
    """启动前探测 fapi 是否被封禁(418 banned until); 是则睡到解封+60s"""
    import re
    for _ in range(10):
        try:
            data = fdc.fetch_klines(int(time.time() * 1000) - 600000, int(time.time() * 1000), limit=2)
            log("fapi 连通, 无需等待")
            return
        except Exception as exc:
            body = str(exc)
            m = re.search(r"banned until (\d{13})", body)
            if m:
                until_s = int(m.group(1)) / 1000
                wait = max(60, until_s - time.time() + 60)
                log(f"fapi 被封禁至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until_s))}, 等待 {wait:.0f}s...")
                time.sleep(min(wait, 600))
                continue
            log(f"探测 fapi 失败: {type(exc).__name__} {str(exc)[:80]}, 60s 后重试...")
            time.sleep(60)
    log("多次探测仍未连通, 继续尝试下载")


def main():
    check_single_instance()
    wait_for_unban()

    seg_ms = SEGMENT_DAYS * 24 * 3600 * 1000
    ts_set, min_ts = load_existing()
    log(f"已有累积数据: {len(ts_set)} 根, 起始 {time.strftime('%Y-%m-%d', time.localtime(min_ts/1000)) if min_ts else '-'}")

    segments = []
    s = START_MS
    while s < END_MS:
        e = min(s + seg_ms, END_MS)
        if not segment_covered(s, e, ts_set):
            segments.append((s, e))
        s = e + 1

    log(f"待拉段数: {len(segments)}, 并发: {WORKERS}")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_segment, s, e, ts_set): (s, e) for s, e in segments}
        for fut in cf.as_completed(futures):
            s, e = futures[fut]
            try:
                seg_klines = fut.result()
            except Exception as exc:
                log(f"  ✗ 段 {s} 失败: {type(exc).__name__} {exc}")
                seg_klines = []
            if seg_klines:
                save_segment(seg_klines)
                ts_set.update(k[0] for k in seg_klines)
            done += 1
            t1 = time.strftime("%Y-%m-%d", time.localtime(s / 1000))
            t2 = time.strftime("%Y-%m-%d", time.localtime(e / 1000))
            log(f"  ✓ 段{done}/{len(segments)}: {t1}~{t2} +{len(seg_klines)} 累计{len(ts_set)}")

    # 汇总所有累积数据, 排序去重落盘到新文件
    all_data = []
    with open(ACCUM, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    all_data.append(json.loads(line))
                except Exception:
                    pass
    seen = set()
    uniq = []
    for k in sorted(all_data, key=lambda k: k[0]):
        if k[0] not in seen:
            seen.add(k[0])
            uniq.append(k)
    log(f"全部完成, 共 {len(uniq)} 根 (累积 {len(all_data)}), 写入 {OUT_CSV}")
    if uniq:
        fdc.OUTPUT = OUT_CSV
        fdc.save(uniq)
    log("DONE")


if __name__ == "__main__":
    main()
