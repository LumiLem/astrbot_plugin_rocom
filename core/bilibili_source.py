"""
B 站动态数据源

封装 bilibili-api-python 的取数与解析，供洛克公告推送作为可选的 B 站动态数据源。
依赖为懒加载：安装失败时插件仍可正常加载，仅 B 站源不可用。
"""

import re
from typing import Any, Dict, List, Optional

from astrbot.api import logger

try:  # pragma: no cover - 依赖可用性取决于运行环境
    from bilibili_api import (
        Credential,
        opus as bili_opus,
        request_settings,
        user as bili_user,
        video as bili_video,
    )

    BILIBILI_AVAILABLE = True
    BILIBILI_IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    BILIBILI_AVAILABLE = False
    BILIBILI_IMPORT_ERROR = str(_exc)
    bili_opus = None
    bili_video = None

from .bili_login import BiliLoginError, BiliQrLoginClient


_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_ANNOUNCEMENT_LINK_HINTS = (
    "rocom",
    "wegame",
    "/announcement/",
    "thread_id=",
    "bilibili.com/read/",
    "b23.tv/",
)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _absolute_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("http://"):
        return "https://" + text[len("http://") :]
    return text


def extract_links(text: Any) -> List[str]:
    """从文本中提取官方公告相关链接（用于同内容精确判定）。"""
    if not text:
        return []
    found = _URL_RE.findall(str(text))
    result: List[str] = []
    for url in found:
        if any(hint in url for hint in _ANNOUNCEMENT_LINK_HINTS):
            result.append(url)
    return result


def credential_to_dict(credential: Any) -> Dict[str, str]:
    """把 bilibili-api Credential 转成可持久化的字典（不含 proxy）。"""
    if credential is None:
        return {}
    keys = ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value")
    return {key: str(getattr(credential, key, "") or "") for key in keys}


