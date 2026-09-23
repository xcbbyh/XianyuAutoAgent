"""
待审核回复：机器人不直接给买家发消息，先放进这里，卖家在控制台点「同意发送」才发出。

机器人（main.py 子进程）和控制台是两个进程，通过 data/console.db 里的 pending_replies 表交接：
- 机器人：queue() 放入待审核；每隔几秒 claim_approved() 取出已同意的，发送后 mark_sent / mark_failed
- 控制台：list_replies() 展示；approve() 同意（可以先改文字）；reject() 不发送
状态：pending 待审核 → approved 已同意待发送 → sending 发送中 → sent 已发送
                    → rejected 不发送；发送失败为 failed
"""
import json
import time
from datetime import datetime

from loguru import logger

from . import content_guard, rules, store

KINDS = {"ai": "AI 回复", "keyword": "关键词回复", "away": "离线提示", "fallback": "兜底话术", "delivery": "自动发货"}
# 发出去之后记到统计里的事件类型
EVENT_OF_KIND = {"ai": "ai_reply", "fallback": "ai_reply", "keyword": "keyword_reply", "away": "away_reply",
                 "delivery": "delivery"}
STATUS = {"pending": "待审核", "approved": "已同意，等待发送", "sending": "发送中", "sent": "已发送",
          "rejected": "不发送", "failed": "发送失败"}


def queue(kind, chat_id, buyer_id, buyer_name, item_id, item_title, buyer_message, reply, delivery=None):
    now = time.time()
    if kind != "delivery":
        # 买家连发几条时只回最后一条：同一个聊天里还没审核的旧草稿作废，免得一次连发好几条
        store.execute("UPDATE pending_replies SET status = 'rejected', error = '买家又发了新消息，这条已换成新的草稿', "
                      "updated_at = ? WHERE chat_id = ? AND status = 'pending' AND kind != 'delivery'",
                      (now, str(chat_id)))
    rid = store.execute(
        "INSERT INTO pending_replies (kind, chat_id, buyer_id, buyer_name, item_id, item_title, buyer_message, "
        "reply, delivery, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (kind, str(chat_id), str(buyer_id or ""), buyer_name or "", str(item_id or ""), item_title or "",
         buyer_message or "", reply, json.dumps(delivery, ensure_ascii=False) if delivery else "", now, now),
    )
    return rid


def pending_count():
    return store.row("SELECT COUNT(*) AS n FROM pending_replies WHERE status = 'pending'")["n"]


def list_replies(limit=100):
    rows = store.rows(
        "SELECT * FROM pending_replies ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 "
        "WHEN 'sending' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, id DESC LIMIT ?", (limit,))
    for r in rows:
        r["kind_label"] = KINDS.get(r["kind"], r["kind"])
        r["status_label"] = STATUS.get(r["status"], r["status"])
        r["has_delivery"] = bool(r.pop("delivery"))
        r["problems"] = check(r) if r["status"] == "pending" else []
    return rows


def check(r, text=None):
    """发出前的安全检查（敏感词、频率、重复），返回问题列表"""
    problems = content_guard.check_reply(r["chat_id"], r["reply"] if text is None else text,
                                         r.get("item_title", ""), r["kind"])
    busy = store.row("SELECT id FROM pending_replies WHERE chat_id = ? AND id != ? AND status IN ('approved', 'sending')",
                     (r["chat_id"], r["id"]))
    if busy:
        problems.append("这个聊天还有一条回复正在发送，请等它发完再点")
    if r["kind"] != "delivery":
        skip = content_guard.skip_reason(r.get("buyer_message") or "", r.get("item_title", ""))
        if skip:
            problems.append(skip)
        if _answered_after(r):
            problems.append("这条草稿之后，这个聊天里已经回过买家了（你本人或机器人），再发就是追着对方连发，请点「不发送」")
    return problems


