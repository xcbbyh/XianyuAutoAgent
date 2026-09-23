"""关键词回复、自动发货、黑名单规则的增删改查"""
import json
import re
import time

from . import store

MATCH_TYPES = {"contains": "包含", "exact": "完全一致", "regex": "正则"}
DELIVERY_MODES = {"text": "固定内容", "cards": "卡密（每单发一条，发完即止）"}


# ---------------- 关键词回复 ----------------

def list_keywords():
    return store.rows("SELECT * FROM keyword_rules ORDER BY id DESC")


def save_keyword(payload):
    keyword = str(payload.get("keyword", "")).strip()
    reply = str(payload.get("reply", "")).strip()
    match_type = payload.get("match_type", "contains")
    if not keyword or not reply:
        raise ValueError("关键词和回复内容都不能为空")
    if match_type not in MATCH_TYPES:
        raise ValueError("匹配方式不正确")
    if match_type == "regex":
        try:
            re.compile(keyword)
        except re.error as e:
            raise ValueError(f"正则表达式有误：{e}")
    values = (keyword, match_type, reply, str(payload.get("item_id", "")).strip(),
              1 if payload.get("enabled", True) else 0)
    if payload.get("id"):
        store.execute(
            "UPDATE keyword_rules SET keyword = ?, match_type = ?, reply = ?, item_id = ?, enabled = ? WHERE id = ?",
            (*values, int(payload["id"])),
        )
    else:
        store.execute(
            "INSERT INTO keyword_rules (keyword, match_type, reply, item_id, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (*values, time.time()),
        )


def match_keyword(message, item_id):
    """返回匹配到的回复；指定了商品的规则优先"""
    text = (message or "").strip()
    rules = store.rows(
        "SELECT * FROM keyword_rules WHERE enabled = 1 AND (item_id = '' OR item_id = ?) "
        "ORDER BY (item_id = '') , id",
        (item_id or "",),
    )
    for rule in rules:
        keyword = rule["keyword"]
        if rule["match_type"] == "exact":
            hit = text == keyword
        elif rule["match_type"] == "regex":
            try:
                hit = re.search(keyword, text) is not None
            except re.error:
                hit = False
        else:
            hit = any(k and k in text for k in keyword.split("|"))
        if hit:
            store.execute("UPDATE keyword_rules SET hits = hits + 1 WHERE id = ?", (rule["id"],))
            return rule
    return None


# ---------------- 自动发货 ----------------

def list_delivery_rules():
    result = []
    for r in store.rows("SELECT * FROM delivery_rules ORDER BY id DESC"):
        r["stock"] = json.loads(r["stock"] or "[]")
        r["stock_count"] = len(r["stock"])
        result.append(r)
    return result


def save_delivery_rule(payload):
    name = str(payload.get("name", "")).strip()
    item_id = str(payload.get("item_id", "")).strip()
    title_keyword = str(payload.get("title_keyword", "")).strip()
    mode = payload.get("mode", "text")
    content = str(payload.get("content", "")).strip()
    if not name:
        raise ValueError("请填写规则名称")
    if not item_id and not title_keyword:
        raise ValueError("商品 ID 和商品标题关键词至少填一个")
    if mode not in DELIVERY_MODES:
        raise ValueError("发货方式不正确")
    stock = [line.strip() for line in str(payload.get("stock", "")).splitlines() if line.strip()]
    if mode == "text" and not content:
        raise ValueError("请填写发货内容")
    if mode == "cards" and not stock:
        raise ValueError("请至少填写一条卡密")
    if payload.get("id") and mode == "cards":
        # 编辑期间可能已经发出了几条卡密，别把它们又加回库存
        sent = {r["content"] for r in store.rows("SELECT content FROM deliveries WHERE rule_id = ?",
                                                  (int(payload["id"]),))}
        stock = [c for c in stock if not any(s == c or s.endswith("\n" + c) for s in sent)]
        if not stock:
            raise ValueError("填写的卡密都已经发出去了，请补充新的卡密")
    values = (name, item_id, title_keyword, mode, content, json.dumps(stock, ensure_ascii=False),
              1 if payload.get("enabled", True) else 0)
    if payload.get("id"):
        store.execute(
            "UPDATE delivery_rules SET name = ?, item_id = ?, title_keyword = ?, mode = ?, content = ?, "
            "stock = ?, enabled = ? WHERE id = ?",
            (*values, int(payload["id"])),
        )
    else:
        store.execute(
            "INSERT INTO delivery_rules (name, item_id, title_keyword, mode, content, stock, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, time.time()),
        )


