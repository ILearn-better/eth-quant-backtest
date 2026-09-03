# -*- coding: utf-8 -*-
"""daily_log — 按天轮转的双写日志工具 (控制台 + 文件)

用法(在服务入口 __main__ 最前面调用, 之后所有 print / traceback 自动双写):

    import daily_log
    daily_log.setup("spot")        # → logs/spot/YYYY-MM-DD.log
    daily_log.setup("contract")    # → logs/contract/YYYY-MM-DD.log
    daily_log.setup("alpha")       # → logs/alpha/YYYY-MM-DD.log

规则:
  - 日志根目录: <项目根>/logs/
  - 每个模块一个子文件夹(spot/contract/alpha/...), 模块名任意, 目录自动创建
  - 文件名 = 当天日期 YYYY-MM-DD.log; 每个文件只记录一天
  - 进程跨天持续运行时, 写入时检测到日期变化自动关闭旧文件、新建当天文件
  - 既保留控制台输出(不改变现有 print 行为), 又实时落盘(每行 flush)

不依赖第三方库, 不干扰 uvicorn/fastapi 自身的 logging 体系(它们走标准 logging,
不受 stdout 重定向影响)。
"""
import datetime
import os
import sys
import threading

_LOGS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOCK = threading.Lock()
_INSTALLED = {"stdout": False, "stderr": False}
_HANDLER = None  # 当前进程唯一的 _DayFileHandler


class _DayFileHandler:
    """按天文件句柄: 写入前检查日期, 跨天自动换新文件"""

    def __init__(self, module):
        self.module = module
        self.dir = os.path.join(_LOGS_ROOT, module)
        os.makedirs(self.dir, exist_ok=True)
        self._f = None
        self._day = None
        self._open_today()

    # ---- 文件管理 ----
    def _today(self):
        return datetime.date.today()

    def _path_for(self, day):
        return os.path.join(self.dir, day.isoformat() + ".log")

    def _open_today(self):
        self.close()
        self._day = self._today()
        self._f = open(self._path_for(self._day), "a", encoding="utf-8")

    def _ensure_open(self):
        """若跨天则关闭旧文件, 打开当天新文件(不删除/滚动旧文件, 旧文件保持当天内容)"""
        today = self._today()
        if self._f is None or self._day != today:
            self._open_today()

    # ---- 写入 ----
    def write(self, text):
        if not text:
            return
        try:
            with _LOCK:
                self._ensure_open()
                self._f.write(text)
                self._f.flush()
        except Exception:
            pass  # 日志落盘失败不能影响业务

    def close(self):
        if self._f is not None:
            try:
                self._f.flush()
                self._f.close()
            except Exception:
                pass
            self._f = None


class _Tee:
    """把原控制台流与按天文件流串起来: 每行先落盘、再打控制台"""

    def __init__(self, console, handler, name):
        self._console = console   # 原 sys.stdout / sys.stderr
        self._handler = handler   # _DayFileHandler
        self._name = name         # 'stdout' / 'stderr'
        self.encoding = getattr(console, "encoding", None) or "utf-8"
        self.errors = getattr(console, "errors", None) or "replace"

    def write(self, text):
        if not text:
            return
        self._handler.write(text)          # 先落盘(内部已加锁)
        try:
            self._console.write(text)      # 保持原控制台行为
            self._console.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._console.flush()
        except Exception:
            pass
        try:
            with _LOCK:
                if self._handler._f is not None:
                    self._handler._f.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._console.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._console.fileno()

    def reconfigure(self, *args, **kwargs):
        # 兼容启动早期对 sys.stdout 调 reconfigure(utf-8) 的旧代码
        return None

    def __getattr__(self, item):
        # 未知属性代理回原控制台流, 避免第三方库因缺属性报错
        return getattr(self._console, item)


def setup(module="default"):
    """将本进程的 sys.stdout / sys.stderr 双写到 logs/<module>/<当天日期>.log

    返回 None。重复调用只会生效一次(幂等); 若要切换模块, 先调用 teardown()。
    """
    global _HANDLER
    if _HANDLER is not None and _HANDLER.module == module and all(_INSTALLED.values()):
        return
    teardown()
    _HANDLER = _DayFileHandler(module)
    if not _INSTALLED["stdout"]:
        sys.stdout = _Tee(sys.stdout, _HANDLER, "stdout")
        _INSTALLED["stdout"] = True
    if not _INSTALLED["stderr"]:
        sys.stderr = _Tee(sys.stderr, _HANDLER, "stderr")
        _INSTALLED["stderr"] = True


def teardown():
    """恢复原始控制台流并关闭文件句柄"""
    global _HANDLER
    if _INSTALLED["stdout"] and isinstance(sys.stdout, _Tee):
        sys.stdout = sys.stdout._console
        _INSTALLED["stdout"] = False
    if _INSTALLED["stderr"] and isinstance(sys.stderr, _Tee):
        sys.stderr = sys.stderr._console
        _INSTALLED["stderr"] = False
    if _HANDLER is not None:
        _HANDLER.close()
        _HANDLER = None


def current_log_file():
    """返回当前正在写入的日志文件绝对路径(排查用), 未 setup 时返回 None"""
    if _HANDLER is None or _HANDLER._f is None:
        return None
    return os.path.abspath(_HANDLER._f.name)


# 进程退出时关闭文件, 确保内容完整落盘
import atexit
atexit.register(teardown)
