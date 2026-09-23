"""
控制台网页后台

只监听本机 127.0.0.1。所有接口（注册登录除外）都需要登录；
写操作只接受 JSON 请求，并校验 Host，防止其他网页跨站操作。
"""
import atexit
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import dotenv_values, set_key

from . import auth, notify, providers, rules, store

BASE_DIR = store.BASE_DIR
ENV_PATH = os.path.join(BASE_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(BASE_DIR, ".env.example")
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
STATIC_DIR = os.path.join(BASE_DIR, "webui")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

HOST = "127.0.0.1"
PORT = int(os.getenv("WEBUI_PORT", "8765"))
SESSION_COOKIE = "xy_session"

PROMPTS = {
    "price_prompt": ("议价专家", "买家砍价时使用：写清底价、每轮最多让多少、什么情况下包邮"),
    "default_prompt": ("默认客服", "普通咨询时使用：说话风格、发货时间、售后规则"),
    "tech_prompt": ("技术专家", "问参数、型号、对比时使用，会联网搜索（仅通义千问支持）"),
    "classify_prompt": ("意图分类", "判断买家意图的内部提示词，一般不需要改"),
}


# ---------------- .env 与闲鱼 Cookie ----------------

def ensure_env_file():
    if not os.path.exists(ENV_PATH):
        if os.path.exists(ENV_EXAMPLE_PATH):
            shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
        else:
            open(ENV_PATH, "w", encoding="utf-8").close()


def env_values():
    return dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}


def read_cookie():
    cookie = (env_values().get("COOKIES_STR") or "").strip()
    return "" if cookie == "your_cookies_here" else cookie


def parse_cookie(cookie):
    result = {}
    for part in cookie.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            result[key] = value
    return result


def account_info():
    cookie = read_cookie()
    parsed = parse_cookie(cookie)
    nick = unquote(parsed.get("tracknick", ""))
    if "\\u" in nick:
        try:
            nick = nick.encode("latin-1").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return {
        "has_cookie": bool(cookie),
        "cookie_length": len(cookie),
        "user_id": parsed.get("unb", ""),
        "nick": nick,
        "valid_format": "unb" in parsed,
        "updated_at": os.path.getmtime(ENV_PATH) if os.path.exists(ENV_PATH) else None,
    }


def save_cookie(cookie):
    cookie = "".join(str(cookie).splitlines()).strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if not cookie:
        raise ValueError("Cookie 不能为空")
    if "unb=" not in cookie:
        raise ValueError("这段 Cookie 里没有 unb 字段，说明没有登录或复制不完整，请在已登录的闲鱼网页版重新复制")
    ensure_env_file()
    set_key(ENV_PATH, "COOKIES_STR", cookie, quote_mode="never")
    return cookie


# ---------------- 提示词 ----------------

def prompt_path(name, example=False):
    return os.path.join(PROMPT_DIR, f"{name}{'_example' if example else ''}.txt")


def read_prompts():
    result = []
    for name, (label, hint) in PROMPTS.items():
        custom = os.path.exists(prompt_path(name))
        path = prompt_path(name) if custom else prompt_path(name, example=True)
        content = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        result.append({"name": name, "label": label, "hint": hint, "content": content, "custom": custom})
    return result


def save_prompt(name, content):
    if name not in PROMPTS:
        raise ValueError("未知的提示词")
    if not str(content).strip():
        raise ValueError("提示词不能为空")
    os.makedirs(PROMPT_DIR, exist_ok=True)
    with open(prompt_path(name), "w", encoding="utf-8") as f:
        f.write(content)


def reset_prompt(name):
    if name not in PROMPTS:
        raise ValueError("未知的提示词")
    if os.path.exists(prompt_path(name)):
        os.remove(prompt_path(name))


# ---------------- 聊天记录与商品（读取机器人自己的数据库） ----------------

def _item_title(data):
    try:
        return json.loads(data).get("title", "")
    except (ValueError, AttributeError, TypeError):
        return ""


def list_chats():
    chats = store.rows(
        """
        SELECT m.chat_id, m.item_id, MAX(m.timestamp) AS last_time, COUNT(*) AS total,
               (SELECT content FROM messages m2 WHERE m2.chat_id = m.chat_id ORDER BY m2.id DESC LIMIT 1) AS last_message,
               (SELECT user_id FROM messages m3 WHERE m3.chat_id = m.chat_id AND m3.role = 'user' LIMIT 1) AS buyer_id,
               (SELECT count FROM chat_bargain_counts b WHERE b.chat_id = m.chat_id) AS bargain_count
        FROM messages m WHERE m.chat_id IS NOT NULL
        GROUP BY m.chat_id ORDER BY last_time DESC LIMIT 300
        """,
        path=store.CHAT_DB_PATH,
    )
    titles = {r["item_id"]: _item_title(r["data"])
              for r in store.rows("SELECT item_id, data FROM items", path=store.CHAT_DB_PATH)}
    for chat in chats:
        chat["item_title"] = titles.get(chat["item_id"], "")
    return chats


