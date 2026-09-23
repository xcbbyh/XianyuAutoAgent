"""
控制台数据存储（data/console.db）

控制台和机器人进程共用这个数据库：控制台负责写配置，机器人每次处理消息时读取，
所以大部分配置保存后立即生效，不需要重启机器人。
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "console.db")
CHAT_DB_PATH = os.path.join(DATA_DIR, "chat_history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'custom',
    description TEXT DEFAULT '',
    base_url TEXT DEFAULT '',
    model TEXT DEFAULT '',
    api_keys TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 100,
    is_default INTEGER DEFAULT 0,
    supports_search INTEGER DEFAULT 0,
    key_url TEXT DEFAULT '',
    custom INTEGER DEFAULT 0,
    last_test TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS keyword_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    match_type TEXT DEFAULT 'contains',
    reply TEXT NOT NULL,
    item_id TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    hits INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS delivery_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    item_id TEXT DEFAULT '',
    title_keyword TEXT DEFAULT '',
    mode TEXT DEFAULT 'text',
    content TEXT DEFAULT '',
    stock TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1,
    delivered INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    rule_name TEXT DEFAULT '',
    chat_id TEXT,
    item_id TEXT,
    buyer_id TEXT,
    buyer_name TEXT DEFAULT '',
    content TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS blacklist (
    user_id TEXT PRIMARY KEY,
    note TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    chat_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (created_at);
CREATE TABLE IF NOT EXISTS my_items (
    item_id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    price TEXT DEFAULT '',
    pic_url TEXT DEFAULT '',
    status TEXT DEFAULT '',
    synced_at REAL,
    last_polished_at REAL,
    last_polish_result TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    price REAL,
    orig_price REAL,
    delivery TEXT DEFAULT '包邮',
    post_price REAL,
    category_hint TEXT DEFAULT '',
    images TEXT DEFAULT '[]',
    status TEXT DEFAULT 'draft',
    scheduled_at REAL,
    item_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at REAL,
    published_at REAL
);
CREATE TABLE IF NOT EXISTS notify_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    events TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1,
    created_at REAL
);
"""

DEFAULT_SETTINGS = {
    # AI 自动回复总开关
    "ai_enabled": True,
    # 卖家发送这些字符切换人工/AI 接管
    "toggle_keywords": "。",
    # 人工接管多久后自动交还 AI
    "manual_timeout_minutes": 60,
    # AI 全部调用失败时发给买家的兜底话术，留空则不回复
    "fallback_reply": "",
    # 营业时间
    "business_hours": {
        "enabled": False,
        "start": "09:00",
        "end": "23:00",
        "mode": "away",  # away=发送离线提示，silent=不回复
        "away_message": "亲，现在是非营业时间，看到消息后会第一时间回复您~",
    },
    # 控制台启动时自动启动机器人
    "auto_start_bot": False,
    # 机器人意外退出后自动重启
    "auto_restart": True,
    # 防风控模式：standard / steady / cautious
    "safety_level": "steady",
    # 防风控熔断（程序内部维护）
    "safety_pause_until": 0,
    "safety_pause_reason": "",
    # 自动擦亮：每天在时间窗口内随机挑一个时间执行
    "auto_polish": {"enabled": False, "window_start": "08:00", "window_end": "10:00"},
    "polish_last_date": "",
}

_schema_lock = threading.Lock()
_schema_ready = False


def _connect(path=DB_PATH):
    global _schema_ready
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    if path == DB_PATH and not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                conn.commit()
                _schema_ready = True
    return conn


@contextmanager
def db(path=DB_PATH):
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(sql, params=(), path=DB_PATH):
    if path != DB_PATH and not os.path.exists(path):
        return []
    try:
        with db(path) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # 聊天库的表由机器人首次运行时创建，之前查询会失败
        if path == DB_PATH:
            raise
        return []


def row(sql, params=(), path=DB_PATH):
    result = rows(sql, params, path)
    return result[0] if result else None


def execute(sql, params=()):
    with db() as conn:
        return conn.execute(sql, params).lastrowid


# ---------------- 设置 ----------------

def get_settings():
    stored = {r["key"]: r["value"] for r in rows("SELECT key, value FROM settings")}
    result = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = default
        if key in stored:
            try:
                value = json.loads(stored[key])
            except ValueError:
                pass
        if isinstance(default, dict):
            value = {**default, **(value if isinstance(value, dict) else {})}
        result[key] = value
    return result


def save_settings(values):
    with db() as conn:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )


# ---------------- 事件与统计 ----------------

EVENT_TYPES = {
    "message": "买家消息",
    "ai_reply": "AI 回复",
    "keyword_reply": "关键词回复",
    "away_reply": "离线回复",
    "order": "买家付款",
    "delivery": "自动发货",
    "risk": "风控/登录异常",
    "publish": "自动上架",
    "polish": "擦亮",
}


def log_event(event_type, chat_id="", detail=""):
    execute(
        "INSERT INTO events (type, chat_id, detail, created_at) VALUES (?, ?, ?, ?)",
        (event_type, chat_id or "", (detail or "")[:500], time.time()),
    )


def dashboard_stats(days=7):
    now = time.time()
    local = time.localtime(now)
    today_start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    since = today_start - (days - 1) * 86400

    today = {t: 0 for t in EVENT_TYPES}
    for r in rows(
        "SELECT type, COUNT(*) AS n FROM events WHERE created_at >= ? GROUP BY type", (today_start,)
    ):
        today[r["type"]] = r["n"]

    daily = []
    for i in range(days):
        start = since + i * 86400
        daily.append({"date": time.strftime("%m-%d", time.localtime(start)), "message": 0, "replies": 0})
    for r in rows(
        "SELECT type, created_at FROM events WHERE created_at >= ? AND type IN "
        "('message', 'ai_reply', 'keyword_reply', 'away_reply')",
        (since,),
    ):
        index = int((r["created_at"] - since) // 86400)
        if 0 <= index < days:
            key = "message" if r["type"] == "message" else "replies"
            daily[index][key] += 1

    recent = rows("SELECT type, chat_id, detail, created_at FROM events ORDER BY id DESC LIMIT 12")
    buyers_today = row(
        "SELECT COUNT(DISTINCT chat_id) AS n FROM events WHERE type = 'message' AND created_at >= ?",
        (today_start,),
    )["n"]
    return {"today": today, "buyers_today": buyers_today, "daily": daily, "recent": recent}
