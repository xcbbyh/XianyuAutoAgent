"""
按闲鱼规则做的发送前安全检查（依据：禁言事件后整理的「闲鱼规则与机器人安全操作」）

- skip_reason()：哪些聊天机器人根本不起草回复，交给卖家本人（活体动物等敏感商品、问「你是AI吗」、看不懂的「啥」「？」）
- check_reply()：回复发出前检查敏感词、AI 腔（真人模式）、同一聊天的发送频率、和之前发过的话是否几乎一样；有问题就不让发
"""
import difflib
import re
import time

from . import safety, store

# 敏感类目（《闲鱼规则大全》第 7、10 节）：活体动物、药品、食品、虚拟账号、证件票据、成人用品等，机器人不回，交给卖家本人
SENSITIVE_ITEM = re.compile(
    # 活体动物
    r"活体|幼犬|幼猫|宠物狗|宠物猫|狗狗|猫咪|小狗|小猫|边牧|边境牧羊|柯基|泰迪|金毛|拉布拉多|哈士奇|萨摩耶|柴犬|比熊|博美|"
    r"雪纳瑞|法斗|德牧|阿拉斯加|布偶猫|英短|美短|蓝猫|橘猫|暹罗|仓鼠|鹦鹉|乌龟|龟苗|观赏鱼|锦鲤|蜥蜴|守宫|猫舍|犬舍|种公|"
    r"幼犬|狗崽|猫崽|奶猫|奶狗|宠物兔|垂耳兔|荷兰猪|龙猫|刺猬|雪貂|松鼠|八哥|玄凤|鸽子|雏鸟|鸟苗|金鱼|热带鱼|孔雀鱼|"
    r"斗鱼|鱼苗|龙鱼|角蛙|爬宠|异宠|宠物蛇|玉米蛇|球蟒|蜘蛛|独角仙|蚂蚁工坊|蝎子|送养|领养|繁育|赛级|双血统|"
    # 保护动物制品、黄赌毒、危险品
    r"象牙|犀角|玳瑁|穿山甲|毒品|赌博|安全气囊|"
    # 受限实物、虚拟课程
    r"化妆品|内衣|网盘课程|"
    # 医药保健
    r"处方药|药品|药物|医疗器械|保健品|口罩|"
    # 食品、烟酒
    r"食品|零食|奶粉|辅食|月子|保质期|烟草|香烟|白酒|红酒|啤酒|"
    # 虚拟账号、票证卡
    r"游戏账号|账号出售|出售账号|成品号|手机卡|流量卡|电话卡|发票|证件|代办证|"
    # 成人、武器、危险品、假货
    r"成人用品|情趣|催情|仿真枪|水弹枪|管制刀具|弹药|易燃|易爆|高仿|A货|原单|空瓶")

# 对方在要联系方式或骂人：不回，交给卖家本人
ASK_CONTACT = re.compile(r"微信|VX|vx|Vx|V信|v信|威信|薇信|QQ|qq|扣扣|企鹅|手机号|电话号|加你|加我|加个[vV]|\+[vV]|"
                         r"(?<![A-Za-z])(wx|WX)(?![A-Za-z])|二维码|联系方式")
INSULT = re.compile(r"傻[逼bB]|(?<![A-Za-z])(sb|SB)(?![A-Za-z])|滚开|滚蛋|给我滚|神经病|你有病|骗子|垃圾东西|垃圾卖家|妈的|他妈|去死|脑残")

# 对方在问是不是机器人：不回，提醒卖家本人接管
ASK_BOT = re.compile(r"(你|您)是(不是)?(ai|AI|Ai|机器人|自动回复|人工智能|真人吗)|机器人吗|自动回复吗|是真人吗|是人吗")

# 对方明显不是来买东西的，或者没看懂在说什么
CONFUSED = re.compile(r"^\s*(啥|啥\?|啥？|什么|什么\?|什么？|\?+|？+|。+|…+|你在说什么.*|你说啥.*|说的什么.*)\s*$")

