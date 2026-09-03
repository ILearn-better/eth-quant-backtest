import json
import requests

out = {}
params = {"symbol": "ETHUSDT", "interval": "5m", "limit": 2}

def t(name, url, proxies=None, timeout=15):
    try:
        r = requests.get(url, params=params, proxies=proxies, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        try:
            out[name] = "OK " + str(r.status_code) + " ts=" + str(r.json()[0][0])
        except Exception:
            out[name] = "OK " + str(r.status_code) + " text=" + r.text[:120]
    except Exception as e:
        out[name] = "ERR " + type(e).__name__ + ": " + str(e)[:120]

# 官方公开数据 API (非交易 API, 通常独立限流)
t("dvs_fapi_direct", "https://data-api.binance.vision/fapi/v1/klines")
t("dvs_fapi_proxy", "https://data-api.binance.vision/fapi/v1/klines", {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
t("dvs_spot_direct", "https://data-api.binance.vision/api/v3/klines")
# 备用域名
t("fapi_api2_proxy", "https://fapi.binance.com/fapi/v1/klines", {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})

with open("net_test.txt", "w", encoding="utf-8") as f:
    f.write(json.dumps(out, ensure_ascii=False, indent=1))
print("done")
