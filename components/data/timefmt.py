"""时间格式化工具

归纳自 live_trader.py / live_trader_contract.py 中重复的时间格式化函数
(各 2 处: ts_to_str / ts_to_short / now).

用法:
    from components.data.timefmt import ts_to_str, ts_to_short, now
    ts_to_str(1786093200000)   # "2026-08-07 17:00:00"
    ts_to_short(1786093200000) # "08-07 17:00"
    now()                       # "17:01:23"  (当前本地时间)
"""
import datetime


def ts_to_str(ts_ms):
        """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'"""
        return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def ts_to_short(ts_ms):
        """毫秒时间戳 → 'MM-DD HH:MM' (用于图表轴/日志紧凑显示)"""
        return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")


def now():
        """当前本地时间 → 'HH:MM:SS' (用于日志前缀)"""
        return datetime.datetime.now().strftime("%H:%M:%S")
