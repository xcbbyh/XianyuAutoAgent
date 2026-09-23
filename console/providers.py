"""
AI 模型接入：内置常用平台，所有平台都走 OpenAI 兼容接口。

每个平台可以填多个 API Key（轮换使用，某个 Key 失败自动换下一个），
多个平台同时启用时按「默认 > 优先级」顺序故障转移。
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import store

CATEGORIES = {
    "domestic": "国内模型",
    "overseas": "海外模型",
    "aggregator": "聚合平台",
    "local": "本地模型",
    "custom": "自定义",
}

# supports_search：是否支持百炼的联网搜索参数（技术专家会用到）
PRESETS = [
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "category": "domestic",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-max",
     "supports_search": 1, "key_url": "https://bailian.console.aliyun.com/",
     "description": "项目默认模型，中文客服效果好，支持联网搜索；qwen-plus 更便宜"},
    {"id": "deepseek", "name": "DeepSeek", "category": "domestic",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "key_url": "https://platform.deepseek.com/api_keys",
     "description": "性价比高，中文能力强，适合日常客服和议价"},
    {"id": "kimi", "name": "Kimi（月之暗面）", "category": "domestic",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
     "key_url": "https://platform.moonshot.cn/console/api-keys",
     "description": "长文本理解好，回复自然"},
    {"id": "zhipu", "name": "智谱 GLM", "category": "domestic",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
     "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
     "description": "glm-4-flash 免费可用，适合做备用模型"},
    {"id": "doubao", "name": "豆包（火山方舟）", "category": "domestic",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-1-5-pro-32k-250115",
     "key_url": "https://console.volcengine.com/ark",
     "description": "字节跳动出品，响应快、价格低；模型名可填方舟里的接入点 ID"},
    {"id": "hunyuan", "name": "腾讯混元", "category": "domestic",
     "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "model": "hunyuan-turbos-latest",
     "key_url": "https://console.cloud.tencent.com/hunyuan/api-key",
     "description": "腾讯云大模型，有免费额度"},
    {"id": "qianfan", "name": "百度千帆（文心）", "category": "domestic",
     "base_url": "https://qianfan.baidubce.com/v2", "model": "ernie-4.5-turbo-32k",
     "key_url": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
     "description": "百度文心大模型"},
    {"id": "siliconflow", "name": "硅基流动", "category": "aggregator",
     "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct",
     "key_url": "https://cloud.siliconflow.cn/account/ak",
     "description": "国内聚合平台，一个 Key 用多家开源模型，部分模型免费"},
    {"id": "openrouter", "name": "OpenRouter", "category": "aggregator",
     "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini",
     "key_url": "https://openrouter.ai/keys",
     "description": "海外聚合平台，一个 Key 调用几百个模型（需海外网络）"},
    {"id": "openai", "name": "OpenAI", "category": "overseas",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
     "key_url": "https://platform.openai.com/api-keys",
     "description": "GPT 系列（需海外网络）"},
    {"id": "claude", "name": "Anthropic Claude", "category": "overseas",
     "base_url": "https://api.anthropic.com/v1/", "model": "claude-haiku-4-5-20251001",
     "key_url": "https://console.anthropic.com/settings/keys",
     "description": "Claude 系列，通过官方 OpenAI 兼容接口接入（需海外网络）"},
    {"id": "gemini", "name": "Google Gemini", "category": "overseas",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.5-flash",
     "key_url": "https://aistudio.google.com/apikey",
     "description": "有免费额度，速度快（需海外网络）"},
    {"id": "grok", "name": "xAI Grok", "category": "overseas",
     "base_url": "https://api.x.ai/v1", "model": "grok-3-mini",
     "key_url": "https://console.x.ai/",
     "description": "马斯克 xAI 出品（需海外网络）"},
    {"id": "groq", "name": "Groq", "category": "overseas",
     "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-20b",
     "key_url": "https://console.groq.com/keys",
     "description": "推理速度极快，有免费额度（需海外网络）"},
    {"id": "mistral", "name": "Mistral", "category": "overseas",
     "base_url": "https://api.mistral.ai/v1", "model": "ministral-8b-latest",
     "key_url": "https://console.mistral.ai/api-keys",
     "description": "欧洲模型，有免费额度（需海外网络）"},
    {"id": "ollama", "name": "Ollama 本地模型", "category": "local",
     "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
     "key_url": "https://ollama.com/download",
     "description": "在自己电脑上跑模型，不花钱，需要较好的显卡；API Key 随便填"},
]

EDITABLE_FIELDS = ("name", "base_url", "model", "priority", "enabled", "supports_search", "description")


def ensure_presets():
    """首次运行时写入内置平台；并把旧版 .env 里的 API Key 迁移过来"""
    existing = {r["id"] for r in store.rows("SELECT id FROM providers")}
    with store.db() as conn:
        for index, p in enumerate(PRESETS):
            if p["id"] in existing:
                continue
            conn.execute(
                "INSERT INTO providers (id, name, category, description, base_url, model, supports_search, "
                "key_url, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["id"], p["name"], p["category"], p["description"], p["base_url"], p["model"],
                 p.get("supports_search", 0), p["key_url"], (index + 1) * 10),
            )
    _migrate_env_key()


def _migrate_env_key():
    if store.row("SELECT id FROM providers WHERE api_keys != '[]'"):
        return
    from dotenv import dotenv_values

    env = dotenv_values(store.ENV_PATH) if os.path.exists(store.ENV_PATH) else {}
    key = (env.get("API_KEY") or "").strip()
    if not key or "apikey" in key.lower() or "百炼" in key:
        return
    base_url = (env.get("MODEL_BASE_URL") or "").rstrip("/")
    target = next((p for p in PRESETS if p["base_url"].rstrip("/") == base_url), None)
    provider_id = target["id"] if target else "qwen"
    fields = {"api_keys": json.dumps([key]), "enabled": 1, "is_default": 1}
    if env.get("MODEL_NAME"):
        fields["model"] = env["MODEL_NAME"]
    if not target and base_url:
        fields["base_url"] = base_url
    _update(provider_id, fields)


def _update(provider_id, fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    store.execute(f"UPDATE providers SET {sets} WHERE id = ?", (*fields.values(), provider_id))


def _mask(key):
    return key if len(key) <= 8 else f"{key[:4]}****{key[-4:]}"


def _decode(r):
    r = dict(r)
    try:
        r["api_keys"] = json.loads(r["api_keys"] or "[]")
    except ValueError:
        r["api_keys"] = []
    return r


def list_providers():
    """给前端用：Key 只返回打码后的样子"""
    result = []
    for r in store.rows("SELECT * FROM providers ORDER BY is_default DESC, priority, name"):
        r = _decode(r)
        r["key_count"] = len(r["api_keys"])
        r["masked_keys"] = [_mask(k) for k in r["api_keys"]]
        del r["api_keys"]
        r["category_label"] = CATEGORIES.get(r["category"], r["category"])
        try:
            r["last_test"] = json.loads(r["last_test"]) if r["last_test"] else None
        except ValueError:
            r["last_test"] = None
        result.append(r)
    return result


def active_providers():
    """机器人调用顺序：已启用且有 Key，默认平台最先，其余按优先级"""
    return [
        _decode(r)
        for r in store.rows(
            "SELECT * FROM providers WHERE enabled = 1 AND api_keys != '[]' ORDER BY is_default DESC, priority, name"
        )
    ]


def get_provider(provider_id):
    r = store.row("SELECT * FROM providers WHERE id = ?", (provider_id,))
    if not r:
        raise ValueError("模型平台不存在")
    return _decode(r)


def save_provider(payload):
    """保存平台配置。new_keys 追加 Key；remove_key_indexes 删除指定序号的 Key"""
    provider = get_provider(payload.get("id", ""))
    fields = {}
    for key in EDITABLE_FIELDS:
        if key in payload:
            value = payload[key]
            if key in ("priority",):
                value = int(value or 0)
            elif key in ("enabled", "supports_search"):
                value = 1 if value else 0
            else:
                value = str(value).strip()
            fields[key] = value
    if "base_url" in fields and not fields["base_url"]:
        raise ValueError("接口地址不能为空")
    if "model" in fields and not fields["model"]:
        raise ValueError("模型名称不能为空")

    keys = provider["api_keys"]
    remove = {int(i) for i in payload.get("remove_key_indexes", [])}
    keys = [k for i, k in enumerate(keys) if i not in remove]
    for k in str(payload.get("new_keys", "")).splitlines():
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    fields["api_keys"] = json.dumps(keys)
    if fields.get("enabled") and not keys:
        raise ValueError("请先填写至少一个 API Key 再启用")
    _update(provider["id"], fields)
    # 还没有默认平台时，第一个启用的平台自动成为默认
    if fields.get("enabled") and not store.row("SELECT id FROM providers WHERE is_default = 1 AND enabled = 1"):
        set_default(provider["id"])


def set_default(provider_id):
    provider = get_provider(provider_id)
    if not provider["api_keys"]:
        raise ValueError("请先配置 API Key")
    with store.db() as conn:
        conn.execute("UPDATE providers SET is_default = 0")
        conn.execute("UPDATE providers SET is_default = 1, enabled = 1 WHERE id = ?", (provider_id,))


def create_custom(payload):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("请填写平台名称")
    if not str(payload.get("base_url", "")).strip() or not str(payload.get("model", "")).strip():
        raise ValueError("接口地址和模型名称不能为空")
    if payload.get("enabled") and not str(payload.get("new_keys", "")).strip():
        raise ValueError("请先填写至少一个 API Key 再启用")
    provider_id = f"custom_{int(time.time() * 1000)}"
    store.execute(
        "INSERT INTO providers (id, name, category, description, base_url, model, custom, priority) "
        "VALUES (?, ?, 'custom', ?, ?, ?, 1, 500)",
        (provider_id, name, "自定义 OpenAI 兼容接口",
         str(payload.get("base_url", "")).strip(), str(payload.get("model", "")).strip()),
    )
    save_provider({**payload, "id": provider_id})
    return provider_id


def delete_custom(provider_id):
    provider = get_provider(provider_id)
    if not provider["custom"]:
        raise ValueError("内置平台不能删除，可以停用")
    store.execute("DELETE FROM providers WHERE id = ?", (provider_id,))


# 常见错误码的中文说明，测试结果里显示给用户看
ERROR_HINTS = {
    400: "请求被拒绝，多半是模型名称填错了",
    401: "Key 无效或已过期",
    402: "账户余额不足",
    403: "没有权限（该模型不在你的套餐/免费档里，或地区受限）",
    404: "接口地址或模型名称不存在",
    422: "请求参数不被平台接受",
    429: "请求太频繁或额度用完了（免费档有每秒次数限制）",
}


def _platform_message(e):
    """取出平台返回的原始错误说明，例如 Mistral 的 {"message": "..."}"""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = err.get("message") or err.get("detail") or ""
        if isinstance(msg, (list, dict)):
            msg = json.dumps(msg, ensure_ascii=False)
        if msg:
            return str(msg)[:160]
    return str(e)[:160]


def _describe_error(e, provider):
    """把调用异常整理成「状态码 + 中文原因 + 原始信息」；网络问题和 Key 问题分开说"""
    code = getattr(e, "status_code", None)
    name = type(e).__name__
    kind = "key"
    if code:
        hint = ERROR_HINTS.get(code) or ("平台服务器出错，稍后再试" if code >= 500 else "调用失败")
        if code >= 500:
            kind = "server"
    elif "Timeout" in name or "Connection" in name:
        kind = "network"
        what = "超时没有响应" if "Timeout" in name else "连不上接口地址"
        if provider.get("category") == "local":
            hint = f"网络问题：{what}，请确认本地模型程序已经启动"
        elif provider.get("category") == "overseas" or provider.get("id") == "openrouter":
            hint = f"网络问题：{what}。海外模型需要代理，请确认代理软件（如 Clash）让 python.exe 走代理，而不是直连"
        else:
            hint = f"网络问题：{what}，请检查网络或接口地址"
    else:
        hint = "调用失败"
    return {"code": code, "kind": kind, "hint": hint, "error": str(e)[:300], "detail": _platform_message(e)}


def explain_error(e, provider):
    """一句话说明失败原因，给 AI 助手等页面直接显示"""
    d = _describe_error(e, provider)
    text = f"{d['code']} {d['hint']}" if d["code"] else d["hint"]
    if d["kind"] != "network" and d["detail"] and d["detail"] not in text:
        text += f"（平台说：{d['detail']}）"
    return text


def _test_key(provider, index, key):
    from .llm import call_provider

    start = time.time()
    item = {"index": index + 1, "key": _mask(key), "length": len(key)}
    try:
        resp = call_provider(
            provider, key,
            messages=[{"role": "user", "content": "你好，请用一句话介绍你自己"}],
            max_tokens=60, temperature=0.3, timeout=25,
        )
        item.update(ok=True, reply=(resp.choices[0].message.content or "").strip()[:120])
    except Exception as e:
        item.update(ok=False, **_describe_error(e, provider))
    item["ms"] = int((time.time() - start) * 1000)
    return item


def test_provider(provider_id):
    """每个 Key 都真实发一句「你好」，同时测试，返回每个 Key 的状态和耗时"""
    provider = get_provider(provider_id)
    keys = provider["api_keys"]
    if not keys:
        raise ValueError("请先配置 API Key")
    with ThreadPoolExecutor(max_workers=min(len(keys), 8)) as pool:
        items = list(pool.map(lambda pair: _test_key(provider, *pair), enumerate(keys)))
    ok_items = [i for i in items if i["ok"]]
    first_bad = next((i for i in items if not i["ok"]), None)
    result = {
        "ok": bool(ok_items),
        "ok_count": len(ok_items),
        "total": len(items),
        "model": provider["model"],
        "latency": round(min(i["ms"] for i in ok_items) / 1000, 2) if ok_items else None,
        "reply": ok_items[0]["reply"] if ok_items else "",
        "error": "" if ok_items else f"{first_bad['code'] or ''} {first_bad['hint']}".strip(),
        "keys": items,
        "time": time.time(),
    }
    _update(provider_id, {"last_test": json.dumps(result, ensure_ascii=False)})
    return result
