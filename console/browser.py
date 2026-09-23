"""
网页截图：用本机浏览器登录自己的网站，按页面列表自动截图

- 浏览器用电脑自带的 Edge / Chrome（没有才用 Playwright 自带的 Chromium），
  登录状态保存在 data/browser_profile，只需要手动登录一次。
- 截图按日期存到 data/screenshots/2026-09-23/页面名_103015.png。
- 同一个浏览器也能登录闲鱼，然后直接读取 Cookie 填进控制台。

Playwright 的同步接口只能在创建它的线程里用，所以所有浏览器操作都交给一个专用线程执行。
"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from urllib.parse import urlparse

from loguru import logger

from . import store

PROFILE_DIR = os.path.join(store.DATA_DIR, "browser_profile")
SHOT_DIR = os.path.join(store.DATA_DIR, "screenshots")
XIANYU_URL = "https://www.goofish.com/"
# 依次尝试：Edge（Windows 自带）→ Chrome → Playwright 自带的 Chromium
CHANNELS = ("msedge", "chrome", None)
# 登录闲鱼和自己的平台要走国内直连：浏览器不使用系统代理
# （AI 模型的请求由 python.exe 发出，不受影响，照常走代理）
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled", "--start-maximized", "--no-proxy-server"]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
FILE_RE = re.compile(r"[^/\\]+\.png")

_jobs = queue.Queue()
_thread = None
_thread_lock = threading.Lock()
_task_lock = threading.Lock()
_task = {"running": False, "progress": "", "results": [], "finished_at": None}


# ---------------- 浏览器线程 ----------------

class _Browser:
    def __init__(self):
        self.pw = None
        self.ctx = None
        self.channel = ""

    def alive(self):
        """只能在浏览器线程里调用：真的发一个请求确认窗口没被用户关掉"""
        if not self.ctx:
            return False
        try:
            self.ctx.cookies("https://example.com")
            return self.ctx is not None
        except Exception:
            self.ctx = None
            return False

    def ensure(self):
        if self.alive():
            return self.ctx
        self.ctx = None
        if self.pw is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise ValueError("缺少 playwright 库，请重新运行「启动控制台.bat」自动安装依赖")
            self.pw = sync_playwright().start()
        os.makedirs(PROFILE_DIR, exist_ok=True)
        errors = []
        for channel in CHANNELS:
            try:
                ctx = self.pw.chromium.launch_persistent_context(
                    PROFILE_DIR, channel=channel, headless=False, no_viewport=True,
                    args=BROWSER_ARGS,
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:
                errors.append(f"{channel or 'chromium'}：{str(e).splitlines()[0][:120]}")
                continue
            ctx.on("close", lambda *_: self._closed())
            self.ctx, self.channel = ctx, channel or "chromium"
            logger.info(f"[截图] 已打开浏览器（{self.channel}）")
            return ctx
        logger.warning("[截图] 打不开浏览器：" + "；".join(errors))
        raise ValueError("打不开浏览器。请确认电脑装了 Edge 或 Chrome；"
                         "或者在命令行运行 python -m playwright install chromium 后再试")

    def _closed(self):
        self.ctx = None

    def close(self):
        if self.ctx:
            try:
                self.ctx.close()
            except Exception:
                pass
        self.ctx = None


_browser = _Browser()


def _loop():
    while True:
        fn, future = _jobs.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn(_browser))
        except BaseException as e:
            future.set_exception(e)


def _call(fn, timeout=120):
    """把 fn(browser) 交给浏览器线程执行并等待结果"""
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, daemon=True, name="browser")
            _thread.start()
    future = Future()
    _jobs.put((fn, future))
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise ValueError("浏览器操作超时，请看一下弹出的浏览器窗口是否卡住了")


# ---------------- 页面列表 ----------------

def _clean_url(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"网址格式不正确：{url}")
    return url


def _safe_name(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip()).strip("._")
    return (name or fallback)[:40]


def get_config():
    s = store.get_settings()
    return {"login_url": s["screenshot_login_url"], "pages": s["screenshot_pages"]}


def save_config(payload):
    login_url = _clean_url(payload.get("login_url", ""))
    pages = []
    for i, p in enumerate(payload.get("pages") or []):
        if not isinstance(p, dict):
            raise ValueError("页面参数格式不正确")
        url = _clean_url(p.get("url", ""))
        if not url:
            continue
        try:
            wait = max(0.0, min(float(p.get("wait") or 0), 60.0))
        except (TypeError, ValueError):
            raise ValueError("等待秒数请填数字")
        pages.append({
            "name": _safe_name(p.get("name"), f"页面{i + 1}"),
            "url": url,
            "full_page": bool(p.get("full_page")),
            "wait": wait,
            "selector": str(p.get("selector") or "").strip()[:200],
        })
    if len(pages) > 30:
        raise ValueError("最多 30 个页面")
    store.save_settings({"screenshot_login_url": login_url, "screenshot_pages": pages})
    return get_config()


# ---------------- 浏览器操作 ----------------

def status():
    is_open = _browser.ctx is not None
    if is_open and not _task["running"]:
        # 用户可能已经手动关掉了浏览器窗口，交给浏览器线程确认一下
        try:
            is_open = _call(lambda b: b.alive(), timeout=5)
        except Exception:
            pass
    return {"open": is_open, "channel": _browser.channel, "task": task_state()}


def _check_idle():
    if _task["running"]:
        raise ValueError("正在截图中，请等截图完成")


def open_page(url):
    """打开可见的浏览器窗口，让用户自己登录"""
    _check_idle()
    url = _clean_url(url)
    if not url:
        raise ValueError("请先填写网址")

    def job(b):
        ctx = b.ensure()
        page = ctx.pages[0] if len(ctx.pages) == 1 and ctx.pages[0].url in ("about:blank", "") else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            raise ValueError(f"网页打不开：{str(e).splitlines()[0][:150]}")
        page.bring_to_front()
        return {"channel": b.channel}

    return _call(job)


def close_browser():
    _check_idle()
    return _call(lambda b: b.close())


def _shot_one(ctx, p, folder):
    started = time.time()
    result = {"name": p["name"], "url": p["url"], "ok": False}
    page = ctx.new_page()
    try:
        page.goto(p["url"], wait_until="load", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # 有的网站一直有请求，等不到空闲也照样截
        if p.get("wait"):
            page.wait_for_timeout(int(p["wait"] * 1000))
        filename = f"{p['name']}_{datetime.now():%H%M%S}.png"
        path = os.path.join(folder, filename)
        if p.get("selector"):
            page.locator(p["selector"]).first.screenshot(path=path, timeout=15000)
        else:
            page.screenshot(path=path, full_page=bool(p.get("full_page")))
        final = page.url
        result.update(ok=True, file=filename, final_url=final)
        # 被跳到登录页多半是登录过期了，截到的是登录页
        if re.search(r"login|signin|sign-in|passport|登录", final, re.I) and final.rstrip("/") != p["url"].rstrip("/"):
            result["warning"] = "被跳转到了登录页，可能需要重新登录"
    except Exception as e:
        result["error"] = str(e).splitlines()[0][:200]
    finally:
        try:
            page.close()
        except Exception:
            pass
    result["seconds"] = round(time.time() - started, 1)
    return result


def task_state():
    return {**_task, "results": list(_task["results"])}


def start_capture(names=None):
    """后台按页面列表逐个截图；names 为空表示全部"""
    pages = get_config()["pages"]
    if names:
        pages = [p for p in pages if p["name"] in set(names)]
    if not pages:
        raise ValueError("还没有要截图的页面，请先添加")
    if not _task_lock.acquire(blocking=False):
        raise ValueError("正在截图中，请稍后")
    _task.update(running=True, progress="正在打开浏览器…", results=[], finished_at=None)
    folder = os.path.join(SHOT_DIR, datetime.now().strftime("%Y-%m-%d"))

    def job(b):
        ctx = b.ensure()
        os.makedirs(folder, exist_ok=True)
        for i, p in enumerate(pages):
            _task["progress"] = f"正在截图 {i + 1}/{len(pages)}：{p['name']}"
            _task["results"].append(_shot_one(ctx, p, folder))

    def worker():
        try:
            _call(job, timeout=len(pages) * 150 + 120)
            ok = sum(1 for r in _task["results"] if r["ok"])
            _task["progress"] = f"完成：{ok}/{len(pages)} 张截图成功"
            logger.info(f"[截图] {_task['progress']}，保存在 {folder}")
        except Exception as e:
            _task["progress"] = f"出错：{e}"
            logger.error(f"[截图] 出错：{e}")
        finally:
            _task.update(running=False, finished_at=time.time())
            _task_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return task_state()


def read_xianyu_cookie():
    """从浏览器里读出闲鱼登录 Cookie（需要先在这个浏览器里登录 goofish.com）"""
    _check_idle()
    def job(b):
        if not b.alive():
            raise ValueError("浏览器没有打开，请先点「打开闲鱼登录」并登录")
        return b.ctx.cookies(["https://www.goofish.com", "https://h5api.m.goofish.com"])

    cookies = _call(job)
    jar = {}
    for c in cookies:
        jar.setdefault(c["name"], c["value"])
    if not jar.get("unb"):
        raise ValueError("还没有检测到闲鱼登录，请在打开的浏览器里登录闲鱼后再点")
    return "; ".join(f"{k}={v}" for k, v in jar.items())


# ---------------- 截图文件 ----------------

def list_shots(limit_days=30):
    days = []
    if os.path.isdir(SHOT_DIR):
        for day in sorted(os.listdir(SHOT_DIR), reverse=True)[:limit_days]:
            folder = os.path.join(SHOT_DIR, day)
            if not DATE_RE.fullmatch(day) or not os.path.isdir(folder):
                continue
            files = sorted((f for f in os.listdir(folder) if f.lower().endswith(".png")),
                           key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
            days.append({"date": day, "files": files})
    return {"folder": SHOT_DIR, "days": days}


def shot_path(day, filename):
    if not DATE_RE.fullmatch(day or "") or not FILE_RE.fullmatch(filename or "") or ".." in filename:
        raise ValueError("截图不存在")
    path = os.path.join(SHOT_DIR, day, filename)
    if not os.path.isfile(path):
        raise ValueError("截图不存在")
    return path


def delete_shot(day, filename):
    os.remove(shot_path(day, filename))


def open_folder():
    """在电脑上打开截图文件夹（控制台只在本机运行）"""
    os.makedirs(SHOT_DIR, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(SHOT_DIR)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", SHOT_DIR])
        else:
            subprocess.Popen(["xdg-open", SHOT_DIR])
    except Exception as e:
        raise ValueError(f"打不开文件夹，请手动打开：{SHOT_DIR}（{e}）")
    return SHOT_DIR
