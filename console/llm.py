"""
多模型路由客户端

对外提供和 openai.OpenAI 相同的 client.chat.completions.create(...) 用法，
原有 Agent 代码不用改调用方式。调用时按顺序尝试已启用的平台和 Key：
Key 轮换使用，失败自动换下一个 Key / 下一个平台。
"""
import threading
from types import SimpleNamespace

from loguru import logger
from openai import OpenAI

from . import providers

_clients = {}
_clients_lock = threading.Lock()


def _client(base_url, api_key):
    cache_key = (base_url, api_key)
    with _clients_lock:
        if cache_key not in _clients:
            _clients[cache_key] = OpenAI(api_key=api_key or "none", base_url=base_url, timeout=40, max_retries=0)
        return _clients[cache_key]


def call_provider(provider, api_key, **kwargs):
    """调用单个平台；遇到平台不支持的参数时去掉该参数重试"""
    params = dict(kwargs)
    params["model"] = provider["model"]
    extra = params.pop("extra_body", None)
    if extra and provider.get("supports_search"):
        params["extra_body"] = extra
    client = _client(provider["base_url"], api_key)
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
                    resp = call_provider(provider, keys[index], **kwargs)
                    if errors:
                        logger.info(f"已切换到 {provider['name']} 第 {index + 1} 个 Key 调用成功")
                    return resp
                except Exception as e:
                    logger.warning(f"{provider['name']} 第 {index + 1} 个 Key 调用失败：{str(e)[:200]}")
                    errors.append(f"{provider['name']}#{index + 1}")
        raise RuntimeError("所有 AI 模型都调用失败：" + "、".join(errors))
