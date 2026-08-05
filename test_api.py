"""快速测试 Binance API 连通性 + 下载少量数据"""
import requests
import time
import csv
import os

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ETHUSDT-1d-test.csv")
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

def test_binance():
    # 测试1: 获取 2026-01-01 至今的日线 (约 130 根, 数据量很小)
    print("测试 Binance API...")
    print("-" * 40)

    # 2026-01-01 00:00:00 UTC 的毫秒时间戳
    start_ms = 1767225600000
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": "ETHUSDT",
        "interval": "1d",
        "startTime": start_ms,
        "limit": 200,
    }

    try:
        print(f"请求: GET {url}")
        print(f"参数: symbol=ETHUSDT, interval=1d, 从2026-01-01起, limit=200")
        resp = requests.get(url, params=params, timeout=30)
        print(f"状态码: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ 成功! 获取 {len(data)} 根K线")

            if data:
                first_t = time.strftime('%Y-%m-%d', time.localtime(int(data[0][0])/1000))
                last_t = time.strftime('%Y-%m-%d', time.localtime(int(data[-1][0])/1000))
                print(f"范围: {first_t} ~ {last_t}")
                print(f"首根: O={data[0][1]} H={data[0][2]} L={data[0][3]} C={data[0][4]}")

                # 保存为测试文件
                headers = ["timestamp","open","high","low","close","volume",
                           "close_time","quote_volume","trades","taker_buy_base","taker_buy_quote"]
                with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(headers)
                    for k in data:
                        w.writerow([int(k[0]), round(float(k[1]),2), round(float(k[2]),2),
                                    round(float(k[3]),2), round(float(k[4]),2), round(float(k[5]),6),
                                    int(k[6]), round(float(k[7]),2), int(k[8]),
                                    round(float(k[9]),6), round(float(k[10]),6)])
                print(f"\n已保存到: {OUTPUT}")
        else:
            print(f"❌ HTTP {resp.status_code}: {resp.text[:200]}")

    except requests.exceptions.ConnectionError as e:
        print(f"❌ 连接失败 (ConnectionError): {e}")
        print("   → 可能需要检查代理/VPN 设置")
    except requests.exceptions.Timeout:
        print(f"❌ 请求超时 (30s)")
        print("   → 网络不通或代理太慢")
    except Exception as e:
        print(f"❌ 异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    test_binance()
