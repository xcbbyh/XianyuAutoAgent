"""
机器人扩展功能入口（main.py 调用）

把控制台里的配置接到原有机器人流程上：关键词回复、自动发货、黑名单、
营业时间、AI 开关、通知推送、数据统计，以及固定开启的防风控发送延迟。
这里的任何异常都不应该影响原有自动回复，所以每个方法都自带兜底。
"""
import random
import re
import time

from loguru import logger

from . import approvals, content_guard, notify, rules, safety, store

# 付款后闲鱼会在会话里推送这类系统卡片
PAYMENT_PATTERN = re.compile(r"我已付款|已付款.{0,4}(待|等待).{0,2}发货|等待你发货|等待卖家发货")

# ---- 防风控发送延迟（固定开启，不提供关闭选项）----
# 真人看到消息再打字需要时间，秒回容易被平台识别为机器人
DELAY_BASE = (1.5, 3.5)      # 看消息、思考的时间（秒）
DELAY_PER_CHAR = (0.08, 0.2)  # 每个字的打字时间（秒）
DELAY_MAX = 12.0             # 上限，避免长回复让买家等太久

AWAY_REPLY_INTERVAL = 6 * 3600  # 同一个买家的离线提示 6 小时内只发一次


class BotHooks:
    def __init__(self):
        self._settings = None
        self._settings_time = 0
        self._away_sent = {}
        self._throttled = {}
        self._human_notified = {}

    def settings(self):
        # 缓存 3 秒：控制台改完配置几秒内生效，又不会每条消息都查库
        if self._settings is None or time.time() - self._settings_time > 3:
            try:
                self._settings = store.get_settings()
                self._settings_time = time.time()
            except Exception as e:
                logger.warning(f"读取控制台配置失败，使用默认配置：{e}")
                self._settings = dict(store.DEFAULT_SETTINGS)
        return self._settings

    def ai_enabled(self):
        return bool(self.settings().get("ai_enabled", True))

    def toggle_keywords(self):
        return self.settings().get("toggle_keywords") or "。"

    def manual_timeout_seconds(self):
        return int(self.settings().get("manual_timeout_minutes") or 60) * 60

    def fallback_reply(self):
        return (self.settings().get("fallback_reply") or "").strip()

    def human_delay(self, text, chat_id=""):
        """
        固定的基础延迟 × 防风控模式倍数；新买家的第一句回复再多等一会儿（真人需要先看商品和消息）
        """
        delay = random.uniform(*DELAY_BASE) + len(text or "") * random.uniform(*DELAY_PER_CHAR)
        delay = min(delay, DELAY_MAX)
        try:
            p = safety.params()
            delay *= p["reply_delay_factor"]
            if chat_id and not store.row(
                "SELECT id FROM events WHERE chat_id = ? AND type IN ('ai_reply', 'keyword_reply', 'away_reply') LIMIT 1",
                (chat_id,),
            ):
                delay += random.uniform(*p["first_reply_extra"])
        except Exception as e:
            logger.warning(f"读取防风控配置失败：{e}")
        return delay

    def allow_reply(self, chat_id, buyer_name=""):
        """同一买家一小时内自动回复过多时暂停回复，防止被刷消息或两个机器人互相对话"""
        try:
            limit = safety.params()["buyer_hourly_limit"]
            count = store.row(
                "SELECT COUNT(*) AS n FROM events WHERE chat_id = ? AND created_at > ? "
                "AND type IN ('ai_reply', 'keyword_reply', 'away_reply')",
                (chat_id, time.time() - 3600),
            )["n"]
        except Exception as e:
            logger.warning(f"检查回复频率失败：{e}")
            return True
        if count < limit:
            return True
        if chat_id not in self._throttled or time.time() - self._throttled[chat_id] > 3600:
            self._throttled[chat_id] = time.time()
            notify.notify("message", "买家消息过多，已暂停自动回复",
                          f"{buyer_name or chat_id} 一小时内已自动回复 {count} 条，达到防风控上限，请人工查看")
        return False

    # ---------------- 发送前审核 ----------------

    def queue_reply(self, kind, chat_id, buyer_id, buyer_name, item_id, item_title, buyer_message, reply,
                    delivery=None):
        """放进「待审核回复」并推送通知（不会发给买家）"""
        approvals.queue(kind, chat_id, buyer_id, buyer_name, item_id, item_title, buyer_message, reply, delivery)
        try:
            title = "买家已付款，发货内容等你审核" if kind == "delivery" else "有一条回复等你审核"
            text = (f"{buyer_name or '买家'}（商品 {item_title or item_id}）" if kind == "delivery"
                    else f"{buyer_name or '买家'}：{buyer_message}\n准备回复：{reply}")
            notify.notify("message", title, text + "\n请到控制台「待审核回复」点「同意发送」或「不发送」")
        except Exception as e:
            logger.warning(f"推送审核通知失败：{e}")

    def need_human(self, chat_id, buyer_name, message, reason):
        """机器人不回的聊天：记日志并提醒卖家本人去闲鱼里回复（同一个聊天 1 小时内只提醒一次）"""
        if time.time() - self._human_notified.get(chat_id, 0) < 3600:
            return
        self._human_notified[chat_id] = time.time()
        try:
            notify.notify("message", "这条消息需要你本人回复", f"{buyer_name or '买家'}：{message}\n原因：{reason}")
        except Exception as e:
            logger.warning(f"推送提醒失败：{e}")

    @staticmethod
    def check_reply(chat_id, text, item_title="", kind="ai"):
        try:
            return content_guard.check_reply(chat_id, text, item_title, kind)
        except Exception as e:
            return [f"安全检查出错：{e}"]

    @staticmethod
    def is_known_own_item(item_id):
        """控制台同步过的「我的商品」或自己上架成功的商品，一定是自己发布的"""
        try:
            return bool(store.row("SELECT item_id FROM my_items WHERE item_id = ?", (str(item_id),)) or
                        store.row("SELECT id FROM listings WHERE item_id = ? AND item_id != ''", (str(item_id),)))
        except Exception as e:
            logger.warning(f"查询我的商品失败：{e}")
            return False

    @staticmethod
    def known_item_text(item_id):
        """同步过的「我的商品」或自己上架的商品的标题和描述（商品详情接口取不到时用）"""
        try:
            r = store.row("SELECT title, '' AS description FROM my_items WHERE item_id = ?", (str(item_id),)) or \
                store.row("SELECT title, description FROM listings WHERE item_id = ? AND item_id != ''", (str(item_id),))
            return (r["title"] or "", r["description"] or "") if r else ("", "")
        except Exception as e:
            logger.warning(f"查询我的商品失败：{e}")
            return "", ""

    # ---------------- 规则 ----------------

    def is_blacklisted(self, user_id):
        try:
            return rules.is_blacklisted(user_id)
        except Exception as e:
            logger.warning(f"检查黑名单失败：{e}")
            return False

    def keyword_reply(self, message, item_id):
        try:
            rule = rules.match_keyword(message, item_id)
            return rule["reply"] if rule else None
        except Exception as e:
            logger.warning(f"匹配关键词失败：{e}")
            return None

    def away_reply(self, chat_id):
        """
        营业时间外的处理。
        返回 None 表示营业中，正常回复；返回 "" 表示不回复；返回文本表示发送离线提示。
        """
        hours = self.settings().get("business_hours") or {}
        if not hours.get("enabled"):
            return None
        now = time.strftime("%H:%M")
        start, end = hours.get("start", "00:00"), hours.get("end", "23:59")
        is_open = start <= now < end if start <= end else (now >= start or now < end)
        if is_open:
            return None
        if hours.get("mode") != "away":
            return ""
        last = self._away_sent.get(chat_id, 0)
        if time.time() - last < AWAY_REPLY_INTERVAL:
            return ""
        self._away_sent[chat_id] = time.time()
        return hours.get("away_message") or ""

    @staticmethod
    def is_payment_message(message):
        return bool(PAYMENT_PATTERN.search(message or ""))

    def take_delivery(self, chat_id, item_id, item_title, buyer_id, buyer_name):
        """取出这笔订单的发货内容；没有匹配规则、已发过或库存不足时返回 None"""
        try:
            content, rule, remaining, delivery_id, card = rules.take_delivery(
                chat_id, item_id, item_title, buyer_id, buyer_name)
        except Exception as e:
            logger.error(f"自动发货出错：{e}")
            return None
        if rule and content is None and remaining == 0:
            logger.warning(f"自动发货规则「{rule['name']}」卡密已用完")
            self.event("stock", chat_id, f"规则「{rule['name']}」卡密已用完，买家 {buyer_name} 未能自动发货，请手动发货")
            return None
        if not content:
            return None
        if remaining is not None and remaining <= 3:
            self.event("stock", chat_id, f"规则「{rule['name']}」卡密只剩 {remaining} 条")
        return {"content": content, "rule_id": rule["id"], "rule_name": rule["name"],
                "delivery_id": delivery_id, "card": card}

    def rollback_delivery(self, delivery, buyer_name=""):
        try:
            rules.rollback_delivery(delivery["delivery_id"], delivery["rule_id"], delivery["card"])
        except Exception as e:
            logger.error(f"退回卡密失败：{e}")
        notify.notify("delivery", "自动发货失败",
                      f"给 {buyer_name} 的发货消息没有发出去（网络断开），卡密已退回库存，请手动发货")

    @staticmethod
    def is_new_order(chat_id, item_id):
        """同一个订单的付款通知可能推送两次（订单状态 + 会话卡片），10 分钟内只记一次"""
        try:
            return store.row(
                "SELECT id FROM events WHERE type = 'order' AND chat_id = ? AND created_at > ?",
                (chat_id, time.time() - 600),
            ) is None
        except Exception:
            return True

    # ---------------- 事件 ----------------

    def event(self, event_type, chat_id="", detail=""):
        """记录统计并按配置推送通知（stock 只推送不计入统计）"""
        try:
            if event_type in store.EVENT_TYPES:
                store.log_event(event_type, chat_id, detail)
            if event_type in notify.NOTIFY_EVENTS:
                notify.notify(event_type, notify.NOTIFY_EVENTS[event_type], detail)
        except Exception as e:
            logger.warning(f"记录事件失败：{e}")