# 闲鱼敏感词：命中就不能发
# 违规词（《闲鱼规则大全》第 8 节）：命中就不能发
BANNED = [
    ("站外联系方式", ["微信", "VX", "vx", "V信", "威信", "薇信", "QQ", "qq", "扣扣", "企鹅", "手机号", "电话", "加我",
                "私聊", "二维码", "链接", "网盘", "加群", "进群", "群号", "直播", "+V", "+v", "力口", "wx", "http", "www."]),
    ("绕开平台交易", ["线下交易", "转账", "支付宝", "银行卡", "红包", "定金转我", "不走平台", "面交付款"]),
    ("社交刷量", ["互粉", "互赞", "互想要", "赚钱", "兼职", "日结"]),
    ("电商腔/经营性用语", ["全新", "售后无忧", "七天无理由", "一手货源", "批发", "厂家", "代发", "正品保证", "官方",
                   "品质保证", "超值"]),
    ("广告法极限词", ["最便宜", "最好", "最低价", "第一", "100%", "绝对", "顶级", "极致", "历史最低", "必备"]),
    ("假货暗示", ["高仿", "A货", "原单", "尾单", "复刻", "1:1"]),
    ("功效医疗", ["疗效", "治疗", "祛湿", "减肥", "壮阳", "保健"]),
    ("低俗迷信", ["开光", "辟邪", "转运"]),
    ("活体相关", ["快递发货", "包活", "包纯", "疫苗齐全", "繁殖", "种公", "猫舍", "犬舍"]),
    ("自称官方/客服", ["客服", "店铺"]),
]

# 真人模式：拼到回复类提示词的最后（优先级最高）
HUMAN_STYLE_PROMPT = """

【真人模式（必须遵守，优先级最高）】
1. 你就是卖家本人在手机上打字，像平时发消息一样自然随意，一两句话写在一行，不超过30字
2. 不列卖点，不用「/」或一串顿号堆词，不分点，不换行，不复述商品页上对方能看到的价格和标题
3. 不用表情符号，不用感叹号，不用每句都「亲」，不用「亲亲」「哦~」「呢~」「为您服务」这类客服腔，一条最多一个语气词
4. 可以口语一点，比如「在的」「可以」「嗯」「行」「我看下」「晚点回你」
5. 不推销、不催单，对方问什么只答什么；价格和优惠只按商品信息和对话里说过的来，不自己加
6. 绝对不说自己是 AI、机器人、助手或客服，也不要暗示；对方问你是不是 AI、表示没看懂（「啥」「？」）、要联系方式或骂人，只输出 -
7. 不提微信、QQ、电话、链接、转账、线下交易；不用「全新」「售后无忧」「官方」「最」「第一」「100%」这类词
8. 每次换自己的话说，不重复前面说过的句子
9. 商品信息里没写的不编，不确定就说「我看下再回你」
"""

# 一看就是 AI / 客服写的话
AI_SELF = re.compile(r"AI|ai助手|人工智能|机器人|语言模型|大模型|作为.{0,6}(助手|客服)|我是.{0,4}(助手|客服)|智能回复|自动回复")
SALES_TONE = ["亲亲", "亲~", "哦~", "呢~", "哈~", "欢迎咨询", "竭诚", "为您服务", "感谢您的", "有任何问题", "随时联系",
              "随时咨询", "放心购买", "放心拍", "想要就下单", "赶紧", "手慢无", "抓紧", "欢迎下单", "欢迎选购", "性价比超高"]
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
MAX_HUMAN_LEN = 60

MIN_GAP_SECONDS = 120   # 同一个聊天两条自动回复之间至少隔 2 分钟
MAX_PER_HOUR = 3        # 同一个聊天每小时最多 3 条
MAX_PER_DAY = 10        # 同一个聊天每天最多 10 条
MAX_PEOPLE_PER_HOUR = 15  # 全账号每小时最多给 15 个人自动回复
NIGHT = ("23:00", "08:30")  # 夜里不发
SIMILAR_RATIO = 0.8     # 和之前发过的话相似度超过 80% 就不发
MAX_SAME_TEXT_PER_HOUR = 3  # 同一句话一小时内最多原样发给 3 个人

SENT_TYPES = ("ai_reply", "keyword_reply", "away_reply")


