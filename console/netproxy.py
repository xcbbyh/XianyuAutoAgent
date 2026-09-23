"""
AI 模型调用走哪条网络线路

只给 AI 模型的调用用：闲鱼接口、闲鱼 WebSocket、浏览器登录都不经过这里，保持直连。
找代理的顺序：
1. 控制台里手动填的（「AI 模型」页面，填 direct 表示强制直连）
2. 环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY
3. Windows 系统代理（Clash 开了「系统代理」时会写在这里）
4. 本机常见代理软件端口：Clash 7890、Clash Verge 7897（端口开着才用）
都没有就直连。
"""
import os
import socket
import threading
import time
import urllib.request

from . import store

COMMON_PORTS = ((7890, "Clash"), (7897, "Clash Verge"))
_cache = {"at": 0, "value": None}
_lock = threading.Lock()
# 每个平台最近一次调用成功走的线路，给页面显示
last_route = {}


def _normalize(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _detect():
    manual = str(store.get_settings().get("ai_proxy") or "").strip()
    if manual.lower() in ("direct", "直连", "none"):
        return {"url": "", "source": "手动设置为直连"}
    if manual:
        return {"url": _normalize(manual), "source": "控制台手动设置"}
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(key):
            return {"url": _normalize(os.environ[key]), "source": f"环境变量 {key}"}
    try:
        system = urllib.request.getproxies()  # Windows 上会读取系统代理设置
    except Exception:
        system = {}
    url = system.get("https") or system.get("http")
    if url:
        return {"url": _normalize(url), "source": "Windows 系统代理"}
    for port, name in COMMON_PORTS:
        if _port_open(port):
            return {"url": f"http://127.0.0.1:{port}", "source": f"检测到本机 {name} 代理端口"}
    return {"url": "", "source": "没有检测到代理软件"}


def current(refresh=False):
    """当前 AI 调用要用的代理：{"url": "http://127.0.0.1:7890" 或 "", "source": "来源说明"}"""
    with _lock:
        if refresh or not _cache["value"] or time.time() - _cache["at"] > 30:
            try:
                _cache["value"] = _detect()
            except Exception as e:
                _cache["value"] = {"url": "", "source": f"检测代理出错：{e}"}
            _cache["at"] = time.time()
        return dict(_cache["value"])


def is_local(base_url):
    host = str(base_url or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.startswith("192.168.") or host.startswith("10.")


def routes(base_url):
    """这次调用依次尝试的线路：有代理时先走代理，连不上再直连；本地模型只直连"""
    if is_local(base_url):
        return [""]
    proxy = current()["url"]
    return [proxy, ""] if proxy else [""]


def route_label(proxy_url):
    return f"代理 {proxy_url}" if proxy_url else "直连"


def status():
    info = current(refresh=True)
    return {**info, "label": route_label(info["url"]), "last": dict(last_route)}
