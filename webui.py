"""
闲鱼 AutoAgent 本地控制台

运行后会在浏览器打开 http://127.0.0.1:8765 ，可以在网页上：
- 启动 / 停止机器人，查看实时日志
- 填写 API Key、Cookie 等配置
- 编辑四个专家的提示词
- 查看历史对话记录

控制台只监听本机 127.0.0.1，局域网和外网都访问不到。
"""
import atexit
import collections
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values, set_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(BASE_DIR, ".env.example")
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
DB_PATH = os.path.join(BASE_DIR, "data", "chat_history.db")
INDEX_PATH = os.path.join(BASE_DIR, "webui", "index.html")

HOST = "127.0.0.1"
PORT = int(os.getenv("WEBUI_PORT", "8765"))

PROMPTS = {
    "price_prompt": "议价专家",
    "default_prompt": "默认客服",
    "tech_prompt": "技术专家",
    "classify_prompt": "意图分类",
}
CONFIG_KEYS = [
    "API_KEY",
    "COOKIES_STR",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "TOGGLE_KEYWORDS",
    "SIMULATE_HUMAN_TYPING",
]
SECRET_KEYS = {"API_KEY", "COOKIES_STR"}


def read_config():
    """合并 .env.example 默认值和 .env 中的配置"""
    config = {}
    if os.path.exists(ENV_EXAMPLE_PATH):
        config.update(dotenv_values(ENV_EXAMPLE_PATH))
    if os.path.exists(ENV_PATH):
        config.update({k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None})
    return config


def secret_is_set(key, config):
    """密钥类配置是否已填写（等于示例文件里的占位符也算未填写）"""
    value = config.get(key) or ""
    placeholder = dotenv_values(ENV_EXAMPLE_PATH).get(key) if os.path.exists(ENV_EXAMPLE_PATH) else None
    return bool(value.strip()) and value != placeholder


def mask(value):
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}****{value[-4:]}"


def ensure_env_file():
    if not os.path.exists(ENV_PATH):
        if os.path.exists(ENV_EXAMPLE_PATH):
            shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
        else:
            open(ENV_PATH, "w", encoding="utf-8").close()


def save_config(values):
    ensure_env_file()
    for key in CONFIG_KEYS:
        if key not in values:
            continue
        value = str(values[key]).strip()
        if key == "COOKIES_STR":
            value = "".join(value.splitlines())
        # 密钥留空表示不修改
        if key in SECRET_KEYS and not value:
            continue
        set_key(ENV_PATH, key, value, quote_mode="never")


def prompt_path(name, example=False):
    suffix = "_example" if example else ""
    return os.path.join(PROMPT_DIR, f"{name}{suffix}.txt")


def read_prompts():
    result = []
    for name, label in PROMPTS.items():
        custom = os.path.exists(prompt_path(name))
        path = prompt_path(name) if custom else prompt_path(name, example=True)
        content = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        result.append({"name": name, "label": label, "content": content, "custom": custom})
    return result


def save_prompt(name, content):
    if name not in PROMPTS:
        raise ValueError("未知的提示词")
    os.makedirs(PROMPT_DIR, exist_ok=True)
    with open(prompt_path(name), "w", encoding="utf-8") as f:
        f.write(content)


def query_db(sql, params=()):
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # 机器人还没建表
        return []
    finally:
        conn.close()


def list_chats():
    chats = query_db(
        """
        SELECT m.chat_id, m.item_id, MAX(m.timestamp) AS last_time, COUNT(*) AS total,
               (SELECT content FROM messages m2 WHERE m2.chat_id = m.chat_id
                ORDER BY m2.id DESC LIMIT 1) AS last_message,
               (SELECT count FROM chat_bargain_counts b WHERE b.chat_id = m.chat_id) AS bargain_count
        FROM messages m
        WHERE m.chat_id IS NOT NULL
        GROUP BY m.chat_id
        ORDER BY last_time DESC
        LIMIT 200
        """
    )
    titles = {}
    for row in query_db("SELECT item_id, data FROM items"):
        try:
            titles[row["item_id"]] = json.loads(row["data"]).get("title", "")
        except (ValueError, AttributeError):
            pass
    for chat in chats:
        chat["item_title"] = titles.get(chat["item_id"], "")
    return chats