def _answered_after(r):
    """草稿生成之后，这个聊天里是否已经有卖家发出的消息（你在闲鱼里手动回的也算）"""
    since = datetime.fromtimestamp(r.get("created_at") or 0).isoformat()
    if store.row("SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' AND timestamp > ? LIMIT 1",
                 (r["chat_id"], since), path=store.CHAT_DB_PATH):
        return True
    return store.row("SELECT id FROM pending_replies WHERE chat_id = ? AND id != ? AND status = 'sent' "
                     "AND kind != 'delivery' AND updated_at > ?",
                     (r["chat_id"], r["id"], r.get("created_at") or 0)) is not None


def _get(reply_id):
    r = store.row("SELECT * FROM pending_replies WHERE id = ?", (int(reply_id),))
    if not r:
        raise ValueError("这条回复不存在")
    return r


def approve(reply_id, text=None):
    r = _get(reply_id)
    if r["status"] not in ("pending", "failed"):
        raise ValueError(f"这条回复当前是「{STATUS.get(r['status'])}」，不能再发送")
    reply = r["reply"]
    if text is not None and r["kind"] != "delivery":
        reply = str(text).strip()
        if not reply:
            raise ValueError("回复内容不能为空")
    if r["status"] == "failed" and r["kind"] == "delivery":
        raise ValueError("这条发货没有确认发出，不能再点同意（避免重复发卡密），请到闲鱼聊天里看一下，没发出就你本人手动发货")
    problems = check(r, reply)
    if problems:
        raise ValueError("；".join(problems))
    store.execute("UPDATE pending_replies SET status = 'approved', reply = ?, error = '', updated_at = ? "
                  "WHERE id = ? AND status IN ('pending', 'failed')", (reply, time.time(), int(reply_id)))


def reject(reply_id):
    r = _get(reply_id)
    if r["status"] not in ("pending", "failed", "approved"):
        raise ValueError(f"这条回复当前是「{STATUS.get(r['status'])}」，不能取消")
    store.execute("UPDATE pending_replies SET status = 'rejected', updated_at = ? WHERE id = ?",
                  (time.time(), int(reply_id)))
    if r["kind"] == "delivery" and r["delivery"] and r["status"] != "failed":
        _rollback(r)


def reject_all_pending():
    for r in store.rows("SELECT id FROM pending_replies WHERE status = 'pending'"):
        reject(r["id"])


def _rollback(r):
    try:
        d = json.loads(r["delivery"])
        rules.rollback_delivery(d["delivery_id"], d["rule_id"], d.get("card"))
    except Exception as e:
        logger.error(f"退回卡密失败：{e}")


# ---------------- 机器人调用 ----------------

def claim_approved(limit=10):
    """取出已同意的回复并标记为发送中（只有机器人进程调用）"""
    rows = store.rows("SELECT * FROM pending_replies WHERE status = 'approved' ORDER BY id LIMIT ?", (int(limit),))
    claimed = []
    for r in rows:
        with store.db() as conn:
            cur = conn.execute("UPDATE pending_replies SET status = 'sending', updated_at = ? "
                               "WHERE id = ? AND status = 'approved'", (time.time(), r["id"]))
            if cur.rowcount:
                claimed.append(r)
    return claimed


def mark_sent(reply_id):
    store.execute("UPDATE pending_replies SET status = 'sent', updated_at = ? WHERE id = ?",
                  (time.time(), int(reply_id)))


def mark_failed(r, error):
    store.execute("UPDATE pending_replies SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                  (str(error)[:300], time.time(), int(r["id"])))
    if r["kind"] == "delivery" and r["delivery"]:
        _rollback(r)


def requeue(r, reason):
    """已同意但发送时规则不允许（夜里、风控暂停、发太快）：退回待审核，卡密继续保留，之后可以再点同意"""
    store.execute("UPDATE pending_replies SET status = 'pending', error = ?, updated_at = ? WHERE id = ?",
                  (f"没有发出：{reason}"[:300], time.time(), int(r["id"])))


def recover_sending():
    """机器人启动时：上次发送中被中断的，不确定发没发出去，标记失败让卖家确认"""
    store.execute("UPDATE pending_replies SET status = 'failed', error = '发送过程中机器人被关闭，请到闲鱼确认是否已发出' "
                  "WHERE status = 'sending'")
