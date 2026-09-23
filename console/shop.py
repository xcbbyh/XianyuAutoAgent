"""
商品运营：同步在售商品、自动擦亮、自动上架（含定时队列）、AI 写文案

所有闲鱼接口调用都受防风控模式约束：夜间静默、动作间隔、每日上限、风控熔断。
后台调度线程每 30 秒检查一次：到点擦亮、按队列逐个上架。
"""
import base64
import difflib
import io
import json
import os
import random
import re
import threading
import time
import uuid

from loguru import logger

from . import notify, safety, store
from .xianyu_client import RiskControlError, XianyuClient, XianyuError

UPLOAD_DIR = os.path.join(store.DATA_DIR, "uploads")
DELIVERY_CHOICES = ("包邮", "按距离计费", "一口价", "无需邮寄")
LISTING_STATUS = {"draft": "草稿", "queued": "排队中", "publishing": "发布中", "published": "已上架", "failed": "失败"}

_client = None
_task_lock = threading.Lock()
_task_state = {"name": "", "progress": "", "running": False, "finished_at": None}


def client():
    global _client
    if _client is None:
        from .server import read_cookie
        _client = XianyuClient(read_cookie)
    return _client


def task_state():
    return dict(_task_state)


def clear_task_error():
    """换了 Cookie 之后，之前因为 Cookie 失败的提示不再显示，免得以为新 Cookie 也不行"""
    if not _task_state["running"] and "Cookie" in (_task_state.get("progress") or ""):
        _task_state.update(name="", progress="", finished_at=None)


def _run_task(name, fn):
    """后台任务同一时间只跑一个，避免并发请求像机器"""
    if not _task_lock.acquire(blocking=False):
        raise ValueError(f"「{_task_state['name']}」正在进行中，请稍后")
    _task_state.update(name=name, progress="开始", running=True, finished_at=None)

    def worker():
        try:
            fn()
        except RiskControlError as e:
            _task_state["progress"] = f"已停止：{e}"
            logger.warning(f"{name}被防风控中止：{e}")
        except Exception as e:
            _task_state["progress"] = f"出错：{e}"
            logger.error(f"{name}出错：{e}")
        finally:
            _task_state["running"] = False
            _task_state["finished_at"] = time.time()
            _task_lock.release()

    threading.Thread(target=worker, daemon=True).start()


# ---------------- 在售商品 ----------------

def sync_items():
    """拉取自己的在售商品列表，保存到 my_items"""
    uid = client().user_id
    items, page = [], 1
    while page <= 20:
        data = client().call("mtop.idle.web.xyh.item.list", {
            "needGroupInfo": False, "pageNumber": page, "pageSize": 20,
            "groupName": "在售", "groupId": "58877261", "defaultGroup": True, "userId": uid,
        })
        cards = data.get("cardList") or []
        for card in cards:
            d = card.get("cardData") or {}
            if not d.get("id"):
                continue
            price = d.get("priceInfo") or {}
            items.append({
                "item_id": str(d["id"]), "title": d.get("title", ""),
                "price": f"{price.get('preText', '')}{price.get('price', '')}",
                "pic_url": (d.get("picInfo") or {}).get("picUrl", ""),
                "status": str(d.get("itemStatus", "")),
            })
        if len(cards) < 20:
            break
        page += 1
        time.sleep(random.uniform(1.5, 3.5))
    now = time.time()
    with store.db() as conn:
        for i in items:
            conn.execute(
                "INSERT INTO my_items (item_id, title, price, pic_url, status, synced_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET title = excluded.title, price = excluded.price, "
                "pic_url = excluded.pic_url, status = excluded.status, synced_at = excluded.synced_at",
                (i["item_id"], i["title"], i["price"], i["pic_url"], i["status"], now),
            )
        # 同步不到的商品视为已下架/已售出
        conn.execute("DELETE FROM my_items WHERE synced_at < ?", (now - 1,))
    return len(items)


def list_my_items():
    return store.rows("SELECT * FROM my_items ORDER BY synced_at DESC, title")


# ---------------- 擦亮 ----------------

