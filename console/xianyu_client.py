"""
闲鱼网页端接口客户端（控制台用：同步商品、擦亮、上架）

签名方式和原项目 XianyuApis 相同：sign = md5(token&t&appKey&data)，token 取 Cookie 里 _m_h5_tk 的前半段。
每次请求都从 .env 读取最新 Cookie（机器人会自动续期写回），接口返回的新 token 只保存在内存里，
不回写 .env，避免和机器人同时写文件互相覆盖。
"""
import json
import threading
import time

import requests
from loguru import logger

from utils.xianyu_utils import generate_sign

from . import safety

APP_KEY = "34839810"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
TOKEN_ERRORS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_ILLEGAL", "TOKEN_EXPIRED")


class XianyuError(Exception):
    pass


class RiskControlError(XianyuError):
    pass


def _parse_cookie(cookie):
    result = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            if key:
                result[key] = value
    return result


class XianyuClient:
    def __init__(self, cookie_loader):
        self._cookie_loader = cookie_loader
        self._fresh = {}
        self._base = None
        self._lock = threading.Lock()
        self.session = requests.Session()

    def cookies(self):
        raw = self._cookie_loader()
        cookies = _parse_cookie(raw)
        if not (raw or "").strip():
            raise XianyuError("还没有填写闲鱼 Cookie，请到「闲鱼账号」页填写")
        if not cookies.get("unb"):
            raise XianyuError("闲鱼 Cookie 里没有 unb（登录已失效或复制不完整），请在闲鱼网页版重新登录后再复制 Cookie")
        with self._lock:
            # 用户换了 Cookie（重新登录或换账号）时，丢弃之前接口返回的旧 token
            if raw != self._base:
                self._base = raw
                self._fresh = {}
            cookies.update(self._fresh)
        return cookies

    @property
    def user_id(self):
        return self.cookies().get("unb", "")

    def _headers(self, cookies):
        return {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.goofish.com",
            "referer": "https://www.goofish.com/",
            "user-agent": USER_AGENT,
            "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }

    def _remember(self, response):
        with self._lock:
            # 只记住签名用的 token，其他登录态 Cookie 以 .env 为准
            for key, value in response.cookies.items():
                if value and key.startswith("_m_h5_tk"):
                    self._fresh[key] = value

    def call(self, api, data, version="1.0", spm_cnt="a21ybx.im.0.0", retries=2):
        """调用 mtop 接口，成功返回 data；触发风控时熔断并抛出 RiskControlError"""
        if safety.is_paused():
            raise RiskControlError("防风控熔断中，后台任务已暂停")
        cookies = self.cookies()
        token = cookies.get("_m_h5_tk", "").split("_")[0]
        t = str(int(time.time() * 1000))
        data_val = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        params = {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": generate_sign(t, token, data_val),
            "v": version, "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "api": api, "sessionOption": "AutoLoginOnly", "spm_cnt": spm_cnt,
        }
        try:
            response = self.session.post(f"https://h5api.m.goofish.com/h5/{api}/{version}/", params=params,
                                         data={"data": data_val}, headers=self._headers(cookies), timeout=25)
        except requests.RequestException as e:
            raise XianyuError(f"网络请求失败：{e}")
        self._remember(response)
        try:
            result = response.json()
        except ValueError:
            raise XianyuError(f"接口返回异常：{response.text[:200]}")

        ret = " ".join(result.get("ret") or [])
        if "SUCCESS" in ret:
            return result.get("data") or {}
        if safety.is_risk_response(ret):
            safety.trip(f"{api} 触发风控（{ret[:80]}）")
            raise RiskControlError(f"触发闲鱼风控：{ret}")
        if any(code in ret for code in TOKEN_ERRORS) and retries > 0:
            # 响应里已经带回了新的 _m_h5_tk，重试一次即可
            logger.info(f"{api} token 过期，已刷新后重试")
            time.sleep(0.8)
            return self.call(api, data, version, spm_cnt, retries - 1)
        if "SESSION_EXPIRED" in ret or "FAIL_SYS_SESSION" in ret:
            raise XianyuError("闲鱼登录已失效，请更新 Cookie")
        raise XianyuError(ret or "接口调用失败")

    def upload_image(self, image_bytes, filename="item.jpg"):
        """上传商品图片，返回 {url, width, height}"""
        if safety.is_paused():
            raise RiskControlError("防风控熔断中，后台任务已暂停")
        cookies = self.cookies()
        headers = self._headers(cookies)
        headers.pop("content-type")
        headers["accept"] = "*/*"
        try:
            response = self.session.post(
                "https://stream-upload.goofish.com/api/upload.api",
                params={"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"},
                files={"file": (filename, image_bytes, "image/jpeg")},
                headers=headers, timeout=60,
            )
        except requests.RequestException as e:
            raise XianyuError(f"图片上传失败：{e}")
        self._remember(response)
        if safety.is_risk_response(response.text):
            safety.trip("图片上传触发风控")
            raise RiskControlError("图片上传触发风控")
        try:
            payload = response.json()
        except ValueError:
            raise XianyuError(f"图片上传返回异常：{response.text[:200]}")
        obj = payload.get("object") or payload.get("data") or {}
        url = obj.get("url") or payload.get("url")
        if not url:
            raise XianyuError(f"图片上传失败：{str(payload)[:200]}")
        width = height = 800
        pix = str(obj.get("pix") or "")
        if "x" in pix:
            try:
                width, height = [int(v) for v in pix.lower().split("x", 1)]
            except ValueError:
                pass
        return {"url": url, "width": width, "height": height}