def chat_messages(chat_id):
    return store.rows(
        "SELECT role, content, timestamp FROM messages WHERE chat_id = ? ORDER BY id",
        (chat_id,), path=store.CHAT_DB_PATH,
    )


def list_items():
    result = []
    for r in store.rows("SELECT item_id, data, price, last_updated FROM items ORDER BY last_updated DESC",
                        path=store.CHAT_DB_PATH):
        try:
            data = json.loads(r["data"])
        except ValueError:
            data = {}
        result.append({
            "item_id": r["item_id"],
            "title": data.get("title", ""),
            "price": r["price"],
            "desc": (data.get("desc") or "")[:120],
            "sold_price": data.get("soldPrice"),
            "quantity": data.get("quantity"),
            "last_updated": r["last_updated"],
        })
    return result


# ---------------- 机器人进程 ----------------

class BotProcess:
    """以子进程方式运行 main.py，收集日志，识别风控，异常退出时自动重启"""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.logs = collections.deque(maxlen=5000)
        self.seq = 0
        self.online = False
        self.awaiting_cookie = False
        self.cookie_invalid = False
        self.last_exit_code = None
        self.started_at = None
        self.user_stopped = False
        self.restart_times = collections.deque(maxlen=10)

    def _append(self, line):
        with self.lock:
            self.seq += 1
            self.logs.append((self.seq, time.time(), line))

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        if not read_cookie():
            raise ValueError("请先在「闲鱼账号」页填写 Cookie")
        env = os.environ.copy()
        active = providers.active_providers()
        if active:
            # 兼容原有启动检查：把默认平台传给 main.py，实际调用走多模型路由
            env.update(API_KEY=active[0]["api_keys"][0], MODEL_BASE_URL=active[0]["base_url"],
                       MODEL_NAME=active[0]["model"])
        else:
            key = (env_values().get("API_KEY") or "").strip()
            if not key or "百炼" in key:
                raise ValueError("请先在「AI 模型」页配置并启用至少一个模型")
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.online = False
        self.awaiting_cookie = False
        self.cookie_invalid = False
        self.last_exit_code = None
        self.user_stopped = False
        self.started_at = time.time()
        self._append("[控制台] 正在启动机器人...")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"], cwd=BASE_DIR,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if "连接注册完成" in line:
                self.online = True
            elif "WebSocket连接已关闭" in line or "连接发生错误" in line:
                self.online = False
            if "触发风控" in line:
                self.awaiting_cookie = True
                store.log_event("risk", "", "触发闲鱼风控，需要过滑块并更新 Cookie")
                notify.notify("risk", "触发风控", "闲鱼要求滑块验证，请打开控制台更新 Cookie，否则机器人会停止")
            elif "Cookie已更新" in line:
                self.awaiting_cookie = False
            if "Cookie已失效" in line:
                self.cookie_invalid = True
                store.log_event("risk", "", "Cookie 已失效")
                notify.notify("risk", "Cookie 已失效", "闲鱼登录已失效，机器人已停止，请在控制台更新 Cookie 后重新启动")
            self._append(line)
            if "KeyError: 'unb'" in line:
                self._append("[控制台] Cookie 不完整（缺少 unb 字段），请确认闲鱼网页版已登录，再重新复制完整 Cookie")
        code = proc.wait()
        if proc is not self.proc:
            return
        self.online = False
        self.awaiting_cookie = False
        # 手动停止不算异常退出
        self.last_exit_code = 0 if self.user_stopped else code
        self._append("[控制台] 机器人已停止" if self.user_stopped else f"[控制台] 机器人已退出（退出码 {code}）")
        self._maybe_restart()

    def _maybe_restart(self):
        if self.user_stopped or self.cookie_invalid or self.last_exit_code in (0, None):
            return
        if not store.get_settings().get("auto_restart"):
            return
        now = time.time()
        recent = [t for t in self.restart_times if now - t < 3600]
        if len(recent) >= 5:
            self._append("[控制台] 1 小时内已自动重启 5 次，不再自动重启，请查看日志排查原因")
            return
        self.restart_times.append(now)
        self._append("[控制台] 机器人意外退出，15 秒后自动重启...")

        def restart():
            if not self.running() and not self.user_stopped:
                try:
                    self.start()
                except ValueError as e:
                    self._append(f"[控制台] 自动重启失败：{e}")

        threading.Timer(15, restart).start()

    def stop(self):
        self.user_stopped = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self._append("[控制台] 正在停止机器人...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def submit_cookie(self, cookie):
        """风控时机器人在等待输入新 Cookie，把它写进子进程的标准输入"""
        cookie = save_cookie(cookie)
        if self.running() and self.awaiting_cookie:
            self.proc.stdin.write(cookie + "\n")
            self.proc.stdin.flush()
            self.awaiting_cookie = False
            self._append("[控制台] 已提交新的 Cookie")

    def status(self):
        return {
            "running": self.running(),
            "online": self.online,
            "awaiting_cookie": self.awaiting_cookie,
            "cookie_invalid": self.cookie_invalid,
            "last_exit_code": self.last_exit_code,
            "started_at": self.started_at if self.running() else None,
        }

    def logs_since(self, since):
        with self.lock:
            return {"lines": [item for item in self.logs if item[0] > since], "last": self.seq}


bot = BotProcess()


# ---------------- 接口 ----------------

def _require_admin(user):
    if not user["is_admin"]:
        raise PermissionError("只有管理员可以进行这个操作")


def _overview():
    stats = store.dashboard_stats()
    active = providers.active_providers()
    return {
        **stats,
        "bot": bot.status(),
        "account": account_info(),
        "model": {"name": active[0]["name"], "model": active[0]["model"], "count": len(active)} if active else None,
        "settings": store.get_settings(),
        "counts": {
            "keywords": store.row("SELECT COUNT(*) AS n FROM keyword_rules WHERE enabled = 1")["n"],
            "delivery": store.row("SELECT COUNT(*) AS n FROM delivery_rules WHERE enabled = 1")["n"],
            "channels": store.row("SELECT COUNT(*) AS n FROM notify_channels WHERE enabled = 1")["n"],
        },
    }


def _save_settings(payload, user):
    settings = store.get_settings()
    for key in store.DEFAULT_SETTINGS:
        if key in payload:
            settings[key] = payload[key]
    hours = settings["business_hours"]
    for field in ("start", "end"):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(hours.get(field, ""))):
            raise ValueError("营业时间格式应为 HH:MM，例如 09:00")
    settings["manual_timeout_minutes"] = max(1, int(settings.get("manual_timeout_minutes") or 60))
    if not str(settings.get("toggle_keywords") or "").strip():
        raise ValueError("人工接管关键词不能为空")
    store.save_settings(settings)
    return store.get_settings()