def polish_one(item_id):
    if safety.in_quiet_hours():
        raise ValueError("现在是防风控夜间静默时段，不执行擦亮")
    try:
        try:
            client().call("mtop.taobao.idle.item.polish", {"itemId": str(item_id)})
        except RiskControlError:
            raise
        except XianyuError:
            # 部分账号走新版接口
            client().call("mtop.idle.item.polish", {"itemId": str(item_id)})
        result = "成功"
        store.log_event("polish", "", f"擦亮商品 {item_id}")
    except RiskControlError:
        raise
    except XianyuError as e:
        result = f"失败：{e}"
    store.execute("UPDATE my_items SET last_polished_at = ?, last_polish_result = ? WHERE item_id = ?",
                  (time.time(), result, str(item_id)))
    return result


def polish_all(sync_first=True):
    if sync_first:
        _task_state["progress"] = "正在同步在售商品"
        sync_items()
    items = list_my_items()
    ok = 0
    gap = safety.params()["polish_gap"]
    random.shuffle(items)  # 打乱顺序，避免每天同样的节奏
    for index, item in enumerate(items, 1):
        if safety.is_paused():
            raise RiskControlError("防风控熔断中")
        if safety.in_quiet_hours():
            raise RiskControlError("到了夜间静默时段，剩下的明天再擦亮")
        _task_state["progress"] = f"擦亮中 {index}/{len(items)}：{item['title'][:20]}"
        if polish_one(item["item_id"]) == "成功":
            ok += 1
        if index < len(items):
            time.sleep(random.uniform(*gap))
    _task_state["progress"] = f"擦亮完成：成功 {ok}/{len(items)}"
    logger.info(_task_state["progress"])
    return ok, len(items)


def start_polish_all():
    if safety.in_quiet_hours():
        raise ValueError("现在是防风控夜间静默时段，不执行擦亮")
    _run_task("批量擦亮", polish_all)


def start_sync():
    def job():
        n = sync_items()
        _task_state["progress"] = f"同步完成：{n} 个在售商品"
    _run_task("同步在售商品", job)


# ---------------- 上架草稿 ----------------

def save_image(name, data_url):
    """保存上传的图片（统一转成 JPEG，最长边不超过 2048）"""
    text = str(data_url or "")
    if "," in text and text.startswith("data:"):
        text = text.split(",", 1)[1]
    return _store_image(name, base64.b64decode(text))


def import_image_file(path):
    """把本地图片（例如网页截图）存成上架图片"""
    with open(path, "rb") as f:
        return _store_image(os.path.basename(path), f.read(), max_mb=40)


def _store_image(name, raw, max_mb=10):
    try:
        from PIL import Image
    except ImportError:
        raise ValueError("缺少 Pillow 库，请重新运行「启动控制台.bat」自动安装依赖")
    if len(raw) > max_mb * 1024 * 1024:
        raise ValueError(f"单张图片不能超过 {max_mb}MB")
    try:
        image = Image.open(io.BytesIO(raw))
        image = image.convert("RGB")
    except Exception:
        raise ValueError(f"{name} 不是有效的图片")
    image.thumbnail((2048, 2048))
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.jpg"
    image.save(os.path.join(UPLOAD_DIR, filename), format="JPEG", quality=88)
    return filename


def image_path(filename):
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", filename or ""):
        raise ValueError("图片不存在")
    return os.path.join(UPLOAD_DIR, filename)


def recover_interrupted():
    """控制台在上架过程中被关闭：不确定是否已发出，标记为失败让用户到闲鱼确认"""
    store.execute(
        "UPDATE listings SET status = 'failed', error = '上架过程中控制台被关闭，请先到闲鱼确认是否已经上架，再决定是否重新加入队列' "
        "WHERE status = 'publishing'"
    )


def list_listings():
    result = []
    for r in store.rows("SELECT * FROM listings ORDER BY id DESC"):
        r["images"] = json.loads(r["images"] or "[]")
        r["status_label"] = LISTING_STATUS.get(r["status"], r["status"])
        result.append(r)
    return result


def _num(value, name, required=False):
    if value in (None, ""):
        if required:
            raise ValueError(f"请填写{name}")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}格式不正确")
    if number < 0:
        raise ValueError(f"{name}不能为负数")
    return number


