import base64
import json
import asyncio
import time
import os
import websockets
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import XianyuApis
import sys
from collections import defaultdict, deque


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager
from console.hooks import BotHooks
from console import approvals, content_guard, safety

# Docker 里通过 ENV_FILE 把 .env 放进 data 目录，本地运行仍用当前目录的 .env
ENV_PATH = os.getenv("ENV_FILE") or ".env"


class XianyuLive:
    def __init__(self, cookies_str):
        self.xianyu = XianyuApis()
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.context_manager = ChatContextManager()
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        
        
        # 控制台扩展功能（关键词回复、自动发货、营业时间、通知等），发送延迟在其中固定开启
        self.hooks = BotHooks()
        # 每个会话一把锁：同一买家的消息按顺序回复
        self.chat_locks = defaultdict(asyncio.Lock)
        # 正在处理中的消息任务
        self.message_tasks = set()
        # 最近自动发出的消息，用来识别服务器回传，避免重复记录
        self.recent_sent = defaultdict(lambda: deque(maxlen=30))
        # 会话 -> (买家ID, 买家昵称, 商品ID)
        self.chat_meta = {}
        # 当前连接是否可以发消息（「待审核回复」里同意发送的消息要等连接好了再发）
        self.ws_ready = False

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = self.xianyu.get_token(self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def reply_to_buyer(self, chat_id, item_id, buyer_id, buyer_name, text):
        """按优先级处理一条买家消息：人工接管 > 限流 > 关键词 > AI 开关 > 营业时间 > AI 回复"""
        record_user = lambda: self.context_manager.add_message_by_chat(chat_id, buyer_id, item_id, "user", text)

        # 如果当前会话处于人工接管模式，不进行自动回复
        if self.is_manual_mode(chat_id):
            logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，跳过自动回复")
            record_user()
            return

        # 防风控：同一买家短时间内自动回复过多时暂停
        if not self.hooks.allow_reply(chat_id, buyer_name):
            logger.warning(f"🛡️ 买家 {buyer_name} 一小时内自动回复已达上限，暂停自动回复")
            record_user()
            return

        # 出现过风控信号（禁言、滑块、发送失败）：所有自动功能停 24 小时，连草稿也不写
        if safety.is_paused():
            logger.warning("⛔ 防风控暂停中，不处理自动回复，请你本人在闲鱼里回复")
            record_user()
            return

        # 按闲鱼规则，这些聊天机器人不起草回复，交给你本人：活体动物等敏感商品、问「你是AI吗」、看不懂的「啥」「？」
        item_info = await asyncio.to_thread(self.get_item_info_cached, item_id)
        skip = content_guard.skip_reason(text, (item_info or {}).get("title", ""), (item_info or {}).get("desc", ""))
        if skip:
            logger.info(f"✋ {skip}（买家 {buyer_name}：{text}）")
            record_user()
            self.hooks.need_human(chat_id, buyer_name, text, skip)
            return

        # 关键词回复优先于 AI
        keyword_reply = self.hooks.keyword_reply(text, item_id)
        if keyword_reply:
            logger.info(f"🔑 命中关键词回复: {keyword_reply}")
            record_user()
            await self.send_or_queue("keyword", chat_id, item_id, buyer_id, buyer_name, text, keyword_reply)
            return

        # AI 总开关关闭，或者不在营业时间
        if not self.hooks.ai_enabled():
            logger.info("AI 自动回复已在控制台关闭，跳过")
            record_user()
            return
        away = self.hooks.away_reply(chat_id)
        if away is not None:
            record_user()
            if away:
                logger.info("🌙 非营业时间，发送离线提示")
                await self.send_or_queue("away", chat_id, item_id, buyer_id, buyer_name, text, away)
            else:
                logger.info("🌙 非营业时间，不回复")
            return

        item_info = await asyncio.to_thread(self.get_item_info_cached, item_id)
        if not item_info:
            return

        item_description=f"当前商品的信息如下：{self.build_item_description(item_info)}"

        # 获取完整的对话上下文
        context = self.context_manager.get_context_by_chat(chat_id)
        # 生成回复：放到线程里执行，调用大模型时不阻塞其他买家的消息和心跳
        try:
            bot_reply, intent = await asyncio.to_thread(bot.generate_reply_with_intent, text, item_description, context)
            reply_kind = "ai"
        except Exception as e:
            logger.error(f"AI 生成回复失败: {e}")
            bot_reply, intent = self.hooks.fallback_reply(), "default"
            reply_kind = "fallback"
            if not bot_reply:
                record_user()
                return
            logger.info("使用控制台设置的兜底话术回复")

        # 检查是否需要回复
        if bot_reply == "-":
            logger.info(f"[无需回复] 用户 {buyer_name} 的消息被识别为无需回复类型")
            return

        # 添加用户消息到上下文
        record_user()

        # 检查是否为价格意图，如果是则增加议价次数
        if intent == "price":
            self.context_manager.increment_bargain_count_by_chat(chat_id)
            bargain_count = self.context_manager.get_bargain_count_by_chat(chat_id)
            logger.info(f"用户 {buyer_name} 对商品 {item_id} 的议价次数: {bargain_count}")

        # AI 生成期间卖家可能已经接管了这个会话
        if self.is_manual_mode(chat_id):
            logger.info(f"🔴 会话 {chat_id} 在生成回复期间被人工接管，不发送 AI 回复")
            return

        bot_reply = content_guard.humanize(bot_reply)
        if not bot_reply:
            return
        logger.info(f"机器人回复: {bot_reply}")
        await self.send_or_queue(reply_kind, chat_id, item_id, buyer_id, buyer_name, text, bot_reply)

    def item_title(self, item_id):
        try:
            return (self.context_manager.get_item_info(item_id) or {}).get("title", "") or ""
        except Exception:
            return ""

    async def send_or_queue(self, kind, chat_id, item_id, buyer_id, buyer_name, buyer_message, text):
        """
        默认不直接发：放进控制台「待审核回复」，卖家点「同意发送」后才发出。
        只有卖家在控制台明确关掉对应的「发送前需要我同意」开关，才直接发送。
        """
        problems = [] if self.hooks.needs_approval(kind) else self.hooks.check_reply(chat_id, text, self.item_title(item_id), kind)
        if problems:
            logger.warning(f"⚠️ 这条回复没通过安全检查，改为放进「待审核回复」: {'；'.join(problems)}")
        if problems or self.hooks.needs_approval(kind):
            self.hooks.queue_reply(kind, chat_id, buyer_id, buyer_name, item_id, self.item_title(item_id),
                                   buyer_message, text)
            logger.info(f"📝 回复没有发出，已放进控制台「待审核回复」，等你点「同意发送」: {text}")
            return
        try:
            await self.send_reply(chat_id, buyer_id, item_id, text)
        except Exception as e:
            safety.trip(f"自动回复发送失败（{str(e)[:60]}），可能被禁言或触发风控")
            raise
        self.hooks.event(approvals.EVENT_OF_KIND[kind], chat_id, text)

    async def handle_paid_order(self, chat_id, item_id, buyer_id, buyer_name):
        """买家已付款：按自动发货规则发送内容（人工接管时也照常发货）"""
        async with self.chat_locks[chat_id]:
            # 你是买家的订单（你付款买别人的东西）绝对不能发货
            if not await self.is_my_item(item_id):
                logger.info(f"🛒 订单商品 {item_id} 不是你发布的（你是买家），不处理自动发货")
                return
            if self.hooks.is_new_order(chat_id, item_id):
                self.hooks.event("order", chat_id, f"{buyer_name} 已付款（商品 {item_id}）")
            item_info = await asyncio.to_thread(self.get_item_info_cached, item_id)
            item_title = (item_info or {}).get("title", "")
            delivery = self.hooks.take_delivery(chat_id, item_id, item_title, buyer_id, buyer_name)
            if not delivery:
                return
            if self.hooks.needs_approval("delivery"):
                # 卡密已经预留，同意后发出；点「不发送」会退回库存
                self.hooks.queue_reply("delivery", chat_id, buyer_id, buyer_name, item_id, item_title,
                                       "（买家已付款）", delivery["content"], delivery=delivery)
                logger.info(f"📝 {buyer_name} 已付款，发货内容已放进「待审核回复」，等你点「同意发送」")
                return
            logger.info(f"📦 自动发货给 {buyer_name}（商品 {item_id}）")
            try:
                await self.send_reply(chat_id, buyer_id, item_id, delivery["content"])
            except Exception as e:
                # 没发出去就把卡密退回库存，避免买家没收到、卡密却被扣掉
                logger.error(f"自动发货消息发送失败，已退回库存，请手动发货: {e}")
                self.hooks.rollback_delivery(delivery, buyer_name)
                safety.trip(f"自动发货消息发送失败（{str(e)[:60]}），可能被禁言或触发风控")
                return
            self.hooks.event("delivery", chat_id, f"已给 {buyer_name} 自动发货（商品 {item_title or item_id}）")

    async def handle_paid_order_notice(self, session_id):
        """
        处理闲鱼推送的「等待卖家发货」订单状态消息。
        这类消息只带会话 ID，需要从聊天记录里找出对应的买家和商品。
        """
        try:
            if session_id in self.chat_meta:
                buyer_id, buyer_name, item_id = self.chat_meta[session_id]
                chat_id = session_id
            else:
                found = self.context_manager.find_buyer_by_chat(session_id)
                if not found:
                    logger.info(f"订单 {session_id} 没有聊天记录，等待会话里的付款卡片再处理自动发货")
                    return
                chat_id, buyer_id, item_id = found
                buyer_name = "买家"
            await self.handle_paid_order(chat_id, item_id, buyer_id, buyer_name)
        except Exception as e:
            logger.error(f"处理付款通知失败: {e}")

    def is_system_card(self, message):
        """是否为闲鱼系统发出的卡片消息（买家自己打的字不是）"""
        try:
            m1 = message.get("1") or {}
            content_type = ((m1.get("6") or {}).get("3") or {}).get("4", 0)
            biz_tag = str((m1.get("10") or {}).get("bizTag", "") or "")
            return str(m1.get("7", 0)) == "1" or str(content_type) == "6" or "taskName" in biz_tag
        except Exception:
            return False

    def is_own_echo(self, chat_id, text):
        """是否为机器人刚刚自动发出的消息的回传"""
        sent = self.recent_sent.get(chat_id)
        if not sent:
            return False
        now = time.time()
        for index, (sent_text, sent_at) in enumerate(sent):
            if sent_text == text and now - sent_at < 180:
                del sent[index]
                return True
        return False

    async def send_reply(self, chat_id, to_id, item_id, text):
        """
        自动发送回复：固定带防风控延迟；使用当前连接发送，失败自动重试；
        发送成功后才写入对话上下文，发送失败抛出异常。
        """
        delay = self.hooks.human_delay(text, chat_id)
        logger.info(f"模拟人工输入，延迟发送 {delay:.2f} 秒...")
        await asyncio.sleep(delay)
        entry = (text, time.time())
        self.recent_sent[chat_id].append(entry)
        for attempt in range(3):
            try:
                # 等待期间连接可能已经重建，每次都取最新的连接
                await self.send_msg(self.ws, chat_id, to_id, text)
                break
            except Exception as e:
                if attempt == 2:
                    if entry in self.recent_sent[chat_id]:
                        self.recent_sent[chat_id].remove(entry)
                    logger.error(f"发送消息失败（已重试 3 次）: {e}")
                    raise
                logger.warning(f"发送消息失败，3 秒后重试: {e}")
                await asyncio.sleep(3)
        self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", text)

    def get_item_info_cached(self, item_id):
        """
        从数据库获取商品信息，如果不存在则从API获取并保存（同步方法，请在线程里调用）。
        保存时额外记下商品的卖家 ID（_seller_id），用来判断这个商品是不是自己发布的；
        旧版本缓存里没有卖家 ID 的，重新从 API 取一次。
        """
        item_info = self.context_manager.get_item_info(item_id)
        if item_info and "_seller_id" in item_info:
            logger.info(f"从数据库获取商品信息: {item_id}")
            return item_info
        logger.info(f"从API获取商品信息: {item_id}")
        try:
            api_result = self.xianyu.get_item_info(item_id)
        except Exception as e:
            api_result = {"error": str(e)}
        data = api_result.get('data') if isinstance(api_result, dict) else None
        if isinstance(data, dict) and isinstance(data.get('itemDO'), dict):
            item_info = dict(data['itemDO'])
            item_info['_seller_id'] = self.extract_seller_id(data)
            self.context_manager.save_item_info(item_id, item_info)
            return item_info
        logger.warning(f"获取商品信息失败: {api_result}")
        return item_info

    @staticmethod
    def extract_seller_id(data):
        """从商品详情接口的返回里找出卖家 ID"""
        seller = data.get('sellerDO') or {}
        item = data.get('itemDO') or {}
        for value in (seller.get('sellerId'), seller.get('userId'), item.get('sellerId'), item.get('userId'),
                      (item.get('trackParams') or {}).get('sellerId')):
            if value:
                return str(value)
        return ""

    async def is_my_item(self, item_id):
        """
        这个商品是不是我（当前登录的账号）发布的。
        我去问别人买东西时，聊天里的「对方」其实是卖家，这种会话绝对不能自动回复。
        查不到卖家时按「不是我的」处理，宁可不回复也不能替买家身份乱说话。
        """
        if self.hooks.is_known_own_item(item_id):
            return True
        item_info = await asyncio.to_thread(self.get_item_info_cached, item_id)
        seller_id = str((item_info or {}).get("_seller_id") or "")
        if not seller_id:
            logger.warning(f"⚠️ 查不到商品 {item_id} 的卖家，无法确认是不是你发布的，为安全起见不自动回复")
            return False
        return seller_id == str(self.myid)

    async def send_approved(self, r):
        """发送控制台里点了「同意发送」的回复"""
        chat_id = r["chat_id"]
        async with self.chat_locks[chat_id]:
            if r["kind"] != "delivery" and await asyncio.to_thread(safety.is_paused):
                await asyncio.to_thread(approvals.mark_failed, r, "防风控暂停中，没有发送，请你本人在闲鱼里回复")
                return
            try:
                await self.send_reply(chat_id, r["buyer_id"], r["item_id"], r["reply"])
            except Exception as e:
                logger.error(f"同意发送的回复没有发出去: {e}")
                await asyncio.to_thread(approvals.mark_failed, r, e)
                # 发送失败按风控信号处理：所有自动功能停 24 小时
                await asyncio.to_thread(safety.trip, f"消息发送失败（{str(e)[:60]}），可能被禁言或触发风控")
                return
            await asyncio.to_thread(approvals.mark_sent, r["id"])
            logger.info(f"✅ 已发送你同意的回复（会话 {chat_id}）: {r['reply']}")
            if r["kind"] == "delivery":
                self.hooks.event("delivery", chat_id,
                                 f"已给 {r['buyer_name'] or '买家'} 发货（商品 {r['item_title'] or r['item_id']}）")
            else:
                self.hooks.event(approvals.EVENT_OF_KIND.get(r["kind"], "ai_reply"), chat_id, r["reply"])

    async def approved_sender_loop(self):
        """每 2 秒看一下控制台有没有新同意发送的回复（只在连接正常时发送）"""
        while True:
            try:
                if self.ws_ready and self.ws is not None:
                    for r in await asyncio.to_thread(approvals.claim_approved):
                        task = asyncio.create_task(self.send_approved(r))
                        self.message_tasks.add(task)
                        task.add_done_callback(self.message_tasks.discard)
            except Exception as e:
                logger.error(f"检查待发送回复失败: {e}")
            await asyncio.sleep(2)

    async def send_msg(self, ws, cid, toid, text):
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return bool(message_stripped) and message_stripped in self.hooks.toggle_keywords()

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.hooks.manual_timeout_seconds():
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:

            try:
                message = message_data
                ack = {
                    "code": 200,
                    "headers": {
                        "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                        "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                    }
                }
                if 'app-key' in message["headers"]:
                    ack["headers"]["app-key"] = message["headers"]["app-key"]
                if 'ua' in message["headers"]:
                    ack["headers"]["ua"] = message["headers"]["ua"]
                if 'dt' in message["headers"]:
                    ack["headers"]["dt"] = message["headers"]["dt"]
                await websocket.send(json.dumps(ack))
            except Exception as e:
                pass

            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            try:
                # 判断是否为订单消息,需要自行编写付款后的逻辑
                if message['3']['redReminder'] == '等待买家付款':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'等待买家 {user_url} 付款')
                    return
                elif message['3']['redReminder'] == '交易关闭':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'买家 {user_url} 交易关闭')
                    return
                elif message['3']['redReminder'] == '等待卖家发货':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'交易成功 {user_url} 等待卖家发货')
                    # 这是闲鱼服务端推送的订单状态，买家无法伪造，可以放心触发自动发货
                    await self.handle_paid_order_notice(user_id)
                    return

            except:
                pass

            # 判断消息类型
            if self.is_typing_status(message):
                logger.debug("用户正在输入")
                return
            elif not self.is_chat_message(message):
                logger.debug("其他非聊天消息")
                logger.debug(f"原始消息: {message}")
                return

            # 处理聊天消息
            create_time = int(message["1"]["5"])
            send_user_name = message["1"]["10"]["reminderTitle"]
            send_user_id = message["1"]["10"]["senderUserId"]
            send_message = message["1"]["10"]["reminderContent"]
            
            # 时效性验证（过滤5分钟前消息）
            if (time.time() * 1000 - create_time) > self.message_expire_time:
                logger.debug("过期消息丢弃")
                return
                
            # 获取商品ID和会话ID
            url_info = message["1"]["10"]["reminderUrl"]
            item_id = url_info.split("itemId=")[1].split("&")[0] if "itemId=" in url_info else None
            chat_id = message["1"]["2"].split('@')[0]
            
            if not item_id:
                logger.warning("无法获取商品ID")
                return

            # 检查是否为卖家（自己）发送的消息
            if send_user_id == self.myid:
                # 机器人自动发出的消息会从服务器回传一次，发送时已经记录过，这里忽略
                if self.is_own_echo(chat_id, send_message):
                    return
                logger.debug("检测到卖家消息，检查是否为控制命令")

                # 检查切换命令
                if self.check_toggle_keywords(send_message):
                    mode = self.toggle_manual_mode(chat_id)
                    if mode == "manual":
                        logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                    else:
                        logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                    return

                # 记录卖家人工回复
                self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", send_message)
                logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
                return

            # 只处理自己发布的商品的聊天：如果这个商品是别人的，说明你在这个会话里是买家，对方是卖家，绝对不能自动回复
            if not await self.is_my_item(item_id):
                logger.info(f"🛒 商品 {item_id} 不是你发布的（这个会话里你是买家，{send_user_name} 是卖家），跳过，不自动回复")
                return

            logger.info(f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, 会话: {chat_id}, 消息: {send_message}")
            # 记住会话对应的买家和商品，订单状态通知只带会话 ID 时要用
            self.chat_meta[chat_id] = (send_user_id, send_user_name, item_id)

            # 黑名单买家：不回复也不通知
            if self.hooks.is_blacklisted(send_user_id):
                logger.info(f"买家 {send_user_name} 在黑名单中，跳过")
                return

            # 付款：只认闲鱼系统发出的付款卡片，买家自己打字「我已付款」不会触发发货
            if self.hooks.is_payment_message(send_message):
                if self.is_system_card(message):
                    await self.handle_paid_order(chat_id, item_id, send_user_id, send_user_name)
                    return
                logger.warning(f"⚠️ 买家 {send_user_name} 发送了付款字样，但不是闲鱼系统付款通知，不会自动发货")

            # 带中括号的是闲鱼系统卡片（如「[买家已拍下]」），不回复
            if self.is_bracket_system_message(send_message):
                logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
                return
            if self.is_system_message(message):
                logger.debug("系统消息，跳过处理")
                return

            self.hooks.event("message", chat_id, f"{send_user_name}：{send_message}")

            # 同一买家的消息排队处理（保证回复顺序、限流准确），不同买家之间互不等待
            async with self.chat_locks[chat_id]:
                await self.reply_to_buyer(chat_id, item_id, send_user_id, send_user_name, send_message)
            
        except Exception as e:
            logger.error(f"处理消息时发生错误: {str(e)}")
            logger.debug(f"原始消息: {message_data}")

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def main(self):
        # 上次关闭时正在发送的回复，不确定有没有发出，标记为失败让卖家确认
        try:
            approvals.recover_sending()
        except Exception as e:
            logger.error(f"恢复待发送回复失败: {e}")
        self.sender_task = asyncio.create_task(self.approved_sender_loop())
        while True:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    self.ws_ready = True
                    
                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))
                            
                            # 处理其他消息：每条消息单独一个任务，AI 生成和发送延迟不会卡住后续消息
                            task = asyncio.create_task(self.handle_message(message_data, websocket))
                            self.message_tasks.add(task)
                            task.add_done_callback(self.message_tasks.discard)
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                
            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                
            finally:
                self.ws_ready = False
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                
                # 如果是主动重启，立即重连；否则等待5秒
                if self.connection_restart_flag:
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info("等待5秒后重连...")
                    await asyncio.sleep(5)



