"""消息通知：把买家消息、付款、发货、风控等事件推送到手机"""
import json
import threading
import time

import requests
from loguru import logger

from . import store

CHANNEL_TYPES = {
    "dingtalk": "钉钉机器人",
    "feishu": "飞书机器人",
    "wecom": "企业微信机器人",
    "bark": "Bark（iPhone）",
    "serverchan": "Server酱（微信）",
    "webhook": "通用 Webhook",
}
NOTIFY_EVENTS = {
    "message": "买家新消息",
    "order": "买家付款",
    "delivery": "自动发货完成",
    "stock": "卡密库存不足",
    "risk": "风控 / Cookie 失效",
}


def send(channel_type, url, title, text):
    content = f"【闲鱼助手】{title}\n{text}"
    if channel_type == "dingtalk":
        resp = requests.post(url, json={"msgtype": "text", "text": {"content": content}}, timeout=8)
    elif channel_type == "feishu":
        resp = requests.post(url, json={"msg_type": "text", "content": {"text": content}}, timeout=8)
    elif channel_type == "wecom":
        resp = requests.post(url, json={"msgtype": "text", "text": {"content": content}}, timeout=8)
    elif channel_type == "bark":
        resp = requests.post(url, json={"title": f"闲鱼助手 · {title}", "body": text}, timeout=8)
    elif channel_type == "serverchan":
        resp = requests.post(url, data={"title": f"闲鱼助手 · {title}", "desp": text}, timeout=8)
    else:
        resp = requests.post(url, json={"title": title, "text": text, "time": int(time.time())}, timeout=8)
    resp.raise_for_status()
    return resp.text[:200]


def list_channels():
    result = []
    for r in store.rows("SELECT * FROM notify_channels ORDER BY id"):
        r["events"] = json.loads(r["events"] or "[]")
        r["type_label"] = CHANNEL_TYPES.get(r["type"], r["type"])
        result.append(r)
    return result


def save_channel(payload):
    channel_type = payload.get("type")
    if channel_type not in CHANNEL_TYPES:
        raise ValueError("请选择通知方式")
    url = str(payload.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("请填写正确的 Webhook 地址（以 http 开头）")
    name = str(payload.get("name", "")).strip() or CHANNEL_TYPES[channel_type]
    events = [e for e in payload.get("events", []) if e in NOTIFY_EVENTS]
    enabled = 1 if payload.get("enabled", True) else 0
    if payload.get("id"):
        store.execute(
            "UPDATE notify_channels SET name = ?, type = ?, url = ?, events = ?, enabled = ? WHERE id = ?",
            (name, channel_type, url, json.dumps(events), enabled, int(payload["id"])),
        )
    else:
        store.execute(
            "INSERT INTO notify_channels (name, type, url, events, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, channel_type, url, json.dumps(events), enabled, time.time()),
        )


def notify(event, title, text):
    """异步推送，不阻塞机器人回复"""
    channels = [c for c in list_channels() if c["enabled"] and event in c["events"]]
    if not channels:
        return

    def worker():
        for c in channels:
            try:
                send(c["type"], c["url"], title, text)
            except Exception as e:
                logger.warning(f"通知发送失败（{c['name']}）：{e}")

    threading.Thread(target=worker, daemon=True).start()