def list_deliveries(limit=200):
    return store.rows("SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,))


def take_delivery(chat_id, item_id, item_title, buyer_id, buyer_name):
    """
    为这笔订单取出发货内容。
    返回 (内容, 规则, 剩余库存, 发货记录ID, 取出的卡密)；同一个会话同一个商品 24 小时内只发一次。
    """
    recent = store.row(
        "SELECT id FROM deliveries WHERE chat_id = ? AND item_id = ? AND created_at > ?",
        (chat_id, item_id, time.time() - 86400),
    )
    if recent:
        return None, None, None, None, None

    rules = store.rows("SELECT * FROM delivery_rules WHERE enabled = 1 ORDER BY (item_id = ''), id")
    rule = None
    for r in rules:
        if r["item_id"] and r["item_id"] == item_id:
            rule = r
            break
        if not r["item_id"] and r["title_keyword"] and r["title_keyword"] in (item_title or ""):
            rule = r
            break
    if not rule:
        return None, None, None, None, None

    with store.db() as conn:
        # 在同一个事务里取卡密，避免两个订单拿到同一条
        current = conn.execute("SELECT * FROM delivery_rules WHERE id = ?", (rule["id"],)).fetchone()
        remaining = card = None
        if current["mode"] == "cards":
            stock = json.loads(current["stock"] or "[]")
            if not stock:
                return None, dict(current), 0, None, None
            card = stock.pop(0)
            remaining = len(stock)
            content = f"{current['content']}\n{card}".strip() if current["content"] else card
            conn.execute("UPDATE delivery_rules SET stock = ? WHERE id = ?",
                         (json.dumps(stock, ensure_ascii=False), current["id"]))
        else:
            content = current["content"]
        conn.execute("UPDATE delivery_rules SET delivered = delivered + 1 WHERE id = ?", (current["id"],))
        delivery_id = conn.execute(
            "INSERT INTO deliveries (rule_id, rule_name, chat_id, item_id, buyer_id, buyer_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (current["id"], current["name"], chat_id, item_id, buyer_id, buyer_name, content, time.time()),
        ).lastrowid
    return content, dict(current), remaining, delivery_id, card


def rollback_delivery(delivery_id, rule_id, card=None):
    """发货消息没发出去：删除发货记录，卡密放回库存最前面"""
    with store.db() as conn:
        conn.execute("DELETE FROM deliveries WHERE id = ?", (delivery_id,))
        conn.execute("UPDATE delivery_rules SET delivered = MAX(delivered - 1, 0) WHERE id = ?", (rule_id,))
        if card:
            current = conn.execute("SELECT stock FROM delivery_rules WHERE id = ?", (rule_id,)).fetchone()
            if current:
                stock = [card] + json.loads(current["stock"] or "[]")
                conn.execute("UPDATE delivery_rules SET stock = ? WHERE id = ?",
                             (json.dumps(stock, ensure_ascii=False), rule_id))


# ---------------- 黑名单 ----------------

def list_blacklist():
    return store.rows("SELECT * FROM blacklist ORDER BY created_at DESC")


def add_blacklist(user_id, note=""):
    user_id = str(user_id).strip()
    if not user_id:
        raise ValueError("请填写买家 ID")
    store.execute(
        "INSERT INTO blacklist (user_id, note, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET note = excluded.note",
        (user_id, str(note).strip(), time.time()),
    )


def is_blacklisted(user_id):
    return store.row("SELECT user_id FROM blacklist WHERE user_id = ?", (str(user_id),)) is not None


def delete(table, rule_id):
    if table not in ("keyword_rules", "delivery_rules", "notify_channels"):
        raise ValueError("不支持的删除操作")
    store.execute(f"DELETE FROM {table} WHERE id = ?", (int(rule_id),))