def save_listing(payload):
    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "")).strip()
    images = payload.get("images") or []
    if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
        raise ValueError("图片参数不正确")
    images = [i for i in images if i]
    delivery = payload.get("delivery", "包邮")
    if not title:
        raise ValueError("请填写商品标题")
    if len(title) > 60:
        raise ValueError("标题不能超过 60 个字")
    if not description:
        raise ValueError("请填写商品描述")
    if not images:
        raise ValueError("请至少上传一张图片")
    if len(images) > 9:
        raise ValueError("最多 9 张图片")
    for img in images:
        if not os.path.exists(image_path(img)):
            raise ValueError("有图片已丢失，请重新上传")
    if delivery not in DELIVERY_CHOICES:
        raise ValueError("运费方式不正确")
    price = _num(payload.get("price"), "售价", required=True)
    orig = _num(payload.get("orig_price"), "原价")
    post = _num(payload.get("post_price"), "邮费", required=delivery == "一口价")

    status = "queued" if payload.get("action") in ("publish", "schedule") else "draft"
    if status == "queued":
        problems = listing_problems(title, description, payload.get("category_hint", ""), payload.get("id"))
        if problems:
            raise ValueError("不能上架：" + "；".join(problems))
    scheduled = None
    if payload.get("action") == "schedule":
        scheduled = _num(payload.get("scheduled_at"), "定时时间", required=True)
        if scheduled < time.time() - 60:
            raise ValueError("定时时间不能早于现在")
    elif status == "queued":
        scheduled = time.time()

    values = (title, description, price, orig, delivery, post, str(payload.get("category_hint", "")).strip(),
              json.dumps(images), status, scheduled)
    if payload.get("id"):
        current = store.row("SELECT status FROM listings WHERE id = ?", (int(payload["id"]),))
        if not current:
            raise ValueError("草稿不存在")
        if current["status"] in ("publishing", "published"):
            raise ValueError("已上架的商品不能再编辑，请到闲鱼里修改")
        store.execute(
            "UPDATE listings SET title = ?, description = ?, price = ?, orig_price = ?, delivery = ?, post_price = ?, "
            "category_hint = ?, images = ?, status = ?, scheduled_at = ?, error = '' WHERE id = ?",
            (*values, int(payload["id"])),
        )
        listing_id = int(payload["id"])
    else:
        listing_id = store.execute(
            "INSERT INTO listings (title, description, price, orig_price, delivery, post_price, category_hint, images, "
            "status, scheduled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, time.time()),
        )
    return {"id": listing_id, "status": status, "block": safety.publish_block_reason() if status == "queued" else None}


def listing_problems(title, description, category_hint="", listing_id=None):
    """按《闲鱼规则大全》第 4、7、8、10 节检查上架文案：敏感类目不上架，违规词不能有，同款不重复发"""
    from . import content_guard
    problems = []
    text = f"{title} {description} {category_hint or ''}"
    hit = content_guard.SENSITIVE_ITEM.search(text)
    if hit:
        problems.append(f"商品涉及「{hit.group(0)}」（闲鱼的敏感类目），不能用控制台上架，请你本人在闲鱼 App 里确认能不能发")
    words = content_guard.banned_words(f"{title}\n{description}", title)
    if words:
        problems.append("标题或描述里有闲鱼违规词：" + "、".join(words) + "，请改掉")
    recent = store.rows("SELECT id, title, description FROM listings WHERE status IN ('queued', 'publishing', 'published') "
                        "AND id != ? AND created_at > ?", (int(listing_id or 0), time.time() - 7 * 86400))
    for r in recent:
        if r["title"].strip() == title.strip() or difflib.SequenceMatcher(
                None, r["description"] or "", description).ratio() >= 0.8:
            problems.append(f"和 7 天内上架过的「{r['title'][:20]}」几乎一样，重复铺货会被降权，请换个写法或者别重复发")
            break
    return problems


