"""
防风控模式

目标是让账号的自动化行为接近真人卖家：动作有间隔、有上限、夜里休息，
一旦闲鱼返回风控信号就立刻暂停所有后台任务（擦亮、上架），避免越陷越深。
没有任何办法保证 100% 不被识别，这里做的是尽量降低风险。
"""
import time

from . import notify, store

LEVELS = {
    "standard": {
        "label": "标准",
        "desc": "适合老账号、商品不多：动作间隔较短，上限较宽",
        "reply_delay_factor": 1.0,       # 自动回复延迟倍数（在固定的基础延迟上放大）
        "first_reply_extra": (0, 2),     # 新买家第一句话额外等待（秒）
        "buyer_hourly_limit": 30,        # 同一买家每小时最多自动回复条数，防止被人刷
        "publish_daily_limit": 10,       # 每天最多自动上架数
        "publish_interval_minutes": 5,   # 两次上架最短间隔
        "polish_gap": (10, 30),          # 擦亮两个商品之间的间隔（秒）
        "quiet_start": "00:30",          # 夜间静默：不执行擦亮和上架
        "quiet_end": "07:00",
        "pause_hours": 2,                # 触发风控后暂停后台任务的时长
    },
    "steady": {
        "label": "稳健（推荐）",
        "desc": "大多数卖家的最佳平衡：速度够用，行为更像真人",
        "reply_delay_factor": 1.3,
        "first_reply_extra": (2, 5),
        "buyer_hourly_limit": 20,
        "publish_daily_limit": 5,
        "publish_interval_minutes": 15,
        "polish_gap": (30, 90),
        "quiet_start": "00:00",
        "quiet_end": "08:00",
        "pause_hours": 6,
    },
    "cautious": {
        "label": "谨慎",
        "desc": "新号、刚被风控过、或者商品很多时使用：宁慢勿封",
        "reply_delay_factor": 1.6,
        "first_reply_extra": (4, 9),
        "buyer_hourly_limit": 12,
        "publish_daily_limit": 3,
        "publish_interval_minutes": 30,
        "polish_gap": (60, 180),
        "quiet_start": "23:00",
        "quiet_end": "08:30",
        "pause_hours": 12,
    },
}
DEFAULT_LEVEL = "steady"

# 闲鱼接口返回这些内容说明触发了风控或需要人工验证
RISK_MARKERS = ("RGV587", "被挤爆", "FAIL_SYS_USER_VALIDATE", "USER_VALIDATE", "哎哟喂", "punish")


def level_key():
    key = store.get_settings().get("safety_level") or DEFAULT_LEVEL
    return key if key in LEVELS else DEFAULT_LEVEL


def params():
    return LEVELS[level_key()]


def is_risk_response(text):
    return any(marker in str(text) for marker in RISK_MARKERS)


def _in_window(now_hm, start, end):
    if start <= end:
        return start <= now_hm < end
    return now_hm >= start or now_hm < end


def in_quiet_hours():
    p = params()
    return _in_window(time.strftime("%H:%M"), p["quiet_start"], p["quiet_end"])


def pause_state():
    s = store.get_settings()
    until = float(s.get("safety_pause_until") or 0)
    return {"paused": until > time.time(), "until": until, "reason": s.get("safety_pause_reason") or ""}


def is_paused():
    return pause_state()["paused"]


def trip(reason):
    """触发熔断：暂停擦亮、上架等后台任务，并推送通知"""
    hours = params()["pause_hours"]
    until = time.time() + hours * 3600
    current = pause_state()
    if current["paused"] and current["until"] >= until:
        return
    store.save_settings({"safety_pause_until": until, "safety_pause_reason": reason})
    store.log_event("risk", "", f"防风控熔断：{reason}，后台任务暂停 {hours} 小时")
    notify.notify("risk", "防风控已暂停后台任务",
                  f"{reason}\n擦亮、上架等任务已暂停 {hours} 小时。建议打开闲鱼网页版过一下滑块并更新 Cookie。")


def resume():
    store.save_settings({"safety_pause_until": 0, "safety_pause_reason": ""})


def count_today(event_type):
    local = time.localtime()
    start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    return store.row("SELECT COUNT(*) AS n FROM events WHERE type = ? AND created_at >= ?",
                     (event_type, start))["n"]


def publish_block_reason():
    """返回不能上架的原因；可以上架时返回 None"""
    if is_paused():
        return "防风控熔断中"
    if in_quiet_hours():
        return "夜间静默时段"
    p = params()
    if count_today("publish") >= p["publish_daily_limit"]:
        return f"今天已达上架上限（{p['publish_daily_limit']} 个）"
    last = store.row("SELECT MAX(created_at) AS t FROM events WHERE type = 'publish'")["t"]
    if last and time.time() - last < p["publish_interval_minutes"] * 60:
        return f"距离上次上架不足 {p['publish_interval_minutes']} 分钟"
    return None


def status():
    p = params()
    return {
        "level": level_key(),
        "params": p,
        "levels": LEVELS,
        "pause": pause_state(),
        "quiet": in_quiet_hours(),
        "today": {"publish": count_today("publish"), "polish": count_today("polish")},
        "publish_block": publish_block_reason(),
    }
