"""历史 K 线数据下载工具

归纳自 fetch_data.py / fetch_data_contract.py —— 两者实现几乎完全相同, 仅 URL
与输出目录不同. 本类统一封装, 通过 market 参数切换现货/合约, 消除重复.

输出 CSV 列:
    timestamp, open, high, low, close, volume, close_time,
    quote_volume, trades, taker_buy_base, taker_buy_quote

用法:
    from components.data.fetcher import KlineFetcher
    fetcher = KlineFetcher(market="futures")          # 合约
    fetcher.download_and_save(years=5)                # 下载近5年并存 CSV
    # 或分步:
    klines = fetcher.download(years=5); fetcher.save(klines)
"""
import csv
import os
import time

from components.data.datasource import DataSource


class KlineFetcher:
        """历史 K 线下载器 (现货/合约通用)

        通过 Binance REST API 分批拉取 K 线, 支持重试, 输出标准 CSV.
        现货存 data/, 合约存 data/futures/ (独立目录互不污染).
        """

        SYMBOL = "ETHUSDT"
        INTERVAL = "1h"
        BATCH_LIMIT = 1000
        MAX_RETRIES = 8
        PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
        CSV_HEADERS = ["timestamp", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]

        def __init__(self, market="spot", output_path=None):
                """
                Args:
                    market:      "spot" 现货 / "futures" 合约
                    output_path: CSV 输出路径; 默认按市场自动选择
                                 现货: data/ETHUSDT-1h.csv
                                 合约: data/futures/ETHUSDT-1h.csv
                """
                self.market = market
                self.cfg = DataSource.get(market)
                self.rest_url = self.cfg["rest_kline"]
                if output_path is None:
                        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                        sub = "futures" if market == "futures" else ""
                        data_dir = os.path.join(base, "data", sub) if sub else os.path.join(base, "data")
                        output_path = os.path.join(data_dir, "ETHUSDT-1h.csv")
                self.output_path = output_path
                os.makedirs(os.path.dirname(self.output_path), exist_ok=True)

        def fetch_klines(self, start_time, end_time, limit=1000):
                """单次 REST 请求拉取一批 K 线 (最多 limit 根)"""
                import requests
                params = {"symbol": self.SYMBOL, "interval": self.INTERVAL,
                          "startTime": start_time, "endTime": end_time, "limit": limit}
                resp = requests.get(self.rest_url, params=params, timeout=60, proxies=self.PROXY)
                resp.raise_for_status()
                return resp.json()

        def download(self, years=5):
                """分批下载近 N 年 K 线, 返回完整 klines 列表

                自动翻页 (用最后一条 K 线的 timestamp+1 作为下一批起点),
                每批失败按指数退避重试最多 8 次.
                """
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - years * 365 * 24 * 3600 * 1000
                all_klines = []
                current = start_ms
                batch = 0
                print(f"{'='*60}\n下载 {self.SYMBOL} {self.cfg['name']} {self.INTERVAL} (近{years}年)\n"
                      f"数据源: {self.rest_url}\n目标: "
                      f"{time.strftime('%Y-%m-%d', time.localtime(start_ms/1000))} ~ now\n{'='*60}")

                while current < now_ms:
                        batch += 1
                        data = None
                        for attempt in range(self.MAX_RETRIES):
                                try:
                                        data = self.fetch_klines(current, now_ms)
                                        break
                                except Exception as e:
                                        wait = min(3 + attempt * 4, 30)
                                        if attempt < self.MAX_RETRIES - 1:
                                                print(f"  批次{batch} 重试{attempt+1}/{self.MAX_RETRIES}: 等{wait}s... ({type(e).__name__})")
                                                time.sleep(wait)
                                        else:
                                                raise
                        if not data:
                                break
                        all_klines.extend(data)
                        current = int(data[-1][0]) + 1
                        t1 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(data[0][0]) / 1000))
                        t2 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(data[-1][0]) / 1000))
                        print(f"  ✓ 批次{batch}: +{len(data)} | {t1} ~ {t2} | 总计 {len(all_klines)}")
                        if len(data) < 500:
                                break
                        time.sleep(0.2)
                return all_klines

        def save(self, klines):
                """将 klines 写入 CSV (self.output_path)"""
                with open(self.output_path, "w", newline="", encoding="utf-8") as f:
                        w = csv.writer(f)
                        w.writerow(self.CSV_HEADERS)
                        for k in klines:
                                w.writerow([int(k[0]), round(float(k[1]), 2), round(float(k[2]), 2),
                                            round(float(k[3]), 2), round(float(k[4]), 2), round(float(k[5]), 6),
                                            int(k[6]), round(float(k[7]), 2), int(k[8]),
                                            round(float(k[9]), 6), round(float(k[10]), 6)])
                t1 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(klines[0][0]) / 1000))
                t2 = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(klines[-1][0]) / 1000))
                days = (int(klines[-1][0]) - int(klines[0][0])) / 86400000
                print(f"\n{'='*60}\n✅ 完成! {len(klines)}根 → {self.output_path}\n"
                      f"   {t1} ~ {t2} (~{days:.0f}天 / {days*24:.0f}h)")

        def download_and_save(self, years=5, min_bars=100):
                """一键下载并保存 (含最小根数校验)"""
                klines = self.download(years=years)
                if len(klines) > min_bars:
                        self.save(klines)
                        return self.output_path
                raise RuntimeError(f"下载根数过少 ({len(klines)} ≤ {min_bars}), 未保存")