def delete_listing(listing_id):
    r = store.row("SELECT images, status FROM listings WHERE id = ?", (int(listing_id),))
    if not r:
        return
    if r["status"] == "publishing":
        raise ValueError("正在发布中，稍后再删")
    store.execute("DELETE FROM listings WHERE id = ?", (int(listing_id),))
    in_use = set()
    for other in store.rows("SELECT images FROM listings"):
        in_use.update(json.loads(other["images"] or "[]"))
    for filename in json.loads(r["images"] or "[]"):
        if filename not in in_use:
            try:
                os.remove(image_path(filename))
            except (OSError, ValueError):
                pass


def _category(title, description, images, hint):
    rec_title, rec_desc = title, description
    if hint:
        rec_title = f"{hint} {title}"[:120] if hint not in title else title
        rec_desc = f"类目提示：{hint}\n{description}"
    data = client().call("mtop.taobao.idle.kgraph.property.recommend", {
        "title": rec_title, "description": rec_desc, "lockCpv": False, "multiSKU": False,
        "publishScene": "mainPublish", "scene": "newPublishChoice",
        "imageInfos": [_image_info(i) for i in images], "uniqueCode": str(int(time.time() * 1e6)),
    }, version="2.0", spm_cnt="a21ybx.publish.0.0")
    predict = data.get("categoryPredictResult") or {}
    category = {k: str(predict.get(k) or "") for k in ("catId", "catName", "channelCatId", "tbCatId")}
    if not all(category.values()):
        raise XianyuError("闲鱼没能识别商品类目，请把标题写得更具体，或填写「类目提示」后重试")
    return category, data.get("cardList") or []


def _labels(cards):
    """把类目推荐里默认选中的属性带上，和网页端手动发布一致"""
    labels = []
    for card in cards:
        d = card.get("cardData") or {}
        chosen = next((v for v in d.get("valuesList") or [] if v.get("isClicked")), None)
        if not chosen or not chosen.get("channelCatId") or not chosen.get("catName"):
            continue
        labels.append({
            "channelCateName": chosen["catName"], "channelCateId": chosen["channelCatId"],
            "tbCatId": chosen.get("tbCatId"), "labelType": "common", "propertyName": d.get("propertyName"),
            "propertyId": d.get("propertyId"), "isUserClick": "1", "from": "newPublishChoice",
            "labelFrom": "newPublish", "text": chosen["catName"],
            "properties": f"{d.get('propertyId')}##{d.get('propertyName')}:{chosen['channelCatId']}##{chosen['catName']}",
            "valueId": None, "valueName": None, "subPropertyId": None, "subValueId": None, "labelId": None,
            "isUserCancel": None,
        })
    return labels


def _image_info(image):
    return {"extraInfo": {"isH": "false", "isT": "false", "raw": "false"}, "isQrCode": False,
            "url": image["url"], "heightSize": image["height"], "widthSize": image["width"],
            "major": True, "type": 0, "status": "done"}


def _address():
    data = client().call("mtop.taobao.idle.local.poi.get", {"longitude": 116.40, "latitude": 39.90},
                         spm_cnt="a21ybx.publish.0.0")
    addresses = data.get("commonAddresses") or []
    if not addresses:
        raise XianyuError("闲鱼账号没有常用地址，请先在闲鱼 App 里手动发布一次商品设置发货地址")
    return addresses[0]


def _find_item_id(node):
    if isinstance(node, dict):
        for key in ("itemId", "idleItemId", "item_id"):
            value = str(node.get(key) or "")
            if value.isdigit() and len(value) >= 6:
                return value
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            found = _find_item_id(value)
            if found:
                return found
    return None


