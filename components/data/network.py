"""网络工具: HTTP 代理 + 微信推送通知

归纳自项目中重复的网络工具函数:

    get_proxy_opener:  live_trader.py / live_trader_contract.py /
                       dashboard_server.py / paper_trading.py  (4处重复)
    wx_notify:         live_trader.py / live_trader_contract.py / paper_trading.py  (3处重复)

封装为 ProxyClient (代理请求) 与 WeChatNotifier (Server酱微信推送) 两个类.
本文件不改原文件, 供新代码引用.

用法:
    from components.data.network import ProxyClient, WeChatNotifier
    opener = ProxyClient.opener()                          # 获取带代理的 opener
    WeChatNotifier.send("标题", "内容")                    # 推送微信 (key 从环境变量读)
    notifier = WeChatNotifier(sendkey="SCTxxxx")           # 或显式传入 key
    notifier.send_async("标题", "内容")                    # 异步发送 (独立线程, 不阻塞)
"""
import os
import ssl
import threading
import urllib.request
import urllib.parse


class ProxyClient:
        """HTTP/HTTPS 代理客户端

        项目通过本地代理 (Clash 等) 访问 Binance API, 默认 127.0.0.1:7897.
        提供 opener 供 urllib 使用, 也支持 requests 风格的 proxies 字典.
        """

        HOST = "127.0.0.1"
        PORT = 7897

        @classmethod
        def opener(cls, host=None, port=None):
                """构建带代理的 urllib opener

                Returns:
                    urllib.request.OpenerDirector, 用 opener.open(url) 发请求
                """
                h = host or cls.HOST
                p = port or cls.PORT
                proxy = urllib.request.ProxyHandler({
                        "http": f"http://{h}:{p}",
                        "https": f"http://{h}:{p}",
                })
                return urllib.request.build_opener(proxy)

        @classmethod
        def proxies(cls, host=None, port=None):
                """返回 requests 库风格的 proxies 字典"""
                h = host or cls.HOST
                p = port or cls.PORT
                return {"http": f"http://{h}:{p}", "https": f"http://{h}:{p}"}


class WeChatNotifier:
        """微信通知推送 (基于 Server酱 sct.ftqq.com)

        SendKey 获取: 登录 https://sct.ftqq.com/ 后在「SendKey」页面复制.
        优先级: 构造参数 sendkey > 环境变量 WX_SENDKEY.
        """

        API_BASE = "https://sctapi.ftqq.com"
        TIMEOUT = 10

        def __init__(self, sendkey=None):
                self.sendkey = sendkey or os.environ.get("WX_SENDKEY", "")

        @classmethod
        def send(cls, title, content, sendkey=None):
                """同步发送微信通知 (阻塞, 失败仅打印不抛异常)

                Args:
                    title:   消息标题 (短)
                    content: 消息正文 (支持 Markdown)
                    sendkey: 可选, 不传则从环境变量 WX_SENDKEY 读
                Returns:
                    True 发送成功 / False 失败或无 key
                """
                key = sendkey or os.environ.get("WX_SENDKEY", "")
                if not key:
                        return False
                try:
                        url = f"{cls.API_BASE}/{key}.send"
                        data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
                        ctx = ssl._create_unverified_context()
                        req = urllib.request.Request(url, data=data, method="POST",
                                headers={"User-Agent": "Mozilla/5.0"})
                        urllib.request.urlopen(req, timeout=cls.TIMEOUT, context=ctx)
                        return True
                except Exception as e:
                        print(f"  ⚠️ 微信通知发送失败: {e}")
                        return False

        def send_async(self, title, content):
                """异步发送 (独立线程, 不阻塞调用方)

                信号触发场景下应使用此方法, 避免 WS/主线程被网络请求卡住.
                Returns: threading.Thread (已 start)
                """
                t = threading.Thread(target=self.send, args=(title, content, self.sendkey),
                                      daemon=True, name="wx-notify")
                t.start()
                return t


# ============ 模块级接口函数 (兼容旧调用习惯) ============
def get_proxy_opener(host=None, port=None):
        """兼容旧名: 返回带代理的 urllib opener"""
        return ProxyClient.opener(host, port)


def wx_notify(title, content, sendkey=None):
        """兼容旧名: 同步发送微信通知"""
        return WeChatNotifier.send(title, content, sendkey)
