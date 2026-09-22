from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("bili_login")

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    qrcode = None
    QRCODE_AVAILABLE = False

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False


QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_LOGIN_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": "https://www.bilibili.com/",
}
_REQUIRED_CREDENTIAL_KEYS = ("sessdata", "bili_jct", "dedeuserid")
_CROSS_DOMAIN_HOSTS = {"passport.biligame.com", "passport.bilibili.com"}


class BiliLoginError(Exception):
    """Bilibili 扫码登录失败。"""


@dataclass(frozen=True)
class QrCodeInfo:
    url: str
    qrcode_key: str
    image_path: str


@dataclass(frozen=True)
class LoginPollResult:
    login_state_code: int
    message: str
    credential: Optional[dict[str, Any]] = None

    @property
    def is_done(self) -> bool:
        return self.login_state_code == 0 and self.credential is not None

    @property
    def is_timeout(self) -> bool:
        return self.login_state_code == 86038


def is_cross_domain_login_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in _CROSS_DOMAIN_HOSTS
        and (
            parsed.path.endswith("/crossDomain")
            or bool(parse_qs(parsed.query).get("ticket"))
        )
    )


def _first_query_value(query: dict[str, List[str]], key: str) -> str:
    values = query.get(key)
    if not values:
        return ""
    return values[0]


def _get_cookie(cookies: dict[str, str], *keys: str) -> str:
    for k in keys:
        if k in cookies and cookies[k]:
            return str(cookies[k])
    lower_map = {k.lower(): v for k, v in cookies.items() if v}
    for k in keys:
        if k.lower() in lower_map:
            return str(lower_map[k.lower()])
    return ""


def build_credential(cookies: dict[str, str], refresh_token: str = "") -> dict[str, Any]:
    return {
        "sessdata": _get_cookie(cookies, "SESSDATA", "sessdata"),
        "bili_jct": _get_cookie(cookies, "bili_jct", "biliCSRF", "csrf"),
        "buvid3": _get_cookie(cookies, "buvid3") or None,
        "buvid4": _get_cookie(cookies, "buvid4") or None,
        "dedeuserid": _get_cookie(cookies, "DedeUserID", "dedeuserid"),
        "ac_time_value": refresh_token,
    }


def extract_credential_from_url(url: str, refresh_token: str = "") -> dict[str, Any]:
    query = parse_qs(urlparse(url).query)
    cookies = {
        "SESSDATA": _first_query_value(query, "SESSDATA"),
        "bili_jct": _first_query_value(query, "bili_jct"),
        "buvid3": _first_query_value(query, "buvid3"),
        "buvid4": _first_query_value(query, "buvid4"),
        "DedeUserID": _first_query_value(query, "DedeUserID"),
    }
    return build_credential(cookies, refresh_token)


