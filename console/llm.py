"""
多模型路由客户端

对外提供和 openai.OpenAI 相同的 client.chat.completions.create(...) 用法，
原有 Agent 代码不用改调用方式。调用时按顺序尝试已启用的平台和 Key：
Key 轮换使用，失败自动换下一个 Key / 下一个平台。
"""
import threading
import time
from types import SimpleNamespace

import httpx
from loguru import logger
from openai import OpenAI

from . import netproxy, providers

_clients = {}
_clients_lock = threading.Lock()


def _http_client(proxy_url):
    """只给 AI 调用用的 HTTP 客户端：有代理就明确走代理；直连时不读环境变量里的代理"""
    if not proxy_url:
        return httpx.Client(trust_env=False, timeout=40)
    try:
        return httpx.Client(proxy=proxy_url, trust_env=False, timeout=40)
    except TypeError:  # 旧版 httpx 参数名是 proxies
        return httpx.Client(proxies=proxy_url, trust_env=False, timeout=40)


def _client(base_url, api_key, proxy_url=""):
    cache_key = (base_url, api_key, proxy_url)
    with _clients_lock:
        if cache_key not in _clients:
            _clients[cache_key] = OpenAI(api_key=api_key or "none", base_url=base_url, timeout=40, max_retries=0,
                                         http_client=_http_client(proxy_url))
        return _clients[cache_key]


def _is_network_error(e):
    name = type(e).__name__
    return getattr(e, "status_code", None) is None and ("Timeout" in name or "Connection" in name)


class AIConnectionError(Exception):
    """所有线路（代理、直连）都连不上；attempts 是每条线路的 (线路, 异常)"""

    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("；".join(f"{netproxy.route_label(url)}：{type(e).__name__}: {e}" for url, e in attempts))


def _call_once(client, params):
    """遇到平台不支持的参数时去掉该参数重试"""
    for _ in range(3):
        try:
            return client.chat.completions.create(**params)
        except Exception as e:
            message = str(e).lower()
            if "top_p" in message and "top_p" in params:
                params.pop("top_p")
            elif "temperature" in message and "temperature" in params:
                params.pop("temperature")
            elif "max_tokens" in message and "max_tokens" in params:
                params["max_completion_tokens"] = params.pop("max_tokens")
            else:
                raise
    return client.chat.completions.create(**params)


def call_provider(provider, api_key, **kwargs):
    """
    调用单个平台。网络线路：有本机代理先走代理，连不上再直连（只针对 AI 调用，闲鱼和浏览器不受影响）。
    """
    params = dict(kwargs)
    params["model"] = provider["model"]
    extra = params.pop("extra_body", None)
    if extra and provider.get("supports_search"):
        params["extra_body"] = extra
    attempts = []
    for proxy_url in netproxy.routes(provider["base_url"]):
        try:
            resp = _call_once(_client(provider["base_url"], api_key, proxy_url), dict(params))
            netproxy.last_route[provider.get("id") or provider["name"]] = netproxy.route_label(proxy_url)
            if attempts:
                logger.info(f"{provider['name']} 走代理连不上，已改为{netproxy.route_label(proxy_url)}调用成功")
            return resp
        except Exception as e:
            if not _is_network_error(e):
                raise
            logger.warning(f"{provider['name']} {netproxy.route_label(proxy_url)}连不上：{type(e).__name__}: {e}")
            attempts.append((proxy_url, e))
    if len(attempts) == 1:
        raise attempts[0][1]
    raise AIConnectionError(attempts)


# 限流（429）和平台临时出错（5xx）时同一个 Key 等一会再试，最多再试 2 次
# 免费档常见「每秒 1 次」的限制：AI 助手查资料时会连着调用几次，不等一下就会被拒
RETRY_WAITS = (2, 5)


def _retry_wait(e, attempt):
    """这次失败值得同一个 Key 再试就返回要等的秒数，否则返回 None"""
    code = getattr(e, "status_code", None)
    name = type(e).__name__
    if code == 429 or (code and code >= 500):
        if attempt >= len(RETRY_WAITS):
            return None
        wait = RETRY_WAITS[attempt]
        try:
            header = e.response.headers.get("retry-after")
            if header:
                wait = min(max(float(header), wait), 15)
        except Exception:
            pass
        return wait
    if isinstance(e, AIConnectionError):
        return None  # 代理和直连都已经试过了
    if ("Timeout" in name or "Connection" in name) and attempt == 0:
        return 1
    return None


def call_with_retry(provider, api_key, **kwargs):
    attempt = 0
    while True:
        try:
            return call_provider(provider, api_key, **kwargs)
        except Exception as e:
            wait = _retry_wait(e, attempt)
            if wait is None:
                raise
            logger.info(f"{provider['name']} 暂时不可用（{str(e)[:80]}），{wait} 秒后重试")
            time.sleep(wait)
            attempt += 1


class RoutedClient:
    def __init__(self, fallback_client=None):
        self.fallback_client = fallback_client
        self._round_robin = {}
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _next_index(self, provider_id, size):
        with self._lock:
            index = self._round_robin.get(provider_id, 0) % size
            self._round_robin[provider_id] = index + 1
            return index

    def create(self, **kwargs):
        candidates = providers.active_providers()
        if not candidates:
            if self.fallback_client:
                return self.fallback_client.chat.completions.create(**kwargs)
            raise RuntimeError("没有可用的 AI 模型，请在控制台「AI 模型」里配置并启用")

        errors = []
        for provider in candidates:
            keys = provider["api_keys"]
            start = self._next_index(provider["id"], len(keys))
            for offset in range(len(keys)):
                index = (start + offset) % len(keys)
                try:
                    resp = call_with_retry(provider, keys[index], **kwargs)
                    if errors:
                        logger.info(f"已切换到 {provider['name']} 第 {index + 1} 个 Key 调用成功")
                    return resp
                except Exception as e:
                    logger.warning(f"{provider['name']} 第 {index + 1} 个 Key 调用失败：{str(e)[:300]}")
                    errors.append(f"{provider['name']} 第 {index + 1} 个 Key：{providers.explain_error(e, provider)}")
        raise RuntimeError("所有 AI 模型都调用失败。" + "；".join(errors))
