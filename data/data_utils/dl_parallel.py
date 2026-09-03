"""临时并发下载(增量落盘版): 分段并行拉取 5m 合约数据 (完成后删除)

设计:
  - 每段完成后立即追加到 data/temp/dl_accum.jsonl (进程中断不丢已拉数据)
  - 重新运行自动断点续传: 跳过 end 时间 <= 已有数据最大时间的段
  - 全部完成后排序+去重, 写入 data/futures/ETHUSDT-5m.csv
"""
import concurrent.futures as cf
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import fetch_data_contract as fdc  # 复用 fetch_klines / SYMBOL / OUTPUT

# 强制 5m 周期 + 5m 输出文件 (模块默认是 1h)
fdc.INTERVAL = "5m"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fdc.OUTPUT = os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m.csv")

WORKERS = 1          # 并发请求数 (用户要求: 拉数据不要并发; 旧版 8 并发触发 418 封禁, 串行+批间隔2s 最稳)
SEGMENT_DAYS = 90    # 每段天数 (5m: 90天=25920根 ≈ 18批)
BATCH_SLEEP = 2.0    # 每批间隔 (降低请求速率, 防 418/限流)
YEARS = 5            # 历史年限 (3 年已有, 补拉更早 2 年)
TEMP_DIR = os.path.join(BASE_DIR, "data", "temp")   # 下载中间产物统一放 data/temp/, 不污染主目录
os.makedirs(TEMP_DIR, exist_ok=True)
PROGRESS = os.path.join(TEMP_DIR, "dl_parallel_progress.txt")
ACCUM = os.path.join(TEMP_DIR, "dl_accum.jsonl")
ACCUM_LOCK = threading.Lock()
MUTEX = None


def check_single_instance():
    """单实例守卫: Windows 命名互斥量 (原子获取, 进程死亡自动释放, 无陈旧锁问题)。
    沙箱常把同一条命令启动两遍, 双实例会双倍流量+并发写同一文件"""
    global MUTEX
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, True, "Local\\dl_parallel_single")
    if not h:
        log("CreateMutex 失败, 继续")
        return
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("已有 dl_parallel 实例运行, 本实例退出")
        sys.exit(0)
    MUTEX = h  # 保持引用, 防止被 GC 释放导致互斥量失效


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


def fetch_segment(start_ms, end_ms):
    klines = []
    current = start_ms
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
                if code in (418, 429):
                    # 限流/封禁: 长退避 (60s), 之后重试
                    wait = 60 + attempt * 30
                else:
                    wait = 3 + attempt * 5
                if attempt < 5:
                    log(f"    段{time.strftime('%m-%d', time.localtime(start_ms/1000))} 重试{attempt+1}/6 ({type(last_exc).__name__}{code or ''}), 等{wait}s...")
                    time.sleep(wait)
        if data is None:
            raise RuntimeError(f"段重试耗尽: {last_exc}")
        if not data:
            break
        klines.extend(data)
        current = int(data[-1][0]) + 1
        if len(data) < 500:
            break
        time.sleep(BATCH_SLEEP)
    return klines


def load_existing():
    """读取已累积数据: 返回 (ts集合, 最小时间戳, 最大时间戳)"""
    ts_set = set()
    min_ts = None
    max_ts = 0
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
                        if ts > max_ts:
                            max_ts = ts
                    except Exception:
                        pass
    return ts_set, min_ts, max_ts


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
                now_s = time.time()
                wait = max(60, until_s - now_s + 60)
                log(f"fapi 被封禁至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until_s))}, 等待 {wait:.0f}s...")
                time.sleep(min(wait, 600))
                continue
            # 其他错误(SSLEOF 等): 等待后重试
            log(f"探测 fapi 失败: {type(exc).__name__} {str(exc)[:80]}, 60s 后重试...")
            time.sleep(60)
    log("多次探测仍未连通, 继续尝试下载")


def segment_covered(s, e, ts_set):
    """抽样检查段内是否已有完整数据 (不能用 min/max 跨度判断: 数据有空洞时会把未拉段误判为已覆盖)"""
    n = 5
    for i in range(n):
        ts = int(s + (e - s) * (i + 1) / (n + 1))
        ts5 = ts - ts % 300000  # 对齐到 5m 边界 (K线开市时间)
        if ts5 not in ts_set:
            return False
    return True


def main():
    check_single_instance()
    wait_for_unban()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(YEARS * 365 * 24 * 3600 * 1000)
    seg_ms = SEGMENT_DAYS * 24 * 3600 * 1000

    ts_set, min_ts, max_ts = load_existing()
    log(f"已有累积数据: {len(ts_set)} 根, {time.strftime('%Y-%m-%d', time.localtime(min_ts/1000)) if min_ts else '-'} ~ {time.strftime('%Y-%m-%d %H:%M', time.localtime(max_ts/1000))}")

    # 跳过已完整覆盖的段 (按段内抽样判定, 支持断点续传/补拉)
    segments = []
    s = start_ms
    while s < now_ms:
        e = min(s + seg_ms, now_ms)
        if not segment_covered(s, e, ts_set):
            segments.append((s, e))
        s = e + 1

    log(f"待拉段数: {len(segments)}, 并发: {WORKERS}")
    all_klines = list(ts_set)  # 仅作统计
    done = 0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_segment, s, e): (s, e) for s, e in segments}
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

    # 汇总所有累积数据, 排序去重落盘
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
    log(f"全部完成, 共 {len(uniq)} 根 (累积 {len(all_data)}), 开始落盘...")
    fdc.save(uniq)
    log("DONE")


if __name__ == "__main__":
    main()