def chat_messages(chat_id):
    return query_db(
        "SELECT role, content, timestamp FROM messages WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    )


class BotProcess:
    """以子进程方式运行 main.py，并收集它的日志输出"""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.logs = collections.deque(maxlen=3000)
        self.seq = 0
        self.online = False
        self.awaiting_cookie = False
        self.last_exit_code = None

    def _append(self, line):
        with self.lock:
            self.seq += 1
            self.logs.append((self.seq, line))

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        config = read_config()
        missing = [k for k in ("API_KEY", "COOKIES_STR") if not secret_is_set(k, config)]
        if missing:
            raise ValueError("请先在「配置」页填写：" + "、".join(missing))

        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.online = False
        self.awaiting_cookie = False
        self.last_exit_code = None
        self._append("[控制台] 正在启动机器人...")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            cwd=BASE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
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
            elif "Cookie已更新" in line:
                self.awaiting_cookie = False
            self._append(line)
            if "KeyError: 'unb'" in line:
                self._append("[控制台] Cookie 不完整（缺少 unb 字段），请确认闲鱼网页版已登录，再重新复制完整 Cookie")
        code = proc.wait()
        if proc is self.proc:
            self.online = False
            self.awaiting_cookie = False
            self.last_exit_code = code
        self._append(f"[控制台] 机器人已停止（退出码 {code}）")

    def stop(self):
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
        """风控时机器人在等待输入新 Cookie，这里把它写进子进程的标准输入"""
        cookie = "".join(cookie.split("\n")).strip()
        if not cookie:
            raise ValueError("Cookie 不能为空")
        save_config({"COOKIES_STR": cookie})
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
            "last_exit_code": self.last_exit_code,
        }

    def logs_since(self, since):
        with self.lock:
            lines = [item for item in self.logs if item[0] > since]
            return {"lines": lines, "last": self.seq}


bot = BotProcess()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _host_allowed(self):
        # 防止 DNS 重绑定：只接受以本机地址访问
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_allowed():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            with open(INDEX_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            return self._send(200, bot.status())
        if url.path == "/api/logs":
            since = int(query.get("since", ["0"])[0] or 0)
            return self._send(200, bot.logs_since(since))
        if url.path == "/api/config":
            config = read_config()
            result = {}
            for key in CONFIG_KEYS:
                if key in SECRET_KEYS:
                    is_set = secret_is_set(key, config)
                    value = config.get(key) or ""
                    hint = ""
                    if is_set:
                        hint = f"已填写（{len(value)} 个字符）" if key == "COOKIES_STR" else f"已填写（{mask(value)}）"
                    result[key] = {"set": is_set, "hint": hint}
                else:
                    result[key] = config.get(key) or ""
            return self._send(200, result)
        if url.path == "/api/prompts":
            return self._send(200, read_prompts())
        if url.path == "/api/chats":
            return self._send(200, list_chats())
        if url.path == "/api/messages":
            return self._send(200, chat_messages(query.get("chat_id", [""])[0]))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        # 只接受 JSON 请求：浏览器跨站提交 JSON 会被拦截，防止其他网页偷偷操作控制台
        if not self._host_allowed() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/start":
                bot.start()
            elif path == "/api/stop":
                bot.stop()
            elif path == "/api/restart":
                bot.stop()
                bot.start()
            elif path == "/api/config":
                save_config(payload)
            elif path == "/api/prompts":
                save_prompt(payload.get("name", ""), payload.get("content", ""))
            elif path == "/api/cookie":
                bot.submit_cookie(payload.get("cookie", ""))
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True, **bot.status()})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"出错了：{e}"})


def main():
    ensure_env_file()
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 被占用，控制台可能已经开着了，直接打开 {url}")
        webbrowser.open(url)
        return
    atexit.register(bot.stop)
    print("=" * 50)
    print(f" 闲鱼 AutoAgent 控制台已启动：{url}")
    print(" 请在浏览器里操作，这个窗口不要关（关掉机器人也会停）")
    print("=" * 50)
    threading.Timer(1, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