def publish_listing(listing):
    images = []
    for filename in json.loads(listing["images"]):
        with open(image_path(filename), "rb") as f:
            images.append(client().upload_image(f.read(), filename))
        time.sleep(random.uniform(1, 2.5))
    category, cards = _category(listing["title"], listing["description"], images, listing["category_hint"])
    time.sleep(random.uniform(1, 2))
    addr = _address()

    post_fee = {"canFreeShipping": False, "supportFreight": False, "onlyTakeSelf": False}
    delivery = listing["delivery"]
    if delivery == "包邮":
        post_fee.update(canFreeShipping=True, supportFreight=True)
    elif delivery == "按距离计费":
        post_fee.update(supportFreight=True, templateId="-100")
    elif delivery == "一口价":
        post_fee.update(supportFreight=True, templateId="0",
                        postPriceInCent=str(int(round((listing["post_price"] or 0) * 100))))
    else:
        post_fee["templateId"] = "0"

    price = {"priceInCent": str(int(round(listing["price"] * 100)))}
    if listing["orig_price"]:
        price["origPriceInCent"] = str(int(round(listing["orig_price"] * 100)))

    payload = {
        "freebies": False, "itemTypeStr": "b", "quantity": "1", "simpleItem": "true",
        "imageInfoDOList": [_image_info(i) for i in images],
        "itemTextDTO": {"desc": listing["description"], "title": listing["title"],
                        "titleDescSeparate": listing["description"] != listing["title"]},
        "itemLabelExtList": _labels(cards),
        "itemPriceDTO": price, "defaultPrice": False,
        "userRightsProtocols": [{"enable": False, "serviceCode": "SKILL_PLAY_NO_MIND"}],
        "itemPostFeeDTO": post_fee,
        "itemAddrDTO": {
            "area": addr.get("area", ""), "city": addr.get("city", ""), "divisionId": addr.get("divisionId", 0),
            "gps": f"{addr.get('longitude')},{addr.get('latitude')}", "poiId": addr.get("poiId", ""),
            "poiName": addr.get("poi", ""), "prov": addr.get("prov", ""),
        },
        "itemCatDTO": category,
        "uniqueCode": str(int(time.time() * 1e6)),
        "sourceId": "pcMainPublish", "bizcode": "pcMainPublish", "publishScene": "pcMainPublish",
    }
    time.sleep(random.uniform(2, 5))  # 模拟人填完表单再点发布
    data = client().call("mtop.idle.pc.idleitem.publish", payload, spm_cnt="a21ybx.publish.0.0")
    return _find_item_id(data) or ""


def run_publish(listing_id):
    listing = store.row("SELECT * FROM listings WHERE id = ?", (int(listing_id),))
    if not listing or listing["status"] != "queued":
        return
    # 更新前就排进队列的商品，发布前也按现在的规则再查一遍
    problems = listing_problems(listing["title"], listing["description"], listing["category_hint"], listing["id"])
    if problems:
        store.execute("UPDATE listings SET status = 'failed', error = ? WHERE id = ?",
                      (("没有上架：" + "；".join(problems))[:300], listing["id"]))
        _task_state["progress"] = f"没有上架：{problems[0]}"
        return
    store.execute("UPDATE listings SET status = 'publishing', error = '' WHERE id = ?", (listing["id"],))
    # 失败的尝试同样算一次操作，防止连续失败时频繁请求闲鱼
    store.log_event("publish_attempt", "", listing["title"])
    _task_state["progress"] = f"正在上架：{listing['title'][:20]}"
    try:
        item_id = publish_listing(listing)
        store.execute("UPDATE listings SET status = 'published', item_id = ?, published_at = ? WHERE id = ?",
                      (item_id, time.time(), listing["id"]))
        store.log_event("publish", "", f"已上架「{listing['title']}」{item_id}")
        notify.notify("delivery", "自动上架成功", f"「{listing['title']}」已上架 {item_id}")
        _task_state["progress"] = f"上架成功：{listing['title'][:20]}"
    except RiskControlError as e:
        # 风控：放回队列，熔断结束后再试
        store.execute("UPDATE listings SET status = 'queued', error = ? WHERE id = ?", (str(e), listing["id"]))
        raise
    except Exception as e:
        store.execute("UPDATE listings SET status = 'failed', error = ? WHERE id = ?", (str(e)[:300], listing["id"]))
        _task_state["progress"] = f"上架失败：{e}"
        logger.error(f"上架「{listing['title']}」失败：{e}")


# ---------------- AI 写文案 ----------------