class BilibiliDynamicSource:
    """B 站动态取数客户端（匿名 / 配置 SESSDATA / 扫码登录凭据）。"""

    def __init__(
        self,
        sessdata: str = "",
        proxy: str = "",
        credential_dict: Optional[Dict[str, Any]] = None,
    ):
        self.sessdata = str(sessdata or "").strip()
        self.proxy = str(proxy or "").strip()
        self.credential = None
        self.credential_dict: Dict[str, str] = {}
        if BILIBILI_AVAILABLE:
            saved = credential_dict if isinstance(credential_dict, dict) else {}
            if str(saved.get("sessdata") or "").strip():
                self.set_credential_dict(saved)
            elif self.sessdata:
                try:
                    self.credential = Credential(sessdata=self.sessdata, proxy=self.proxy or None)
                    self.credential_dict = credential_to_dict(self.credential)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[Rocom] B 站 SESSDATA 构建失败，将匿名请求: {exc}")
                    self.credential = None
            self._apply_proxy()

    @property
    def is_available(self) -> bool:
        return BILIBILI_AVAILABLE

    @property
    def is_logged_in(self) -> bool:
        data = self.credential_dict or {}
        return all(str(data.get(key) or "") for key in ("sessdata", "bili_jct", "dedeuserid"))

    def set_credential_dict(self, credential_dict: Optional[Dict[str, Any]]) -> None:
        """用持久化/扫码得到的凭据重建 Credential。"""
        data = credential_dict if isinstance(credential_dict, dict) else {}
        if not str(data.get("sessdata") or "").strip():
            self.credential = None
            self.credential_dict = {}
            return
        try:
            self.credential = Credential(
                sessdata=str(data.get("sessdata") or "") or None,
                bili_jct=str(data.get("bili_jct") or "") or None,
                buvid3=str(data.get("buvid3") or "") or None,
                buvid4=str(data.get("buvid4") or "") or None,
                dedeuserid=str(data.get("dedeuserid") or "") or None,
                ac_time_value=str(data.get("ac_time_value") or "") or None,
                proxy=self.proxy or None,
            )
            self.credential_dict = credential_to_dict(self.credential)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] B 站登录凭据构建失败，将匿名请求: {exc}")
            self.credential = None
            self.credential_dict = {}

    def clear_credential(self) -> None:
        self.credential = None
        self.credential_dict = {}

    async def refresh_credential(self) -> Optional[Dict[str, str]]:
        """按服务端指示刷新登录态，成功返回新凭据字典。"""
        if not BILIBILI_AVAILABLE or self.credential is None:
            return None
        if not str(getattr(self.credential, "ac_time_value", "") or ""):
            return None
        old_dict = dict(self.credential_dict)
        try:
            self._apply_proxy()
            if await self.credential.check_refresh():
                await self.credential.refresh()
                self.credential_dict = credential_to_dict(self.credential)
                return dict(self.credential_dict)
        except Exception as exc:  # noqa: BLE001
            try:
                self.set_credential_dict(old_dict)
            except Exception:
                pass
            logger.warning(f"[Rocom] B 站登录态刷新失败: {exc}")
        return None

    async def verify_credential(self) -> tuple[bool, str, int]:
        """校验当前凭据是否在服务端真正有效，成功返回 (True, uname, mid)，失败返回 (False, '', 0)。"""
        if not BILIBILI_AVAILABLE or self.credential is None:
            return False, "", 0
        self._apply_proxy()
        try:
            from bilibili_api.utils.network import Api

            resp = await Api(
                url="https://api.bilibili.com/x/web-interface/nav",
                method="GET",
                credential=self.credential,
                no_csrf=True,
                ignore_code=True,
            ).result
            if isinstance(resp, dict) and resp.get("isLogin"):
                uname = str(resp.get("uname") or "")
                mid = int(resp.get("mid") or 0)
                return True, uname, mid
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] 校验 B 站登录态异常: {exc}")
        return False, "", 0

    async def logout(self) -> bool:
        """调用 B 站服务端登出接口注销凭据（参考 astrbot_plugin_bilibili）。"""
        if not BILIBILI_AVAILABLE or self.credential is None:
            return False
        bili_jct = str(getattr(self.credential, "bili_jct", "") or "")
        if not bili_jct:
            return False
        self._apply_proxy()
        try:
            from bilibili_api.utils.network import Api

            resp = await Api(
                url="https://api.bilibili.com/login/exit/v2",
                method="POST",
                no_csrf=True,
                data={"biliCSRF": bili_jct},
                credential=self.credential,
                ignore_code=True,
            ).result
            return isinstance(resp, dict) and "redirectUrl" in resp
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Rocom] B 站服务端登出接口调用异常: {exc}")
            return False

    @staticmethod
    def create_qr_login(proxy: str = "") -> Optional[BiliQrLoginClient]:
        """创建扫码登录对象；环境不支持时返回 None。"""
        try:
            return BiliQrLoginClient(proxy=proxy)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] 初始化 B 站扫码登录失败: {exc}")
            return None

    def _apply_proxy(self) -> None:
        if not BILIBILI_AVAILABLE:
            return
        try:
            request_settings.set_proxy(self.proxy)
        except Exception as exc:  # noqa: BLE001
            if self.proxy:
                logger.warning(f"[Rocom] 设置 B 站代理失败: {exc}")

    async def get_latest_dynamics(self, uid: int) -> Optional[Dict[str, Any]]:
        """获取指定 UID 的最新一页动态，失败返回 None。"""
        if not BILIBILI_AVAILABLE:
            return None
        try:
            self._apply_proxy()
            client = bili_user.User(uid, credential=self.credential)
            data = await client.get_dynamics_new()
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] 获取 B 站动态失败 (UID={uid}): {exc}")
            return None

    @staticmethod
    def parse_items(data: Optional[Dict[str, Any]], uid: int = 0) -> List[Dict[str, Any]]:
        """把接口返回的 items 解析为统一结构，跳过无法解析的条目。"""
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        parsed: List[Dict[str, Any]] = []
        for item in items:
            result = BilibiliDynamicSource.parse_item(item, uid)
            if result:
                parsed.append(result)
        return parsed

    @staticmethod
    def parse_item(
        item: Optional[Dict[str, Any]], uid: int = 0, _depth: int = 0
    ) -> Optional[Dict[str, Any]]:
        """解析单条动态为统一结构；无法解析返回 None。"""
        if not isinstance(item, dict):
            return None
        dyn_id = str(item.get("id_str") or item.get("id") or "").strip()
        if not dyn_id:
            return None
        if item.get("type") == "DYNAMIC_TYPE_LIVE_RCMD":
            return None

        modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
        dynamic = (
            modules.get("module_dynamic")
            if isinstance(modules.get("module_dynamic"), dict)
            else {}
        )
        author = (
            modules.get("module_author")
            if isinstance(modules.get("module_author"), dict)
            else {}
        )
        tag = modules.get("module_tag") if isinstance(modules.get("module_tag"), dict) else {}
        major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
        desc = dynamic.get("desc") if isinstance(dynamic.get("desc"), dict) else {}

        # 充电专属 / 仅粉丝可见等不可解析内容
        if major.get("type") == "MAJOR_TYPE_BLOCKED":
            return None

        pub_ts = _as_int(author.get("pub_ts"))
        dyn_type = str(item.get("type") or "")
        title = ""
        has_title = False
        content_cover = ""
        text = ""
        images: List[str] = []
        images_meta: List[Dict[str, Any]] = []
        video: Optional[Dict[str, str]] = None
        url = ""

        opus = major.get("opus") if isinstance(major.get("opus"), dict) else {}
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
        article = major.get("article") if isinstance(major.get("article"), dict) else {}

        if archive:
            title = str(archive.get("title") or "").strip()
            has_title = bool(title)
            text = str(desc.get("text") or archive.get("desc") or "").strip()
            cover = _absolute_url(archive.get("cover"))
            if cover:
                images.append(cover)
                images_meta.append({"url": cover, "width": 0, "height": 0})
            bvid = str(archive.get("bvid") or "").strip()
            jump = _absolute_url(archive.get("jump_url"))
            if jump:
                url = jump
            elif bvid:
                url = f"https://www.bilibili.com/video/{bvid}"
            if cover:
                video = {"cover": cover, "url": url, "bvid": bvid}
        elif opus:
            title = str(opus.get("title") or "").strip()
            has_title = bool(title)
            summary = opus.get("summary") if isinstance(opus.get("summary"), dict) else {}
            text = str(summary.get("text") or "").strip()
            pics = opus.get("pics") if isinstance(opus.get("pics"), list) else []
            for pic in pics:
                if not isinstance(pic, dict):
                    continue
                pic_url = _absolute_url(pic.get("url"))
                if pic_url:
                    images.append(pic_url)
                    images_meta.append(
                        {
                            "url": pic_url,
                            "width": _as_int(pic.get("width")),
                            "height": _as_int(pic.get("height")),
                        }
                    )
            url = _absolute_url(opus.get("jump_url"))
            # 专栏/文章的首图是封面，不是正文图（图文动态的 pics 才是正文图）
            if dyn_type == "DYNAMIC_TYPE_ARTICLE" and images:
                content_cover = images[0]
                images = images[1:]
                images_meta = images_meta[1:]
        elif draw:
            draw_items = draw.get("items") if isinstance(draw.get("items"), list) else []
            for entry in draw_items:
                if not isinstance(entry, dict):
                    continue
                pic_url = _absolute_url(entry.get("src"))
                if pic_url:
                    images.append(pic_url)
                    images_meta.append(
                        {
                            "url": pic_url,
                            "width": _as_int(entry.get("width")),
                            "height": _as_int(entry.get("height")),
                        }
                    )
        elif article:
            title = str(article.get("title") or "").strip()
            has_title = bool(title)
            text = str(desc.get("text") or article.get("desc") or "").strip()
            covers = article.get("covers") if isinstance(article.get("covers"), list) else []
            for cover in covers:
                cover_url = _absolute_url(cover)
                if cover_url:
                    images.append(cover_url)
                    images_meta.append({"url": cover_url, "width": 0, "height": 0})
            cover = _absolute_url(article.get("cover"))
            if cover and cover not in images:
                images.insert(0, cover)
                images_meta.insert(0, {"url": cover, "width": 0, "height": 0})
            if images:
                content_cover = images[0]
                images = images[1:]
                images_meta = images_meta[1:]

        if not text:
            text = str(desc.get("text") or "").strip()

        # 转发动态：合并外层评论与内层原文（只解析一层，避免递归）
        orig = item.get("orig")
        if _depth == 0 and isinstance(orig, dict):
            inner = BilibiliDynamicSource.parse_item(orig, uid, _depth=1)
            if inner:
                if not title:
                    title = inner.get("title") or ""
                    if title:
                        has_title = bool(inner.get("has_title"))
                inner_text = inner.get("text") or ""
                if inner_text:
                    text = (text + "\n" + inner_text).strip()
                if not images:
                    images = list(inner.get("images") or [])
                    images_meta = list(inner.get("images_meta") or [])
                if not video:
                    video = inner.get("video")
                if not content_cover:
                    content_cover = str(inner.get("cover") or "")
        if not url:
            jump = _absolute_url((opus or {}).get("jump_url"))
            if jump:
                url = jump
            else:
                url = f"https://t.bilibili.com/{dyn_id}"
        if not title and text:
            first_line = text.strip().splitlines()[0].strip()
            if first_line:
                title = first_line[:80]

        return {
            "source": "bilibili",
            "id": dyn_id,
            "type": dyn_type,
            "ts": pub_ts,
            "pinned": str(tag.get("text") or "").strip() == "置顶",
            "title": title,
            "has_title": has_title,
            "cover": content_cover,
            "text": text,
            "has_more": bool((opus.get("summary") or {}).get("has_more")) if opus else False,
            "links": extract_links(f"{text}\n{url}"),
            "author": str(author.get("name") or "洛克王国世界").strip(),
            "images": images,
            "images_meta": images_meta,
            "video": video,
            "url": url,
        }

    async def get_opus_detail(self, opus_id: Any) -> Optional[Dict[str, Any]]:
        """获取图文/专栏完整详情（feed 里的 opus.summary 在 has_more 时是截断的）。"""
        if not BILIBILI_AVAILABLE or bili_opus is None:
            return None
        try:
            self._apply_proxy()
            info = await bili_opus.Opus(int(opus_id), credential=self.credential).get_info()
            return info if isinstance(info, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] 获取 B 站图文详情失败 (opus={opus_id}): {exc}")
            return None

    async def get_video_stream_urls(self, bvid: Any) -> tuple[str, Optional[str]]:
        """获取 B 站视频流地址：(video_url, audio_url)。

        若获取到单文件 MP4（如 HTML5 流），返回 (video_url, None)；
        若 HTML5 流未就绪（如新投稿刚发布），回退提取 DASH 音视频分离流，返回 (video_url, audio_url)；
        获取失败返回 ("", None)。
        """
        bvid = str(bvid or "").strip()
        if not BILIBILI_AVAILABLE or bili_video is None or not bvid:
            return "", None
        self._apply_proxy()

        # 1. 优先尝试移动端单文件 HTML5 MP4 流（单流无需合流）
        try:
            info = await bili_video.Video(bvid=bvid, credential=self.credential).get_download_url(
                0, html5=True
            )
            for entry in (info or {}).get("durl") or []:
                url = str((entry or {}).get("url") or "").strip()
                if url:
                    logger.debug(f"[Rocom] 命中 B 站 HTML5 单文件视频流 (bvid={bvid})")
                    return url, None
            logger.debug(f"[Rocom] B 站未返回 HTML5 单文件流，准备尝试 DASH 流 (bvid={bvid})")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Rocom] 获取 B 站 HTML5 视频流异常，准备尝试 DASH 流 (bvid={bvid}): {exc}")

        # 2. 回退尝试 DASH 流（音视频分离流，支持刚发布视频）
        try:
            data = await bili_video.Video(bvid=bvid, credential=self.credential).get_download_url(
                0, html5=False
            )
            detecter = getattr(bili_video, "VideoDownloadURLDataDetecter", None)
            if detecter and isinstance(data, dict):
                codec_pref = []
                for c in ("AVC", "HEV", "AV1"):
                    attr = getattr(bili_video.VideoCodecs, c, None)
                    if attr is not None:
                        codec_pref.append(attr)
                streams = detecter(data).detect_best_streams(
                    codecs=codec_pref or None,
                    no_dolby_video=True,
                    no_hdr=True,
                    no_dolby_audio=True,
                    no_hires=True,
                )
                v_stream = streams[0] if len(streams) > 0 else None
                a_stream = streams[1] if len(streams) > 1 else None
                v_url = str(getattr(v_stream, "url", "") or "").strip()
                a_url = str(getattr(a_stream, "url", "") or "").strip()
                if v_url:
                    logger.info(
                        f"[Rocom] 命中 B 站 DASH 音视频分离流 (bvid={bvid}, has_audio={bool(a_url)})"
                    )
                    return v_url, (a_url or None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Rocom] 获取 B 站 DASH 视频流失败 (bvid={bvid}): {exc}")

        logger.warning(f"[Rocom] 获取 B 站视频流失败 (bvid={bvid}): 无可用媒体流")
        return "", None

    async def get_video_download_url(self, bvid: Any) -> str:
        """获取 B 站视频的可直接下载流地址（兼容旧接口）。"""
        v_url, _ = await self.get_video_stream_urls(bvid)
        return v_url

    @staticmethod
    def parse_opus_detail(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """从 opus 详情中提取完整标题、正文与图片。"""
        if not isinstance(info, dict):
            return {}
        item = info.get("item") if isinstance(info.get("item"), dict) else {}
        modules = item.get("modules") if isinstance(item.get("modules"), list) else []

        title = ""
        images: List[str] = []
        lines: List[str] = []

        for module in modules:
            if not isinstance(module, dict):
                continue
            module_title = module.get("module_title")
            if isinstance(module_title, dict) and module_title.get("text"):
                title = str(module_title["text"]).strip() or title

            module_top = module.get("module_top")
            if isinstance(module_top, dict):
                display = module_top.get("display")
                album = display.get("album") if isinstance(display, dict) else None
                if isinstance(album, dict):
                    for pic in album.get("pics") or []:
                        pic_url = _absolute_url((pic or {}).get("url"))
                        if pic_url:
                            images.append(pic_url)

            content = module.get("module_content")
            if not isinstance(content, dict):
                continue
            for para in content.get("paragraphs") or []:
                if not isinstance(para, dict):
                    continue
                text_bits: List[str] = []
                for key in ("text", "heading", "blockquote", "code"):
                    block = para.get(key)
                    if not isinstance(block, dict):
                        continue
                    for node in block.get("nodes") or []:
                        if not isinstance(node, dict):
                            continue
                        word = node.get("word")
                        if isinstance(word, dict) and word.get("words"):
                            text_bits.append(str(word["words"]))
                        link_card = node.get("link_card")
                        if isinstance(link_card, dict) and link_card.get("jump_url"):
                            text_bits.append(str(link_card["jump_url"]))
                para_list = para.get("list")
                if isinstance(para_list, dict):
                    for list_item in para_list.get("items") or []:
                        if not isinstance(list_item, dict):
                            continue
                        for node in list_item.get("nodes") or []:
                            if isinstance(node, dict):
                                word = node.get("word")
                                if isinstance(word, dict) and word.get("words"):
                                    text_bits.append(str(word["words"]))
                para_pic = para.get("pic")
                if isinstance(para_pic, dict):
                    for pic in para_pic.get("pics") or []:
                        pic_url = _absolute_url((pic or {}).get("url"))
                        if pic_url:
                            images.append(pic_url)
                joined = "".join(text_bits).strip()
                lines.append(joined)

        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return {
            "title": title,
            "text": text,
            "images": list(dict.fromkeys(images)),
        }