def skip_reason(message, item_title="", item_desc=""):
    """返回不起草回复的原因；返回空字符串表示可以起草"""
    if CONFUSED.match(message or ""):
        return "对方的话看起来不是在问商品（例如「啥」「？」），机器人不回"
    if ASK_BOT.search(message or ""):
        return "对方在问你是不是 AI / 机器人，请你本人回复"
    if ASK_CONTACT.search(message or ""):
        return "对方提到了微信、QQ、电话等站外联系方式，机器人不回，你本人回「平台上聊就行」即可"
    if INSULT.search(message or ""):
        return "对方的话里有骂人或情绪很大的词，机器人不回，请你本人处理"
    hit = SENSITIVE_ITEM.search(f"{item_title} {item_desc}")
    if hit:
        return f"商品涉及「{hit.group(0)}」（闲鱼的敏感类目），机器人不回，请你本人处理"
    hit = SENSITIVE_ITEM.search(message or "")
    if hit:
        return f"对方的话涉及「{hit.group(0)}」（闲鱼的敏感类目），机器人不回，请你本人处理"
    return ""


def _contains(text, word):
    # 两个英文字母的短词（vx、qq、wx）前后不能紧挨着英文字母，免得随机卡密里的字母组合被误判
    if len(word) <= 2 and word.isascii() and word.isalpha():
        return re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text) is not None
    return word in text


def banned_words(text, item_title=""):
    hits = []
    for label, words in BANNED:
        for w in words:
            if _contains(text or "", w) and not (w == "全新" and "全新" in (item_title or "")):
                hits.append(f"{w}（{label}）")
    return hits


def is_blank(text):
    """AI 只输出了「-」或标点（表示不用回复），不能当成回复发出去"""
    return not re.sub(r"[\s\-—–－_.。,，!！?？~～…·、]+", "", text or "")


def humanize(text):
    """AI 草稿的简单清理：去掉表情符号，换行改成逗号"""
    text = EMOJI.sub("", text or "")
    text = re.sub(r"\s*\n+\s*", "，", text.strip())
    return text.strip("，").strip()


def ai_tone(text, item_title=""):
    """不像真人说话的地方"""
    text = text or ""
    found = []
    m = AI_SELF.search(text)
    if m and m.group(0) not in (item_title or ""):
        found.append(f"提到了「{m.group(0)}」，会暴露是 AI")
    tone = [w for w in SALES_TONE if w in text]
    if tone:
        found.append("客服腔/推销腔：" + "、".join(tone))
    if EMOJI.search(text):
        found.append("有表情符号")
    if "\n" in text or text.count("/") >= 2 or text.count("、") >= 3 or re.search(r"(^|\s)[1-9][.、]", text):
        found.append("像在列卖点清单")
    if text.count("！") + text.count("!") >= 2:
        found.append("感叹号太多")
    if len(text) > MAX_HUMAN_LEN:
        found.append(f"太长了（{len(text)} 字），真人一般一两句话")
    return found


def recent_sent(chat_id, seconds):
    return store.rows(
        "SELECT detail, created_at FROM events WHERE chat_id = ? AND created_at > ? AND type IN (?, ?, ?) "
        "ORDER BY created_at DESC", (str(chat_id), time.time() - seconds, *SENT_TYPES))


def _pause_problem():
    try:
        pause = safety.pause_state()
    except Exception as e:
        return f"读不到防风控状态（{e}），为安全起见先不发"
    if pause["paused"]:
        return (f"出现过风控信号（{pause['reason'] or '被禁言或发送失败'}），所有自动功能暂停到 "
                f"{time.strftime('%m-%d %H:%M', time.localtime(pause['until']))}，这段时间请你本人在闲鱼里回复")
    return ""


def is_night():
    now = time.strftime("%H:%M")
    return now >= NIGHT[0] or now < NIGHT[1]


def _night_problem():
    if is_night():
        return f"夜里 {NIGHT[0]} 到 {NIGHT[1]} 不自动发消息，明天早上再点，或者你本人在闲鱼里回复"
    return ""


