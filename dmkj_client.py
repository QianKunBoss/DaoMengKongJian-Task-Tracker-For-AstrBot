"""到梦空间 v6 客户端（vendored，自包含）。

搬运自已验证的 dmkj/crypto.py + dmkj/transports.py（v6 加密根因已修复）：
- session_key 必须是 16 位 [A-Za-z0-9] ASCII 串（非 os.urandom），否则服务端 UTF-8 规范化后 AES key 失真 -> 11002。
- RSA 公钥模数 9eed81...f6ab、ReIV 1628092121312213 仍有效。
- 登录走明文（DES 加密密码），活动列表走 build_d 加密。
"""
import base64
import hashlib
import json
import secrets
import string
import time
from binascii import b2a_hex

import requests
from Crypto.Cipher import AES, DES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

# ---------------- 常量 ----------------
BASE_URL = "https://appdmkj.5idream.net/v2/"
CURRENT_APP_VERSION = "4.9.8"

DES_KEY = b"51434574"

# RSA-1024 业务公钥模数 n（e=65537, PKCS1_v1_5）—— 来自逆向分析，已验证仍有效
RSA_N = int(
    "9eed813259ad13f963176bcab53d04a8d8dd79e85cfa73588dd162cc9b9747d4"
    "a05d1a8fd11ce7cb38186729bfdd82f6b24141df2b3b021f0d0f9bca4d1120e1"
    "1554e5fd79dd8373b8025f8bf17a03e463dea876bc4862e8ad19cb38f864ea74e"
    "4aa53ef15735801dc8fcd3e98e75ef5133d50d8515c5b88d40d3a39b055f6ab", 16
)
RSA_E = 65537
RE_IV = b"1628092121312213"
JPUSH_ID = "1a0018970bff627d740"


def rand_session_key(n: int = 16) -> bytes:
    """16 位 [A-Za-z0-9] 随机会话密钥（与官方 App 一致）。

    关键：服务端 RSA 解密后会经 UTF-8 字符串规范化再作为 AES key。
    若用 os.urandom 任意字节 -> UTF-8 解码失真 -> AES key 不一致 -> 解密失败(11002)。
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n)).encode("ascii")


def _b64_spaces(b64: str, n: int = 60) -> str:
    return " ".join(b64[i:i + n] for i in range(0, len(b64), n))


def sign_token(params: dict) -> str:
    s = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha512(s.encode("utf-8")).hexdigest()
    step1 = h[1::2]
    step2 = step1[0::2]
    return hashlib.md5(step2.encode("utf-8")).hexdigest().upper()


def build_d(params: dict) -> str:
    p = dict(params)
    p["timestamp"] = str(int(time.time() * 1000))
    p["signToken"] = sign_token(p)
    plaintext = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    session_key = rand_session_key(16)

    aes = AES.new(session_key, AES.MODE_CBC, RE_IV)
    ct = aes.encrypt(pad(plaintext, AES.block_size))
    seg0 = _b64_spaces(base64.b64encode(ct).decode("ascii"))

    rsa = PKCS1_v1_5.new(RSA.construct((RSA_N, RSA_E)))
    seg1 = base64.b64encode(rsa.encrypt(session_key)).decode("ascii")

    middle = seg0 + "==" + seg1
    return _b64_spaces(base64.b64encode(middle.encode("utf-8")).decode("ascii"))


def des_encrypt_password(pwd: str) -> str:
    cipher = DES.new(DES_KEY, DES.MODE_ECB)
    ct = cipher.encrypt(pad(pwd.encode("utf-8"), DES.block_size))
    return b2a_hex(ct).upper().decode("ascii")


class DmkjClient:
    """精简版 v6 客户端：登录 + 拉活动列表。"""

    def __init__(self, version: str = CURRENT_APP_VERSION, timeout: int = 15):
        self.version = version
        self.timeout = timeout
        self.session = requests.Session()
        self.ua = json.dumps({
            "channelName": "dmkj_Android", "countryCode": "CN",
            "device": "Xiaomi Redmi Note 5", "hardware": "qcom",
            "jPushId": JPUSH_ID, "system": "android",
            "version": self.version,
        }, ensure_ascii=False)

    def _headers(self):
        return {
            "standardUA": self.ua,
            "User-Agent": "okhttp/3.11.0",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def login(self, phone: str, pwd: str):
        data = {"account": phone, "pwd": des_encrypt_password(pwd), "version": self.version}
        r = self.session.post(BASE_URL + "login/phone", headers=self._headers(),
                              data=data, timeout=self.timeout)
        j = r.json()
        if j.get("code") != "100":
            msg = j.get("msg") or (j.get("actionSheet") or {}).get("content") or "未知错误"
            raise RuntimeError(f"登录失败: {msg}")
        d = j["data"]
        return {"uid": d["uid"], "token": d["token"],
                "name": d.get("name"), "school": d.get("schoolName")}

    def _post(self, endpoint: str, biz: dict, ctx: dict):
        params = {
            "uid": ctx["uid"], "token": ctx["token"],
            "version": self.version, "jPushId": JPUSH_ID,
        }
        params.update(biz)
        d = build_d(params)
        r = self.session.post(BASE_URL + endpoint, headers=self._headers(),
                              data={"d": d}, timeout=self.timeout)
        return r.json()

    def list_activities(self, ctx: dict, search: dict = None):
        search = search or {}
        biz = {
            "joinStartTime": "", "startTime": "", "endTime": "",
            "joinFlag": search.get("join_flag", ""),
            "collegeFlag": search.get("college_flag", ""),
            "catalogId": search.get("catalog_id", ""),
            "joinEndTime": "", "specialFlag": "",
            "status": search.get("status", ""),
            "keyword": search.get("keyword", ""),
            "sort": "", "page": str(search.get("page", 1)),
            "catalogId2": search.get("catalog_id2", ""),
            "level": search.get("level", ""),
        }
        j = self._post("activity/activities", biz, ctx)
        out = []
        for it in (j.get("data") or {}).get("list") or []:
            out.append({
                "activityId": it.get("activityId"),
                "name": it.get("name"),
                "statusText": it.get("statusText"),
                "activitytime": it.get("activitytime"),
                "credit": it.get("maxScore"),
            })
        return out

    def get_detail(self, ctx: dict, activity_id):
        biz = {"activityId": activity_id}
        j = self._post("activity/detail", biz, ctx)
        if j.get("code") != "100":
            return None
        return j.get("data") or {}

    @staticmethod
    def fetch(cfg: dict):
        """便捷入口：从配置读账号，登录并拉列表。"""
        acc = (cfg or {}).get("account", {}) or {}
        search = (cfg or {}).get("search", {}) or {}
        version = (cfg or {}).get("version", CURRENT_APP_VERSION)
        client = DmkjClient(version=version)
        ctx = client.login(acc.get("phone"), acc.get("password"))
        return client.list_activities(ctx, search)