def parse_set_cookie_headers(set_cookie_headers: Iterable[str]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in set_cookie_headers:
        try:
            parsed = SimpleCookie()
            parsed.load(header)
            cookies.update({key: morsel.value for key, morsel in parsed.items()})
        except Exception:
            continue
    return cookies


def credential_has_required_cookies(credential: Optional[dict[str, Any]]) -> bool:
    if not credential:
        return False
    return all(str(credential.get(key) or "") for key in _REQUIRED_CREDENTIAL_KEYS)


def _create_qrcode_image(url: str) -> str:
    unique_suffix = os.urandom(4).hex()
    path = os.path.join(tempfile.gettempdir(), f"rocom_bili_qrcode_{unique_suffix}.png")
    if not QRCODE_AVAILABLE or qrcode is None:
        raise BiliLoginError("缺少 qrcode 依赖，无法生成登录二维码。")
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#fb7299", back_color="white")
    image.save(path)
    return path


class BiliQrLoginClient:
    """Bilibili 扫码登录客户端（参考 astrbot_plugin_bilibili 实现，支持 crossDomain Set-Cookie 提取）。"""

    def __init__(self, proxy: str | None = None, timeout_secs: int = 10) -> None:
        self.proxy = (proxy or "").strip() or None
        self.timeout_secs = timeout_secs
        self.qrcode_key = ""

    async def generate_qrcode(self) -> QrCodeInfo:
        """请求 B 站接口生成二维码和 qrcode_key。"""
        payload = await self._http_get_json(QR_GENERATE_URL)
        if payload.get("code") != 0:
            raise BiliLoginError(payload.get("message") or "生成二维码失败")

        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        qrcode_key = str(data.get("qrcode_key") or "")
        if not url or not qrcode_key:
            raise BiliLoginError("生成二维码失败：响应缺少 url 或 qrcode_key。")

        self.qrcode_key = qrcode_key
        return QrCodeInfo(
            url=url,
            qrcode_key=qrcode_key,
            image_path=_create_qrcode_image(url),
        )

    async def poll(self) -> LoginPollResult:
        """轮询二维码扫描状态，并在扫码确认后解析完整凭据。"""
        if not self.qrcode_key:
            raise BiliLoginError("尚未生成二维码。")

        payload = await self._http_get_json(
            QR_POLL_URL, params={"qrcode_key": self.qrcode_key}
        )
        if payload.get("code") != 0:
            raise BiliLoginError(payload.get("message") or "轮询登录状态失败")

        data = payload.get("data") or {}
        poll_code = int(data.get("code", -1))
        message = str(data.get("message") or "")
        if poll_code != 0:
            return LoginPollResult(login_state_code=poll_code, message=message)

        credential = await self._extract_success_credential(data)
        if not credential_has_required_cookies(credential):
            raise BiliLoginError(
                "扫码已确认，但未能获取完整凭据（缺少 SESSDATA/bili_jct/DedeUserID）。"
            )
        return LoginPollResult(
            login_state_code=0, message=message, credential=credential
        )

    async def _http_get_json(self, url: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if CURL_CFFI_AVAILABLE and curl_requests is not None:
            async with curl_requests.AsyncSession(
                headers=_LOGIN_HEADERS,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                impersonate="chrome",
                timeout=self.timeout_secs,
            ) as session:
                response = await session.get(url, params=params)
                return response.json()
        elif AIOHTTP_AVAILABLE and aiohttp is not None:
            async with aiohttp.ClientSession(headers=_LOGIN_HEADERS) as session:
                async with session.get(
                    url, params=params, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=self.timeout_secs)
                ) as resp:
                    return await resp.json()
        else:
            import httpx
            async with httpx.AsyncClient(headers=_LOGIN_HEADERS, proxy=self.proxy, timeout=self.timeout_secs) as client:
                resp = await client.get(url, params=params)
                return resp.json()

    async def _extract_success_credential(self, data: dict[str, Any]) -> dict[str, Any]:
        login_url = str(data.get("url") or "")
        refresh_token = str(data.get("refresh_token") or "")

        if is_cross_domain_login_url(login_url):
            return await self._credential_from_cross_domain_url(login_url, refresh_token)

        credential = extract_credential_from_url(login_url, refresh_token)
        if credential_has_required_cookies(credential):
            return credential

        # 若 URL query 中未包含完整凭据（如 ticket 形式），回退请求 URL 换取 Set-Cookie
        return await self._credential_from_cross_domain_url(login_url, refresh_token)

    async def _credential_from_cross_domain_url(
        self, url: str, refresh_token: str
    ) -> dict[str, Any]:
        """访问单点登录 crossDomain 跳转链接，解析 Set-Cookie 中的真实凭据。"""
        cookie_headers: List[str] = []
        cookies: dict[str, str] = {}
        try:
            if CURL_CFFI_AVAILABLE and curl_requests is not None:
                async with curl_requests.AsyncSession(
                    headers=_LOGIN_HEADERS,
                    proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                    impersonate="chrome",
                    timeout=self.timeout_secs,
                ) as session:
                    response = await session.get(url, allow_redirects=True)
                    for r in [response] + list(getattr(response, "history", []) or []):
                        h = getattr(r, "headers", None)
                        if h and hasattr(h, "get_list"):
                            cookie_headers.extend(h.get_list("Set-Cookie"))
                    cookies = parse_set_cookie_headers(cookie_headers)
                    try:
                        cookies.update(session.cookies.get_dict())
                    except Exception:
                        pass
            elif AIOHTTP_AVAILABLE and aiohttp is not None:
                jar = aiohttp.CookieJar(unsafe=True)
                async with aiohttp.ClientSession(headers=_LOGIN_HEADERS, cookie_jar=jar) as session:
                    async with session.get(
                        url, allow_redirects=True, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=self.timeout_secs)
                    ) as resp:
                        for r in [resp] + list(getattr(resp, "history", []) or []):
                            h = getattr(r, "headers", None)
                            if h and hasattr(h, "getall"):
                                cookie_headers.extend(h.getall("Set-Cookie", []))
                        cookies = parse_set_cookie_headers(cookie_headers)
                        try:
                            cookies.update({k: v.value for k, v in session.cookie_jar.filter_cookies(url).items()})
                        except Exception:
                            pass
            else:
                import httpx
                async with httpx.AsyncClient(
                    headers=_LOGIN_HEADERS, proxy=self.proxy, follow_redirects=True, timeout=self.timeout_secs
                ) as client:
                    resp = await client.get(url)
                    for r in [resp] + list(getattr(resp, "history", []) or []):
                        h = getattr(r, "headers", None)
                        if h and hasattr(h, "get_list"):
                            cookie_headers.extend(h.get_list("set-cookie"))
                    cookies = parse_set_cookie_headers(cookie_headers)
                    try:
                        cookies.update(dict(client.cookies))
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            raise BiliLoginError(f"请求跨域授权链接换取凭据失败: {exc}") from exc

        return build_credential(cookies, refresh_token)
