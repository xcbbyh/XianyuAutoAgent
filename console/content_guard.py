"""
按闲鱼规则做的发送前安全检查（依据：禁言事件后整理的「闲鱼规则与机器人安全操作」）

- skip_reason()：哪些聊天机器人根本不起草回复，交给卖家本人（活体动物等敏感商品、问「你是AI吗」、看不懂的「啥」「？」）
- check_reply()：回复发出前检查敏感词、AI 腔（真人模式）、同一聊天的发送频率、和之前发过的话是否几乎一样；有问题就不让发
"""
import difflib
import re
import time

from . import store

# 活体动物等敏感商品：闲鱼对活体交易管得很严，聊天一律交给卖家本人
SENSITIVE_ITEM = re.compile(
    r"活体|幼犬|幼猫|宠物狗|宠物猫|狗狗|猫咪|小狗|小猫|边牧|边境牧羊|柯基|泰迪|金毛|拉布拉多|哈士奇|萨摩耶|柴犬|比熊|博美|"
    r"雪纳瑞|法斗|德牧|阿拉斯加|布偶猫|英短|美短|蓝猫|橘猫|暹罗|仓鼠|兔子|鹦鹉|乌龟|龟苗|观赏鱼|锦鲤|蛇|蜥蜴|守宫|"
    r"处方药|药品|医疗器械")

# 对方在问是不是机器人：不回，提醒卖家本人接管
ASK_BOT = re.compile(r"(你|您)是(不是)?(ai|AI|Ai|机器人|自动回复|人工智能|真人吗)|机器人吗|自动回复吗|是真人吗|是人吗")

# 对方明显不是来买东西的，或者没看懂在说什么
CONFUSED = re.compile(r"^\s*(啥|啥\?|啥？|什么|什么\?|什么？|\?+|？+|。+|…+|你在说什么.*|你说啥.*|说的什么.*)\s*$")

# 闲鱼敏感词：命中就不能发
BANNED = [
    ("站外联系方式", ["微信", "VX", "vx", "V信", "威信", "薇信", "QQ", "qq", "扣扣", "手机号", "电话", "加我", "私聊",
                "二维码", "链接", "网盘", "+V", "+v", "力口我", "wx"]),
    ("绕开平台交易", ["线下交易", "转账", "支付宝", "银行卡", "红包", "定金转我", "不走平台", "面交付款"]),
    ("电商腔/经营性用语", ["全新", "售后无忧", "七天无理由", "一手货源", "批发", "厂家", "代发", "正品保证", "官方"]),
    ("广告法极限词", ["最便宜", "最好", "第一", "100%", "绝对", "顶级"]),
    ("活体相关", ["快递发货", "包活", "包纯", "疫苗齐全"]),
    ("自称官方/客服", ["客服", "店铺"]),
]

# 真人模式：拼到回复类提示词的最后（优先级最高）
HUMAN_STYLE_PROMPT = """

【真人模式（必须遵守，优先级最高）】
1. 你就是卖家本人在手机上打字，像跟朋友聊天一样自然随意，一两句话说完，不超过30字
2. 不列卖点，不用「/」或一串顿号堆词，不分点，不换行
3. 不用表情符号，不用感叹号，不用「亲亲」「哦~」「呢~」「为您服务」这类客服腔
4. 不推销、不催单，对方问什么只答什么
5. 绝对不说自己是 AI、机器人、助手或客服，也不要暗示；对方问你是不是 AI，只输出 -
6. 每次换自己的话说，不重复前面说过的句子
7. 商品信息里没写的不编，不确定就说「我看下再回你」
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
SIMILAR_RATIO = 0.8     # 和之前发过的话相似度超过 80% 就不发

SENT_TYPES = ("ai_reply", "keyword_reply", "away_reply")


def skip_reason(message, item_title="", item_desc=""):
    """返回不起草回复的原因；返回空字符串表示可以起草"""
    if CONFUSED.match(message or ""):
        return "对方的话看起来不是在问商品（例如「啥」「？」），机器人不回"
    if ASK_BOT.search(message or ""):
        return "对方在问你是不是 AI / 机器人，请你本人回复"
    hit = SENSITIVE_ITEM.search(f"{item_title} {item_desc}")
    if hit:
        return f"商品涉及「{hit.group(0)}」（闲鱼对活体动物、药品管得很严），机器人不回，请你本人处理"
    return ""


def banned_words(text, item_title=""):
    hits = []
    for label, words in BANNED:
        for w in words:
            if w in (text or "") and not (w == "全新" and "全新" in (item_title or "")):
                hits.append(f"{w}（{label}）")
    return hits


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


def check_reply(chat_id, text, item_title="", kind="ai"):
    """
    发出前的检查，返回问题列表（空列表表示可以发）。
    自动发货内容（卡密）是买家付款后应得的，不做这些检查。
    """
    if kind == "delivery":
        return []
    problems = []
    words = banned_words(text, item_title)
    if words:
        problems.append("包含闲鱼敏感词：" + "、".join(words) + "，请改掉再发")
    tone = ai_tone(text, item_title)
    if tone:
        problems.append("不像真人说话：" + "；".join(tone) + "，请改一改")
    day = recent_sent(chat_id, 86400)
    if day:
        gap = time.time() - day[0]["created_at"]
        if gap < MIN_GAP_SECONDS:
            problems.append(f"这个聊天 {int(gap)} 秒前刚发过一条，两条之间至少隔 2 分钟，请稍后再点")
        if sum(1 for r in day if time.time() - r["created_at"] < 3600) >= MAX_PER_HOUR:
            problems.append(f"这个聊天一小时内已经自动发了 {MAX_PER_HOUR} 条，发太多容易被判骚扰，请你本人在闲鱼里回复")
        elif len(day) >= MAX_PER_DAY:
            problems.append(f"这个聊天今天已经自动发了 {MAX_PER_DAY} 条，请你本人在闲鱼里回复")
        for r in day:
            if difflib.SequenceMatcher(None, r["detail"] or "", text or "").ratio() >= SIMILAR_RATIO:
                problems.append("和这个聊天之前发过的一句话几乎一样，重复发容易被判垃圾信息，请改一改")
                break
    return problems