def _llm():
    """控制台里配置的模型；都没配置时退回旧版 .env 里的 API_KEY"""
    from .llm import RoutedClient
    from .server import env_values

    fallback = None
    key = (env_values().get("API_KEY") or "").strip()
    if key and "百炼" not in key:
        from openai import OpenAI
        fallback = OpenAI(api_key=key, base_url=env_values().get("MODEL_BASE_URL") or None)
    return RoutedClient(fallback)


def _parse_json(resp):
    text = (resp.choices[0].message.content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except ValueError:
        raise ValueError("AI 返回的格式不对，请再试一次")
    if not isinstance(data, dict):
        raise ValueError("AI 返回的格式不对，请再试一次")
    return data


def ai_write(brief):
    brief = str(brief or "").strip()
    if not brief:
        raise ValueError("先简单描述一下你要卖的东西，比如：九成新 iPad Air 5 64G 蓝色，带原装充电器")
    try:
        data = _parse_json(_ai_complete(_llm(), brief))
    except RuntimeError as e:
        raise ValueError(str(e))
    return {k: str(data.get(k, "")).strip() for k in ("title", "description", "category_hint")}


CHAT_FIELDS = ("title", "description", "price", "orig_price", "category_hint", "delivery", "post_price")
CHAT_SYSTEM = (
    "你是闲鱼上架助手，帮卖家通过聊天把一件商品整理成可以直接发布的闲鱼商品。\n"
    "规则：\n"
    "1. 根据卖家说的话填写或修改商品草稿；卖家要求改哪里就只改哪里，其余保持不变。\n"
    "2. 标题 15-30 个字，写清品牌型号和成色；描述 80-200 字，口语化，分几行写成色、配件、购买渠道/时间、出售原因、交易说明。\n"
    "3. 不要编造卖家没提到的参数、成色和瑕疵；不要出现微信、QQ、手机号、链接等站外联系方式。"
    "不用「最」「第一」「顶级」「极致」「必备」「100%」「绝对」这类极限词；卖家没说全新就不写全新；不写售后无忧、正品保证、一手货源、批发、代发、厂家、官方、七天无理由；不列一长串卖点，像个人卖闲置一样平实。"
    "活体动物、药品、食品、烟酒、虚拟账号、证件票据、成人用品等敏感类目不帮忙写，在 reply 里说这类商品控制台不能上架。\n"
    "4. 售价只能用卖家说过或明确同意的价格；卖家没给价格时 price 填 null，并在 reply 里问他想卖多少（可以给一个参考区间）。\n"
    "5. delivery 只能是 包邮、按距离计费、一口价、无需邮寄 之一，默认 包邮；虚拟商品/卡密/服务类用 无需邮寄；"
    "选 一口价 时 post_price 填邮费。\n"
    "6. reply 是对卖家说的话，简短友好，说明你改了什么；信息不全就提问。卖家还没上传图片时提醒他上传或从截图里选图。\n"
    '只输出 JSON：{"reply": "...", "draft": {"title": "...", "description": "...", "price": 数字或null, '
    '"orig_price": 数字或null, "category_hint": "...", "delivery": "包邮", "post_price": 数字或null}}'
)


def _clean_draft(raw, base):
    draft = dict(base)
    for key in CHAT_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("price", "orig_price", "post_price"):
            try:
                value = round(float(value), 2) if value not in (None, "") else None
            except (TypeError, ValueError):
                value = draft.get(key)
            if value is not None and value < 0:
                value = None
        else:
            value = str(value or "").strip()
        draft[key] = value
    if draft.get("delivery") not in DELIVERY_CHOICES:
        draft["delivery"] = "包邮"
    draft["title"] = str(draft.get("title") or "")[:60]
    return draft


def ai_chat(messages, draft=None, has_images=False):
    """AI 上架助手：根据对话更新商品草稿，返回 AI 的回复和新草稿（不会直接发布）"""
    if not isinstance(messages, list) or not messages:
        raise ValueError("请先说说你想卖什么")
    history = []
    for m in messages[-20:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip():
            history.append({"role": m["role"], "content": str(m["content"])[:2000]})
    if not history or history[-1]["role"] != "user":
        raise ValueError("请先说说你想卖什么")
    base = _clean_draft(draft if isinstance(draft, dict) else {}, {k: None for k in CHAT_FIELDS})
    context = (f"当前商品草稿：{json.dumps(base, ensure_ascii=False)}\n"
               f"卖家{'已经' if has_images else '还没有'}添加商品图片。")
    try:
        resp = _llm().chat.completions.create(
            model="", temperature=0.5, max_tokens=1200, timeout=90,
            messages=[{"role": "system", "content": CHAT_SYSTEM + "\n\n" + context}, *history],
        )
    except RuntimeError as e:
        raise ValueError(str(e))
    data = _parse_json(resp)
    new_draft = _clean_draft(data.get("draft") if isinstance(data.get("draft"), dict) else {}, base)
    reply = str(data.get("reply") or "").strip() or "草稿已更新，请看右边。"
    return {"reply": reply, "draft": new_draft}


def _ai_complete(llm, brief):
    return llm.chat.completions.create(
        model="", temperature=0.7, max_tokens=800,
        messages=[
            {"role": "system", "content": (
                "你是在闲鱼上卖自己闲置的普通人，写真实、口语化的二手商品文案。"
                "根据卖家给的信息写标题和描述，不要编造卖家没提到的参数和瑕疵情况，不要出现微信、QQ、链接等站外联系方式。"
                "不用「最」「第一」「顶级」「极致」「必备」「100%」「绝对」这类极限词；卖家没说全新就不写全新；不写售后无忧、正品保证、一手货源、批发、代发、厂家、官方、七天无理由；不列一长串卖点，像个人卖闲置一样平实。"
                "标题 15-30 个字，写清品牌型号和成色；描述 80-200 字，分几行写：成色、配件、购买渠道/时间、出售原因、交易说明。"
                '只输出 JSON：{"title": "...", "description": "...", "category_hint": "商品类目，如 平板电脑"}')},
            {"role": "user", "content": brief},
        ],
    )


# ---------------- 调度 ----------------

class Scheduler:
    """每 30 秒检查一次：到点自动擦亮、按队列逐个上架"""

    def __init__(self):
        self._polish_at = {}  # 日期 -> 今天随机选定的擦亮时间

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.warning(f"调度任务出错：{e}")
            time.sleep(30)

    def tick(self):
        if safety.is_paused() or safety.in_quiet_hours() or _task_state["running"]:
            return
        from .server import read_cookie
        if not read_cookie():
            return
        self._check_polish()
        if not _task_state["running"]:
            self._check_publish()

    def _check_polish(self):
        s = store.get_settings()
        conf = s["auto_polish"]
        today = time.strftime("%Y-%m-%d")
        if not conf.get("enabled") or s.get("polish_last_date") == today:
            return
        key = (today, conf["window_start"], conf["window_end"])
        if key not in self._polish_at:
            # 在窗口内随机选一个时间，每天不一样；窗口改了就重新选
            start_h, start_m = map(int, conf["window_start"].split(":"))
            end_h, end_m = map(int, conf["window_end"].split(":"))
            start, end = start_h * 60 + start_m, end_h * 60 + end_m
            minute = random.randint(start, max(start, end - 1))
            self._polish_at = {key: f"{minute // 60:02d}:{minute % 60:02d}"}
            logger.info(f"今天的自动擦亮时间：{self._polish_at[key]}")
        now = time.strftime("%H:%M")
        # 过了窗口（比如电脑刚开机）就等明天，不在窗口外擦亮
        if self._polish_at[key] <= now < conf["window_end"]:
            try:
                _run_task("自动擦亮", polish_all)
            except ValueError:
                return  # 有别的任务在跑，下一轮再试
            store.save_settings({"polish_last_date": today})

    def _check_publish(self):
        due = store.row("SELECT id FROM listings WHERE status = 'queued' AND scheduled_at <= ? "
                        "ORDER BY scheduled_at, id LIMIT 1", (time.time(),))
        if not due or safety.publish_block_reason():
            return
        _run_task("自动上架", lambda: run_publish(due["id"]))

    def next_polish_time(self):
        conf = store.get_settings()["auto_polish"]
        return self._polish_at.get((time.strftime("%Y-%m-%d"), conf["window_start"], conf["window_end"]))


scheduler = Scheduler()
