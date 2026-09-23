"""
AI 助手：用聊天的方式操作控制台（上架、擦亮、关键词回复、自动发货规则、黑名单、回复设置、机器人启停）

模型按约定输出 JSON：{"reply": "...", "calls": [{"tool": "...", "args": {...}}]}，
不依赖各家平台的 function calling，任何 OpenAI 兼容模型都能用。

- 只读工具（查商品、查规则、看状态）助手会自己调用，结果交回给模型继续回答；
- 会改东西的工具不会直接执行，而是生成「待确认操作」，用户在页面上点「确认执行」才执行。
  待确认操作保存在服务端，确认时按保存的参数执行，页面改不了参数（上架商品可以补图片）。
"""
import json
import threading
import time
import uuid

from loguru import logger

from . import browser, rules, safety, shop, store

MAX_ROUNDS = 4
_pending = {}
_pending_lock = threading.Lock()


def _server():
    from . import server  # 避免循环导入
    return server


def _num(value):
    try:
        return round(float(value), 2) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------- 只读工具 ----------------

def _get_status(args):
    s = _server()
    acc = s.account_info()
    sf = safety.status()
    return {
        "机器人": s.bot.status(),
        "闲鱼账号": {"昵称": acc["nick"], "已填Cookie": acc["has_cookie"]},
        "防风控": {"模式": sf["level"], "今日已上架": sf["today"]["publish"], "今日已擦亮": sf["today"]["polish"],
                "暂停": sf["pause"], "夜间静默中": sf["quiet"], "上架限制": sf["publish_block"]},
        "后台任务": shop.task_state(),
    }


def _list_my_items(args):
    return [{"item_id": i["item_id"], "标题": i["title"], "价格": i.get("price"),
             "最近擦亮结果": i.get("last_polish_result") or ""} for i in shop.list_my_items()[:60]]


def _list_listings(args):
    return [{"id": l["id"], "标题": l["title"], "价格": l["price"], "状态": l["status_label"],
             "图片数": len(l["images"]), "错误": l.get("error") or ""} for l in shop.list_listings()[:40]]


def _list_keywords(args):
    return [{"id": r["id"], "关键词": r["keyword"], "匹配": r["match_type"], "回复": r["reply"],
             "限定商品": r["item_id"], "启用": bool(r["enabled"])} for r in rules.list_keywords()]


def _list_delivery_rules(args):
    # 卡密内容不发给模型，只给数量
    return [{"id": r["id"], "名称": r["name"], "商品ID": r["item_id"], "标题关键词": r["title_keyword"],
             "方式": r["mode"], "发货内容": (r["content"] or "")[:200], "剩余卡密": r["stock_count"],
             "启用": bool(r["enabled"])} for r in rules.list_delivery_rules()]


def _list_blacklist(args):
    return rules.list_blacklist()


def _list_screenshots(args):
    files = []
    for day in browser.list_shots(limit_days=7)["days"]:
        files += [f"{day['date']}/{f}" for f in day["files"]]
    return files[:40]


def _get_reply_settings(args):
    s = store.get_settings()
    return {k: s[k] for k in ("ai_enabled", "toggle_keywords", "manual_timeout_minutes", "fallback_reply",
                              "business_hours", "auto_polish")}


# ---------------- 需要确认的工具 ----------------

def _listing_payload(args, images, action):
    return {
        "title": str(args.get("title", "")).strip(),
        "description": str(args.get("description", "")).strip(),
        "price": _num(args.get("price")),
        "orig_price": _num(args.get("orig_price")),
        "post_price": _num(args.get("post_price")),
        "delivery": args.get("delivery") if args.get("delivery") in shop.DELIVERY_CHOICES else "包邮",
        "category_hint": str(args.get("category_hint", "")).strip(),
        "images": images,
        "action": action,
    }


def _screenshot_images(args):
    images = []
    for ref in (args.get("screenshots") or [])[:9]:
        day, _, filename = str(ref).partition("/")
        try:
            images.append(shop.import_image_file(browser.shot_path(day, filename)))
        except (ValueError, OSError):
            raise ValueError(f"找不到截图 {ref}")
    return images


def _create_listing(args, extra):
    images = [i for i in (extra.get("images") or []) if isinstance(i, str)]
    images = (images + _screenshot_images(args))[:9]
    action = "draft" if args.get("mode") == "draft" else "publish"
    r = shop.save_listing(_listing_payload(args, images, action))
    if action == "draft":
        return f"已存为草稿（编号 {r['id']}）"
    return f"已加入上架队列（编号 {r['id']}）" + (f"，{r['block']}，会自动顺延" if r["block"] else "，会按防风控节奏发布")