def _people_problem(chat_id, include_approved=True):
    """全账号每小时最多给 15 个人自动回复；已同意、还在排队发送的也算上，防止一次同意一大批"""
    chats = {r["chat_id"] for r in store.rows(
        "SELECT DISTINCT chat_id FROM events WHERE created_at > ? AND chat_id != ? AND type IN (?, ?, ?)",
        (time.time() - 3600, str(chat_id), *SENT_TYPES))}
    if include_approved:
        chats |= {r["chat_id"] for r in store.rows(
            "SELECT DISTINCT chat_id FROM pending_replies WHERE status IN ('approved', 'sending') "
            "AND kind != 'delivery' AND chat_id != ?", (str(chat_id),))}
    if len(chats) >= MAX_PEOPLE_PER_HOUR:
        return f"这一小时已经给 {len(chats)} 个人自动回复过（含已同意待发送的），全账号每小时最多 {MAX_PEOPLE_PER_HOUR} 人，请稍后再发"
    return ""


def _gap_problem(chat_id):
    day = recent_sent(chat_id, MIN_GAP_SECONDS)
    if day:
        return f"这个聊天 {int(time.time() - day[0]['created_at'])} 秒前刚发过一条，两条之间至少隔 2 分钟，请稍后再点"
    return ""


def _normalize(text):
    return re.sub(r"[\s，。,.!！?？~～…、]+", "", text or "")


def check_reply(chat_id, text, item_title="", kind="ai"):
    """
    发出前的检查，返回问题列表（空列表表示可以发）。
    自动发货内容（卡密）不是聊天用语，不查 AI 腔和频率，但风控暂停、夜间、站外联系方式等违规词照样要查。
    """
    problems = [p for p in (_pause_problem(), _night_problem()) if p]
    words = banned_words(text, item_title)
    if words:
        problems.append("包含闲鱼敏感词：" + "、".join(words) + ("，发货内容不能改，请点「不发送」后你本人在闲鱼里发货"
                                                              if kind == "delivery" else "，请改掉再发"))
    if kind == "delivery":
        return problems
    if is_blank(text):
        problems.append("回复是空的")
    hit = SENSITIVE_ITEM.search(item_title or "")
    if hit:
        problems.append(f"商品涉及「{hit.group(0)}」（闲鱼的敏感类目），机器人不回，请你本人处理")
    people = _people_problem(chat_id)
    if people:
        problems.append(people)
    tone = ai_tone(text, item_title)
    if tone:
        problems.append("不像真人说话：" + "；".join(tone) + "，请改一改")
    day = recent_sent(chat_id, 86400)
    if day:
        gap = _gap_problem(chat_id)
        if gap:
            problems.append(gap)
        if sum(1 for r in day if time.time() - r["created_at"] < 3600) >= MAX_PER_HOUR:
            problems.append(f"这个聊天一小时内已经自动发了 {MAX_PER_HOUR} 条，发太多容易被判骚扰，请你本人在闲鱼里回复")
        elif len(day) >= MAX_PER_DAY:
            problems.append(f"这个聊天今天已经自动发了 {MAX_PER_DAY} 条，请你本人在闲鱼里回复")
        for r in day:
            if difflib.SequenceMatcher(None, r["detail"] or "", text or "").ratio() >= SIMILAR_RATIO:
                problems.append("和这个聊天之前发过的一句话几乎一样，重复发容易被判垃圾信息，请改一改")
                break
    # 同一句话一小时内发给了好几个人：闲鱼会当成群发刷屏
    same = sum(1 for r in store.rows(
        "SELECT detail FROM events WHERE created_at > ? AND chat_id != ? AND type IN (?, ?, ?)",
        (time.time() - 3600, str(chat_id), *SENT_TYPES)) if _normalize(r["detail"]) == _normalize(text))
    if same >= MAX_SAME_TEXT_PER_HOUR:
        problems.append(f"这句话一小时内已经原样发给了 {same} 个人，像群发，请换个说法")
    return problems


def send_time_problems(chat_id, kind):
    """
    同意之后真正发出之前再查一次会随时间变化的规则（同意时是白天、发的时候可能已经到夜里，
    或者中间出现了风控暂停）。自动发货同样受风控暂停和夜间限制。
    """
    problems = [p for p in (_pause_problem(), _night_problem()) if p]
    if kind != "delivery":
        problems += [p for p in (_people_problem(chat_id, include_approved=False), _gap_problem(chat_id)) if p]
    return problems