def check_and_complete_env():
    """检查并补全关键环境变量"""
    # 定义关键变量及其默认无效值（占位符）
    critical_vars = {
        "API_KEY": "默认使用通义千问,apikey通过百炼模型平台获取",
        "COOKIES_STR": "your_cookies_here"
    }
    
    env_path = ENV_PATH
    updated = False
    
    for key, placeholder in critical_vars.items():
        curr_val = os.getenv(key)
        
        # 如果变量未设置，或者值等于占位符
        if not curr_val or curr_val == placeholder:
            logger.warning(f"配置项 [{key}] 未设置或为默认值，请输入")
            while True:
                val = input(f"请输入 {key}: ").strip()
                if val:
                    # 更新当前环境
                    os.environ[key] = val
                    
                    # 尝试持久化到 .env
                    try:
                        # 如果没有.env文件，先创建
                        if not os.path.exists(env_path):
                            with open(env_path, 'w', encoding='utf-8') as f:
                                pass # Create empty file
                        
                        set_key(env_path, key, val)
                        updated = True
                    except Exception as e:
                        logger.warning(f"无法自动写入.env文件，请手动保存: {e}")
                    break
                else:
                    print(f"{key} 不能为空，请重新输入")
    
    if updated:
        logger.info("新的配置已保存/更新至 .env 文件中")


if __name__ == '__main__':
    # 加载环境变量
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH)
        logger.info("已加载 .env 配置")
    
    if os.path.exists(".env.example"):
        load_dotenv(".env.example")  # 不会覆盖已存在的变量
        logger.info("已加载 .env.example 默认配置")
    
    # 配置日志级别
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    logger.info(f"日志级别设置为: {log_level}")
    
    # 交互式检查并补全配置
    check_and_complete_env()
    
    cookies_str = os.getenv("COOKIES_STR")
    bot = XianyuReplyBot()
    xianyuLive = XianyuLive(cookies_str)
    # 常驻进程
    asyncio.run(xianyuLive.main())