def _queue_listing(args, extra):
    listing = next((l for l in shop.list_listings() if l["id"] == int(args.get("id", 0))), None)
    if not listing:
        raise ValueError("找不到这个商品草稿")
    r = shop.save_listing({**listing, "action": "publish"})
    return "已加入上架队列" + (f"（{r['block']}，会自动顺延）" if r["block"] else "")


def _delete_listing(args, extra):
    shop.delete_listing(int(args.get("id", 0)))
    return "已删除草稿（已上架的商品不会从闲鱼删除）"


def _polish_all(args, extra):
    shop.start_polish_all()
    return "已开始批量擦亮，进度可以在「商品管理」里看"


def _sync_items(args, extra):
    shop.start_sync()
    return "已开始同步在售商品"


def _save_keyword(args, extra):
    rules.save_keyword({k: args[k] for k in ("id", "keyword", "reply", "match_type", "item_id", "enabled")
                        if k in args})
    return "关键词回复已保存"


def _delete_keyword(args, extra):
    rules.delete("keyword_rules", int(args.get("id", 0)))
    return "关键词回复已删除"


def _save_delivery_rule(args, extra):
    payload = {k: args[k] for k in ("id", "name", "item_id", "title_keyword", "mode", "content", "enabled")
               if k in args}
    stock = args.get("stock")
    if isinstance(stock, list):
        stock = "\n".join(str(s) for s in stock)
    payload["stock"] = stock or ""
    rules.save_delivery_rule(payload)
    return "自动发货规则已保存"


def _delete_delivery_rule(args, extra):
    rules.delete("delivery_rules", int(args.get("id", 0)))
    return "自动发货规则已删除"


def _add_blacklist(args, extra):
    rules.add_blacklist(str(args.get("user_id", "")), str(args.get("note", "")))
    return "已加入黑名单"


def _update_reply_settings(args, extra):
    allowed = ("ai_enabled", "toggle_keywords", "manual_timeout_minutes", "fallback_reply", "business_hours")
    _server()._save_settings({k: args[k] for k in allowed if k in args}, None)
    return "回复设置已保存"


def _bot_control(args, extra):
    action = args.get("action")
    bot = _server().bot
    if action == "start":
        bot.start()
    elif action == "stop":
        bot.stop()
    elif action == "restart":
        bot.restart()
    else:
        raise ValueError("action 只能是 start / stop / restart")
    return {"start": "机器人已启动", "stop": "机器人已停止", "restart": "机器人已重启"}[action]


def _count_lines(stock):
    if isinstance(stock, list):
        return len([x for x in stock if str(x).strip()])
    return len([x for x in str(stock or "").splitlines() if x.strip()])


def _money(v):
    v = _num(v)
    return f"¥{v:g}" if v is not None else "未定价"