def _bot_action(action):
    def handler(payload, user):
        if action == "start":
            bot.start()
        elif action == "stop":
            bot.stop()
        else:
            bot.stop()
            bot.start()
        return bot.status()
    return handler


GET_ROUTES = {
    "/api/status": lambda q, u: bot.status(),
    "/api/logs": lambda q, u: bot.logs_since(int(q.get("since", ["0"])[0] or 0)),
    "/api/overview": lambda q, u: _overview(),
    "/api/settings": lambda q, u: store.get_settings(),
    "/api/providers": lambda q, u: {"providers": providers.list_providers(), "categories": providers.CATEGORIES},
    "/api/keywords": lambda q, u: {"rules": rules.list_keywords(), "match_types": rules.MATCH_TYPES},
    "/api/delivery": lambda q, u: {"rules": rules.list_delivery_rules(), "records": rules.list_deliveries(),
                                   "modes": rules.DELIVERY_MODES},
    "/api/blacklist": lambda q, u: rules.list_blacklist(),
    "/api/notify": lambda q, u: {"channels": notify.list_channels(), "types": notify.CHANNEL_TYPES,
                                 "events": notify.NOTIFY_EVENTS},
    "/api/account": lambda q, u: account_info(),
    "/api/prompts": lambda q, u: read_prompts(),
    "/api/chats": lambda q, u: list_chats(),
    "/api/messages": lambda q, u: chat_messages(q.get("chat_id", [""])[0]),
    "/api/items": lambda q, u: list_items(),
    "/api/users": lambda q, u: auth.list_users(),
}

