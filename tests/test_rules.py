"""
闲鱼规则相关的自动测试：python -m unittest discover -s tests

每个测试用一个临时 data 目录，不会碰你自己的 data/console.db。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = tempfile.mkdtemp(prefix="xianyu_test_")
os.environ["XIANYU_DATA_DIR"] = DATA
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from console import approvals, content_guard, rules, safety, shop, store  # noqa: E402
from context_manager import ChatContextManager  # noqa: E402

MY_ID = "1000"


def reset_db():
    for name in ("console.db", "console.db-wal", "console.db-shm", "chat_history.db"):
        try:
            os.remove(os.path.join(DATA, name))
        except OSError:
            pass
    store._schema_ready = False


class Base(unittest.TestCase):
    def setUp(self):
        reset_db()
        # 默认按白天测试，夜间规则单独测
        self.night = mock.patch.object(content_guard, "is_night", return_value=False)
        self.night.start()
        self.quiet = mock.patch.object(safety, "in_quiet_hours", return_value=False)
        self.quiet.start()
        self.notify = mock.patch("console.notify.notify")
        self.notify.start()

    def tearDown(self):
        mock.patch.stopall()

    def queue(self, chat, text="在的", kind="ai", title="九成新 iPad", buyer_message="还在吗", delivery=None):
        return approvals.queue(kind, chat, f"b{chat}", "买家", "i1", title, buyer_message, text, delivery)

    def sent_event(self, chat, text, ago=0):
        store.execute("INSERT INTO events (type, chat_id, detail, created_at) VALUES ('ai_reply', ?, ?, ?)",
                      (str(chat), text, time.time() - ago))


class ApprovalRules(Base):
    def test_bulk_approve_cannot_pass_15_people_per_hour(self):
        ids = [self.queue(f"c{i}", f"在的，第{i}个") for i in range(20)]
        approved = 0
        for rid in ids:
            try:
                approvals.approve(rid)
                approved += 1
            except ValueError as e:
                self.assertIn("每小时最多", str(e))
        self.assertEqual(approved, content_guard.MAX_PEOPLE_PER_HOUR)

    def test_night_blocks_approve_and_send(self):
        rid = self.queue("c1")
        with mock.patch.object(content_guard, "is_night", return_value=True):
            with self.assertRaises(ValueError):
                approvals.approve(rid)
            self.assertTrue(content_guard.send_time_problems("c1", "delivery"))
            self.assertTrue(content_guard.send_time_problems("c1", "ai"))

    def test_pause_blocks_delivery_too(self):
        rid = self.queue("c1", "卡密：ABCD-1234", kind="delivery", delivery={"delivery_id": 1, "rule_id": 1})
        safety.trip("测试")
        with self.assertRaises(ValueError):
            approvals.approve(rid)
        self.assertTrue(content_guard.send_time_problems("c1", "delivery"))

    def test_delivery_with_offsite_contact_blocked(self):
        rid = self.queue("c1", "网盘链接 https://pan.example.com/abc", kind="delivery",
                         delivery={"delivery_id": 1, "rule_id": 1})
        with self.assertRaises(ValueError):
            approvals.approve(rid)

    def test_delivery_random_card_key_not_misread(self):
        rid = self.queue("c1", "卡密：k8vxqQQz-wxyz", kind="delivery", delivery={"delivery_id": 1, "rule_id": 1})
        approvals.approve(rid)

    def test_banned_words(self):
        self.assertTrue(content_guard.banned_words("加vx聊"))
        self.assertTrue(content_guard.banned_words("https://a.b"))
        self.assertTrue(content_guard.banned_words("加我QQ"))
        self.assertFalse(content_guard.banned_words("在的"))

    def test_stale_draft_after_manual_reply(self):
        rid = self.queue("c1")
        time.sleep(0.01)
        ChatContextManager(db_path=store.CHAT_DB_PATH).add_message_by_chat("c1", MY_ID, "i1", "assistant", "在")
        with self.assertRaises(ValueError) as e:
            approvals.approve(rid)
        self.assertIn("已经回过", str(e.exception))

    def test_same_text_to_many_people_blocked(self):
        for i in range(3):
            self.sent_event(f"x{i}", "在的", ago=600)
        rid = self.queue("c1", "在的")
        with self.assertRaises(ValueError) as e:
            approvals.approve(rid)
        self.assertIn("群发", str(e.exception))

    def test_sensitive_item_and_bot_question_blocked_at_approve(self):
        rid = self.queue("c1", "在的", title="边牧幼犬")
        with self.assertRaises(ValueError):
            approvals.approve(rid)
        rid = self.queue("c2", "在的", buyer_message="你是AI吗")
        with self.assertRaises(ValueError):
            approvals.approve(rid)

    def test_gap_between_sends(self):
        self.sent_event("c1", "在的", ago=30)
        self.assertTrue(content_guard.send_time_problems("c1", "ai"))

    def test_requeue_keeps_row_pending(self):
        rid = self.queue("c1")
        approvals.approve(rid)
        r = approvals.claim_approved()[0]
        approvals.requeue(r, "夜里不发")
        row = store.row("SELECT status, error FROM pending_replies WHERE id = ?", (rid,))
        self.assertEqual(row["status"], "pending")
        self.assertIn("夜里", row["error"])

    def test_blank_reply(self):
        for text in ("-", " - ", "—", "。", ""):
            self.assertTrue(content_guard.is_blank(text), text)
        self.assertFalse(content_guard.is_blank("在的"))

    def test_old_default_away_message_replaced(self):
        store.save_settings({"business_hours": {**store.DEFAULT_SETTINGS["business_hours"],
                                                "away_message": store.OLD_AWAY_MESSAGES[0]}})
        msg = store.get_settings()["business_hours"]["away_message"]
        self.assertEqual(content_guard.banned_words(msg) + content_guard.ai_tone(msg), [])

    def test_keyword_rule_rejects_banned_words(self):
        with self.assertRaises(ValueError):
            rules.save_keyword({"keyword": "便宜", "reply": "最低价了亲亲"})
        rules.save_keyword({"keyword": "还在", "reply": "在的"})


class ListingRules(Base):
    def test_sensitive_listing_refused(self):
        self.assertTrue(shop.listing_problems("边牧幼犬 两个月", "疫苗打了"))
        self.assertTrue(shop.listing_problems("自用 iPad", "最便宜，一手货源"))
        self.assertFalse(shop.listing_problems("全新 iPad Air 5", "全新没拆，送壳"))
        self.assertTrue(shop.listing_problems("九成新 iPad Air 5", "全新没拆"))

    def test_duplicate_listing_refused(self):
        store.execute("INSERT INTO listings (title, description, status, created_at) VALUES (?, ?, 'published', ?)",
                      ("九成新 iPad Air 5 64G", "用了一年，屏幕没划痕，带充电器", time.time()))
        self.assertTrue(shop.listing_problems("九成新 iPad Air 5 64G", "别的描述"))

    def test_queued_listing_rechecked_before_publish(self):
        lid = store.execute("INSERT INTO listings (title, description, images, status, category_hint, created_at) "
                            "VALUES ('柯基幼犬', '纯种', '[]', 'queued', '', ?)", (time.time(),))
        with mock.patch.object(shop, "publish_listing") as publish:
            shop.run_publish(lid)
            publish.assert_not_called()
        self.assertEqual(store.row("SELECT status FROM listings WHERE id = ?", (lid,))["status"], "failed")

    def test_polish_refused_at_night(self):
        with mock.patch.object(safety, "in_quiet_hours", return_value=True):
            with self.assertRaises(ValueError):
                shop.polish_one("1")


class CookieWriteBack(unittest.TestCase):
    def test_no_overwrite_without_unb_and_backslash_safe(self):
        from XianyuApis import XianyuApis
        folder = tempfile.mkdtemp()
        env = os.path.join(folder, ".env")
        with open(env, "w", encoding="utf-8") as f:
            f.write("API_KEY=x\nCOOKIES_STR=unb=1000; cookie2=abc\n")
        api = XianyuApis()
        with mock.patch.dict(os.environ, {"ENV_FILE": env}):
            api.session.cookies.set("cookie2", "zzz")
            api.update_env_cookies()
            with open(env, encoding="utf-8") as f:
                self.assertIn("unb=1000", f.read())
            api.session.cookies.set("unb", "1000")
            api.session.cookies.set("tracknick", "\\u5c0f\\u660e")
            api.update_env_cookies()
            with open(env, encoding="utf-8") as f:
                text = f.read()
        self.assertIn("tracknick=\\u5c0f\\u660e", text)
        self.assertIn("API_KEY=x", text)
        shutil.rmtree(folder)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def make_live():
    import main
    live = main.XianyuLive(f"unb={MY_ID}; cookie2=x")
    live.context_manager = ChatContextManager(db_path=store.CHAT_DB_PATH)
    live.ws = FakeWS()
    live.ws_ready = True
    return main, live


def chat_packet(sender, text, item_id, chat="c1", **extra):
    m1 = {"2": f"{chat}@goofish", "5": str(int(time.time() * 1000)),
          "10": {"reminderTitle": "对方", "senderUserId": sender, "reminderContent": text,
                 "reminderUrl": f"https://x?itemId={item_id}&a=1"}}
    m1.update(extra)
    return {"1": m1}


class BotFlow(Base):
    def run_message(self, live, main, message):
        payload = {"headers": {"mid": "1"}, "body": {"syncPushPackage": {"data": [{"data": "@@not-base64@@"}]}}}
        with mock.patch.object(main, "decrypt", return_value=json.dumps(message)):
            asyncio.run(live.handle_message(payload, FakeWS()))

    def item_api(self, seller, title="九成新 iPad"):
        return {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"title": title, "desc": "", "soldPrice": 100},
                                                     "sellerDO": {"sellerId": seller}}}

    def setUp(self):
        super().setUp()
        self.main, self.live = make_live()
        self.main.bot = mock.Mock()
        self.main.bot.generate_reply_with_intent.return_value = ("在的", "default")

    def pending(self):
        return store.rows("SELECT * FROM pending_replies WHERE status = 'pending'")

    def test_buyer_side_chat_never_drafted(self):
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value=self.item_api("9999", "边牧")):
            self.run_message(self.live, self.main, chat_packet("9999", "要买吗", "555"))
        self.assertEqual(self.pending(), [])
        self.main.bot.generate_reply_with_intent.assert_not_called()

    def test_unknown_seller_never_drafted(self):
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value={"error": "x"}):
            self.run_message(self.live, self.main, chat_packet("2000", "还在吗", "556"))
        self.assertEqual(self.pending(), [])

    def test_own_item_drafted_not_sent(self):
        ws = self.live.ws
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value=self.item_api(MY_ID)):
            self.run_message(self.live, self.main, chat_packet("2000", "还在吗", "557"))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(ws.sent, [])

    def test_card_message_not_replied(self):
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value=self.item_api(MY_ID)):
            self.run_message(self.live, self.main,
                             chat_packet("2000", "我已拍下，待付款", "558", **{"6": {"3": {"4": 6}}}))
        self.assertEqual(self.pending(), [])

    def test_sensitive_item_with_failed_detail_uses_synced_title(self):
        store.execute("INSERT INTO my_items (item_id, title, synced_at) VALUES ('559', '柯基幼犬', ?)", (time.time(),))
        rules.save_keyword({"keyword": "还在", "reply": "在的"})
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value={"error": "x"}):
            self.run_message(self.live, self.main, chat_packet("2000", "还在吗", "559"))
        self.assertEqual(self.pending(), [])

    def test_send_time_night_requeues_delivery(self):
        rid = self.queue("c9", "卡密：ABCD", kind="delivery",
                         delivery={"delivery_id": 1, "rule_id": 1, "card": "ABCD"})
        approvals.approve(rid)
        r = approvals.claim_approved()[0]
        with mock.patch.object(content_guard, "is_night", return_value=True):
            asyncio.run(self.live.send_approved(r))
        self.assertEqual(self.live.ws.sent, [])
        self.assertEqual(store.row("SELECT status FROM pending_replies WHERE id = ?", (rid,))["status"], "pending")

    def test_send_approved_sends_once(self):
        rid = self.queue("c8", "在的")
        approvals.approve(rid)
        r = approvals.claim_approved()[0]
        with mock.patch.object(self.live.hooks, "human_delay", return_value=0):
            asyncio.run(self.live.send_approved(r))
        self.assertEqual(len(self.live.ws.sent), 1)
        self.assertEqual(store.row("SELECT status FROM pending_replies WHERE id = ?", (rid,))["status"], "sent")

    def test_rejected_send_receipt_trips_pause(self):
        asyncio.run(self.live.send_msg(self.live.ws, "c1", "b1", "在的"))
        mid = self.live.ws.sent[-1]["headers"]["mid"]
        self.live.check_send_receipt({"code": 403, "headers": {"mid": mid}, "body": {"reason": "禁言"}})
        self.assertTrue(safety.is_paused())

    def test_ok_receipt_does_not_trip(self):
        asyncio.run(self.live.send_msg(self.live.ws, "c1", "b1", "在的"))
        mid = self.live.ws.sent[-1]["headers"]["mid"]
        self.live.check_send_receipt({"code": 200, "headers": {"mid": mid}})
        self.assertFalse(safety.is_paused())

    def test_system_mute_notice_trips_but_buyer_text_does_not(self):
        with mock.patch.object(self.live.xianyu, "get_item_info", return_value=self.item_api(MY_ID)):
            self.run_message(self.live, self.main, chat_packet("2000", "你再不回我就举报你禁言", "560"))
            self.assertFalse(safety.is_paused())
            self.run_message(self.live, self.main,
                             chat_packet("", "[你已被禁言，点此查看详情]", "560"))
        self.assertTrue(safety.is_paused())


if __name__ == "__main__":
    unittest.main()