# name: (说明, 参数, 执行函数, 是否需要确认, 确认时显示的说明)
TOOLS = {
    "get_status": ("查看机器人运行状态、闲鱼账号、防风控和今日上架/擦亮数量", "{}", _get_status, False, None),
    "list_my_items": ("查看闲鱼上在售的商品（来自上次同步）", "{}", _list_my_items, False, None),
    "list_listings": ("查看上架队列和草稿", "{}", _list_listings, False, None),
    "list_keywords": ("查看关键词自动回复规则", "{}", _list_keywords, False, None),
    "list_delivery_rules": ("查看自动发货规则", "{}", _list_delivery_rules, False, None),
    "list_blacklist": ("查看黑名单", "{}", _list_blacklist, False, None),
    "list_screenshots": ("查看最近的网页截图文件（可用作商品图片）", "{}", _list_screenshots, False, None),
    "get_reply_settings": ("查看 AI 自动回复、人工接管、营业时间等设置", "{}", _get_reply_settings, False, None),
    "create_listing": (
        "新建商品并上架（mode=publish 加入上架队列，mode=draft 只存草稿）。价格必须是卖家说过的；"
        "图片可以用 list_screenshots 里的文件，没有也行，卖家确认时可以再补",
        '{"title": "15-30字", "description": "80-200字", "price": 数字, "orig_price": 数字可选, '
        '"delivery": "包邮/按距离计费/一口价/无需邮寄", "post_price": 一口价时的邮费, "category_hint": "类目", '
        '"screenshots": ["日期/文件名"], "mode": "publish 或 draft"}',
        _create_listing, True,
        lambda a: f"{'上架' if a.get('mode') != 'draft' else '存草稿'}：「{a.get('title', '')}」{_money(a.get('price'))}，"
                  f"{a.get('delivery') or '包邮'}"),
    "queue_listing": ("把一个草稿或失败的商品加入上架队列", '{"id": 草稿编号}', _queue_listing, True,
                      lambda a: f"把草稿 {a.get('id')} 加入上架队列"),
    "delete_listing": ("删除一个草稿（不影响闲鱼上已上架的商品）", '{"id": 草稿编号}', _delete_listing, True,
                       lambda a: f"删除草稿 {a.get('id')}"),
    "polish_all": ("把闲鱼上所有在售商品擦亮一遍（按防风控节奏）", "{}", _polish_all, True,
                   lambda a: "擦亮所有在售商品"),
    "sync_items": ("从闲鱼同步在售商品列表", "{}", _sync_items, True, lambda a: "从闲鱼同步在售商品"),
    "save_keyword": (
        "新增或修改关键词自动回复（带 id 是修改）",
        '{"id": 可选, "keyword": "关键词", "reply": "回复内容", "match_type": "contains/exact/regex", '
        '"item_id": "只对某商品生效，可空", "enabled": true}',
        _save_keyword, True,
        lambda a: f"{'修改' if a.get('id') else '新增'}关键词回复：买家说「{a.get('keyword', '')}」→ 回复「{a.get('reply', '')}」"),
    "delete_keyword": ("删除一条关键词回复", '{"id": 编号}', _delete_keyword, True,
                       lambda a: f"删除关键词回复 {a.get('id')}"),
    "save_delivery_rule": (
        "新增或修改自动发货规则（带 id 是修改）。mode=text 每单发同样的 content；mode=cards 每单发一条卡密",
        '{"id": 可选, "name": "规则名", "item_id": "商品ID", "title_keyword": "或商品标题关键词", '
        '"mode": "text/cards", "content": "发货内容", "stock": ["卡密1", "卡密2"], "enabled": true}',
        _save_delivery_rule, True,
        lambda a: f"{'修改' if a.get('id') else '新增'}自动发货规则「{a.get('name', '')}」"
                  f"（{'卡密 %d 条' % _count_lines(a.get('stock')) if a.get('mode') == 'cards' else '固定内容'}）"),
    "delete_delivery_rule": ("删除一条自动发货规则", '{"id": 编号}', _delete_delivery_rule, True,
                             lambda a: f"删除自动发货规则 {a.get('id')}"),
    "add_blacklist": ("把买家加入黑名单（不再回复）", '{"user_id": "买家ID", "note": "备注"}', _add_blacklist, True,
                      lambda a: f"把买家 {a.get('user_id')} 加入黑名单"),
    "update_reply_settings": (
        "修改回复设置", '{"ai_enabled": true/false, "fallback_reply": "...", "toggle_keywords": "...", '
        '"manual_timeout_minutes": 数字, "business_hours": {"enabled": true, "start": "09:00", "end": "23:00", '
        '"mode": "away/silent", "away_message": "..."}}',
        _update_reply_settings, True, lambda a: "修改回复设置：" + "、".join(a.keys())),
    "bot_control": ("启动、停止或重启客服机器人", '{"action": "start/stop/restart"}', _bot_control, True,
                    lambda a: {"start": "启动", "stop": "停止", "restart": "重启"}.get(a.get("action"), "?") + "机器人"),
}


def _system_prompt():
    tools = "\n".join(
        f"- {name}{'（需卖家确认）' if t[3] else ''}：{t[0]}。参数：{t[1]}" for name, t in TOOLS.items())
    return (
        "你是闲鱼卖家的 AI 助手，住在卖家自己电脑上的闲鱼控制台里。卖家用中文和你说需求，"
        "你通过下面的工具帮他操作：上架商品、擦亮、同步商品、关键词自动回复、自动发货规则、黑名单、回复设置、启停机器人。\n"
        f"工具：\n{tools}\n\n"
        "规则：\n"
        "1. 每次只输出一个 JSON：{\"reply\": \"对卖家说的话\", \"calls\": [{\"tool\": \"工具名\", \"args\": {...}}]}，不要输出别的。\n"
        "2. 需要先查信息时调用只读工具，系统会把结果发给你，你再继续回答；不需要工具时 calls 为 []。\n"
        "3. 标了「需卖家确认」的工具不会立刻执行，会给卖家一个确认按钮，所以 reply 里要说清楚「请确认」要做什么。\n"
        "4. 信息不够时（比如上架没说价格）先在 reply 里问，不要编造价格、卡密、商品参数，不要猜 id，先用只读工具查。\n"
        "5. 写商品文案：标题 15-30 字，写清品牌型号和成色；描述 80-200 字，口语化，不要出现微信、QQ、链接等站外联系方式。"
        "不用「最」「第一」「顶级」「极致」「必备」「100%」「绝对」这类极限词；卖家没说全新就不写全新；不写售后无忧、正品保证、一手货源、批发、代发、厂家、官方、七天无理由；不列一长串卖点，像个人卖闲置一样平实。"
        "活体动物、药品、食品、烟酒、虚拟账号、证件票据、成人用品等敏感类目不能用控制台上架，直接告诉卖家到闲鱼 App 里本人处理。\n"
        "6. 做不到的事（比如修改或下架闲鱼上已经发布的商品、回复买家消息）直接说明控制台暂时不支持，建议到闲鱼 App 里操作。\n"
        "7. reply 简短、友好、用中文。"
    )


