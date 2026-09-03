"""下载 ETHUSDT 合约(USDⓈ-M Futures) K线数据 - Binance Futures API

数据源: datasource.FUTURES (fapi.binance.com)
用法:
  python fetch_data_contract.py [interval] [years]
    interval: 1h(默认) / 5m / 15m / 1d ...
    years:    近N年数据(默认 5)
输出: data/futures/ETHUSDT-<interval>.csv (独立目录, 不污染现货 data/)
"""

import os as _os, sys as _sys
_ROOT_ = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _ROOT_)

import csv
import os
import time
import sys

# Windows 终端/重定向 UTF-8 兼容 (print 含 ✓❌ 等字符, 避免 GBK 编码崩溃)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datasource import FUTURES

SYMBOL = "ETHUSDT"
INTERVAL = sys.argv[1] if len(sys.argv) > 1 else "1h"
YEARS = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "futures", f"ETHUSDT-{INTERVAL}.csv")
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)


_session = None
def _get_session():
    """复用连接, 减少 SSL 握手 (频繁 SSLError 的根因是每批新建连接)"""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def fetch_klines(start_time, end_time, limit=1500):
    import requests
    url = FUTURES["rest_kline"]
    params = {"symbol": SYMBOL, "interval": INTERVAL, "startTime": start_time, "endTime": end_time, "limit": limit}
    # 直连优先, 失败走代理 (与 live_trader_contract._rest_get 策略一致)
    try:
        resp = _get_session().get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        resp = requests.get(url, params=params, timeout=30, proxies=proxies)
        resp.raise_for_status()
        return resp.json()


def download():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(YEARS * 365 * 24 * 3600 * 1000)
    all_klines = []
    current = start_ms
    batch = 0
    progress_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dl_progress.txt")
    print(f"{'='*60}\n下载 {SYMBOL} {FUTURES['name']} {INTERVAL} (近{YEARS:g}年)\n数据源: {FUTURES['rest_kline']}\n目标: {time.strftime('%Y-%m-%d', time.localtime(start_ms/1000))} ~ now\n{'='*60}")

    def log_progress(msg):
        print(msg)
        try:
            with open(progress_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    while current < now_ms:
        batch += 1
        for attempt in range(8):
            try:
                data = fetch_klines(current, now_ms)
                break
            except Exception as e:
                wait = min(3 + attempt * 4, 30)
                if attempt < 7:
                    log_progress(f"  批次{batch} 重试{attempt+1}/8: 等{wait}s... ({type(e).__name__})")
                    time.sleep(wait)
                else:
                    raise
        if not data:
            break
        all_klines.extend(data)
        current = int(data[-1][0]) + 1
        t1 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(data[0][0])/1000))
        t2 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(data[-1][0])/1000))
        log_progress(f"  ✓ 批次{batch}: +{len(data)} | {t1} ~ {t2} | 总计 {len(all_klines)}")
        if len(data) < 500:
            break
        time.sleep(0.15)
    return all_klines


def save(klines):
    headers = ["timestamp","open","high","low","close","volume","close_time","quote_volume","trades","taker_buy_base","taker_buy_quote"]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for k in klines:
            w.writerow([int(k[0]), round(float(k[1]),2), round(float(k[2]),2), round(float(k[3]),2), round(float(k[4]),2), round(float(k[5]),6), int(k[6]), round(float(k[7]),2), int(k[8]), round(float(k[9]),6), round(float(k[10]),6)])
    t1 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(klines[0][0])/1000))
    t2 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(klines[-1][0])/1000))
    days = (int(klines[-1][0]) - int(klines[0][0])) / 86400000
    print(f"\n{'='*60}\n✅ 完成! {len(klines)}根 → {OUTPUT}\n   {t1} ~ {t2} (~{days:.0f}天 / {days*24:.0f}h)")


if __name__ == "__main__":
    try:
        klines = download()
        if len(klines) > 100:
            save(klines)
        else:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n中断")
    except Exception as e:
        print(f"\n❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