POST_ROUTES = {
    "/api/bot/start": _bot_action("start"),
    "/api/bot/stop": _bot_action("stop"),
    "/api/bot/restart": _bot_action("restart"),
    "/api/settings": _save_settings,
    "/api/providers/save": lambda p, u: providers.save_provider(p),
    "/api/providers/create": lambda p, u: providers.create_custom(p),
    "/api/providers/delete": lambda p, u: providers.delete_custom(p.get("id", "")),
    "/api/providers/default": lambda p, u: providers.set_default(p.get("id", "")),
    "/api/providers/test": lambda p, u: providers.test_provider(p.get("id", "")),
    "/api/keywords/save": lambda p, u: rules.save_keyword(p),
    "/api/keywords/delete": lambda p, u: rules.delete("keyword_rules", p.get("id")),
    "/api/delivery/save": lambda p, u: rules.save_delivery_rule(p),
    "/api/delivery/delete": lambda p, u: rules.delete("delivery_rules", p.get("id")),
    "/api/blacklist/add": lambda p, u: rules.add_blacklist(p.get("user_id", ""), p.get("note", "")),
    "/api/blacklist/delete": lambda p, u: store.execute("DELETE FROM blacklist WHERE user_id = ?",
                                                        (str(p.get("user_id", "")),)),
    "/api/notify/save": lambda p, u: notify.save_channel(p),
    "/api/notify/delete": lambda p, u: rules.delete("notify_channels", p.get("id")),
    "/api/notify/test": lambda p, u: notify.send(p.get("type"), p.get("url", ""), "测试通知",
                                                 "收到这条消息说明通知配置成功"),
    "/api/account/cookie": lambda p, u: bot.submit_cookie(p.get("cookie", "")) or account_info(),
    "/api/prompts/save": lambda p, u: save_prompt(p.get("name", ""), p.get("content", "")),
    "/api/prompts/reset": lambda p, u: reset_prompt(p.get("name", "")),
    "/api/users/create": lambda p, u: _require_admin(u) or auth.create_user(
        p.get("username"), p.get("password"), bool(p.get("is_admin"))),
    "/api/users/delete": lambda p, u: _require_admin(u) or auth.delete_user(p.get("id"), u["id"]),
    "/api/users/password": lambda p, u: auth.change_password(u["id"], p.get("old_password"),
                                                             p.get("new_password")),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _host_allowed(self):
        # 防止 DNS 重绑定：只接受以本机地址访问
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _token(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_allowed():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[url.path]
            with open(os.path.join(STATIC_DIR, filename), "rb") as f:
                return self._send(200, f.read(), content_type)
        if url.path == "/api/auth/state":
            user = auth.user_by_token(self._token())
            return self._send(200, {"has_users": auth.has_users(), "user": user})
        handler = GET_ROUTES.get(url.path)
        if not handler:
            return self._send(404, {"error": "not found"})
        user = auth.user_by_token(self._token())
        if not user:
            return self._send(401, {"error": "请先登录"})
        try:
            return self._send(200, handler(parse_qs(url.query), user))
        except Exception as e:
            return self._send(500, {"error": f"出错了：{e}"})

    def do_POST(self):
        # 只接受 JSON：浏览器跨站提交 JSON 会被拦截，防止其他网页偷偷操作控制台
        if not self._host_allowed() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/auth/register":
                current = auth.user_by_token(self._token())
                if auth.has_users() and not (current and current["is_admin"]):
                    raise PermissionError("已经有账号了，请直接登录；新增账号请让管理员在「账号与安全」里添加")
                auth.create_user(payload.get("username"), payload.get("password"), is_admin=not auth.has_users())
                return self._login(payload)
            if path == "/api/auth/login":
                return self._login(payload)
            if path == "/api/auth/logout":
                auth.logout(self._token())
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

            handler = POST_ROUTES.get(path)
            if not handler:
                return self._send(404, {"error": "not found"})
            user = auth.user_by_token(self._token())
            if not user:
                return self._send(401, {"error": "请先登录"})
            result = handler(payload, user)
            return self._send(200, {"ok": True, "data": result})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"出错了：{e}"})

    def _login(self, payload):
        token = auth.login(payload.get("username"), payload.get("password"))
        max_age = auth.SESSION_DAYS * 86400
        return self._send(200, {"ok": True}, headers={
            "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"})


def main():
    ensure_env_file()
    providers.ensure_presets()
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 被占用，控制台可能已经开着了，直接打开 {url}")
        webbrowser.open(url)
        return
    atexit.register(bot.stop)
    print("=" * 56)
    print(f" 闲鱼 AutoAgent 控制台已启动：{url}")
    print(" 请在浏览器里操作；这个窗口不要关（关掉机器人也会停）")
    print("=" * 56)
    if store.get_settings().get("auto_start_bot"):
        try:
            bot.start()
        except ValueError as e:
            print(f"自动启动机器人失败：{e}")
    threading.Timer(1, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()