def _call_llm(messages):
    resp = shop._llm().chat.completions.create(
        model="", temperature=0.3, max_tokens=1500, timeout=90,
        messages=[{"role": "system", "content": _system_prompt()}, *messages],
    )
    text = (resp.choices[0].message.content or "").strip()
    try:
        data = shop._parse_json(resp)
    except ValueError:
        return {"reply": text or "（AI 没有回复内容）", "calls": []}, text
    calls = data.get("calls") if isinstance(data.get("calls"), list) else []
    return {"reply": str(data.get("reply") or "").strip(), "calls": calls}, text


def _add_pending(name, args):
    tool = TOOLS[name]
    action_id = uuid.uuid4().hex[:12]
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > 86400]:
            del _pending[k]
        _pending[action_id] = {"tool": name, "args": args, "created": now}
    try:
        summary = tool[4](args)
    except Exception:
        summary = name
    return {"id": action_id, "tool": name, "summary": summary, "args": args}


def chat(messages):
    """messages：页面保存的完整对话（含内部消息）。返回新增的消息和待确认操作"""
    if not isinstance(messages, list):
        raise ValueError("参数格式不正确")
    history = []
    for m in messages[-40:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip():
            history.append({"role": m["role"], "content": str(m["content"])[:6000]})
    if not history or history[-1]["role"] != "user":
        raise ValueError("请先输入你的需求")

    new_messages, actions = [], []
    for round_no in range(MAX_ROUNDS):
        if round_no:
            time.sleep(1.2)  # 免费档模型常有「每秒 1 次」的限制，连续查资料时稍微隔开
        try:
            result, raw = _call_llm(history + [{k: m[k] for k in ("role", "content")} for m in new_messages])
        except RuntimeError as e:
            raise ValueError(str(e))
        reads, writes = [], []
        for call in result["calls"]:
            if not isinstance(call, dict) or call.get("tool") not in TOOLS:
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            (writes if TOOLS[call["tool"]][3] else reads).append((call["tool"], args))
        if reads and round_no < MAX_ROUNDS - 1:
            outputs = []
            for name, args in reads[:5]:
                try:
                    out = TOOLS[name][2](args)
                except Exception as e:
                    out = f"出错：{e}"
                outputs.append(f"{name} 的结果：{json.dumps(out, ensure_ascii=False, default=str)[:4000]}")
            new_messages.append({"role": "assistant", "content": raw, "hidden": True})
            new_messages.append({"role": "user", "content": "【工具结果】\n" + "\n".join(outputs)
                                 + "\n请根据结果继续，仍然只输出 JSON。", "hidden": True})
            continue
        actions = [_add_pending(name, args) for name, args in writes[:5]]
        reply = result["reply"] or ("请确认下面的操作。" if actions else "好的。")
        new_messages.append({"role": "assistant", "content": reply})
        break
    else:
        new_messages.append({"role": "assistant", "content": "这个问题我查了几轮还没理清，换个说法再试试？"})
    return {"messages": new_messages, "actions": actions}


def confirm(action_id, extra=None):
    """执行卖家确认的操作；extra 只接受上架商品时补充的图片"""
    with _pending_lock:
        pending = _pending.pop(str(action_id), None)
    if not pending:
        raise ValueError("这个操作已经执行过或已过期，请重新让 AI 生成")
    extra = extra if isinstance(extra, dict) and pending["tool"] == "create_listing" else {}
    try:
        result = TOOLS[pending["tool"]][2](pending["args"], extra)
    except ValueError:
        with _pending_lock:
            _pending[str(action_id)] = pending  # 比如缺图片，补完可以再点确认
        raise
    logger.info(f"[AI 助手] 已执行 {pending['tool']}：{result}")
    return {"result": result}


def cancel(action_id):
    with _pending_lock:
        _pending.pop(str(action_id), None)
