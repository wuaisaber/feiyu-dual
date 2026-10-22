"""B 站通道：把"一个账号"整个交给 LLM。

和 QQ 两侧的根本区别
------------------
QQ 那边是"别人说话 → 我们回"；B 站这边是"我们自己出门逛 → 看到东西说两句"。
所以这里没有消息推送，只有一轮一轮的主动行为。

身份 = Cookie（SESSDATA / bili_jct / buvid3），行为 = 调 B 站 web 端接口。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import httpx

from .. import config, memory

log = logging.getLogger("feiyu.bili")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
VC = "https://api.vc.bilibili.com"

# 风控信号：出现任意一个就立刻停机，绝不原地重试
RISK_CODES = {-352, -412, -509, -799}

_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class RiskControlError(RuntimeError):
    """B 站风控：账号需要冷静，继续请求只会更糟。"""


class BiliClient:
    """薄封装：只管请求、签名、Cookie，不含任何业务判断。"""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_at = 0.0
        self._dev_id = uuid.uuid4().hex[:16]
        self.load_cookies()

    # ------------------------------------------------------------ Cookie
    def load_cookies(self) -> None:
        data = config.load_cookie()
        for k, v in (data.get("cookies") or {}).items():
            self.client.cookies.set(k, v, domain=".bilibili.com")
        if data.get("dev_id"):
            self._dev_id = data["dev_id"]

    def has_login(self) -> bool:
        return bool(self.client.cookies.get("SESSDATA"))

    @property
    def csrf(self) -> str:
        return self.client.cookies.get("bili_jct") or ""

    def save_cookies(self, nickname: str = "", mid: str = "") -> None:
        jar = {c.name: c.value for c in self.client.cookies.jar}
        config.save_cookie(
            {
                "cookies": jar,
                "dev_id": self._dev_id,
                "nickname": nickname,
                "mid": mid,
                "saved_at": time.time(),
            }
        )

    # ------------------------------------------------------------ 基础请求
    def _req(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 20,
    ) -> Dict[str, Any]:
        try:
            r = self.client.request(method, url, params=params, data=data, timeout=timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"网络错误：{e}") from e
        if r.status_code == 412:
            raise RiskControlError("HTTP 412：账号被限流")
        if r.status_code >= 500:
            raise RuntimeError(f"B 站服务端 {r.status_code}")
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"返回不是 JSON（HTTP {r.status_code}）")
        code = int(j.get("code") or 0)
        if code in RISK_CODES:
            raise RiskControlError(f"接口返回 {code}：触发风控")
        return j

    # ------------------------------------------------------------ wbi 签名
    def _get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys and time.time() - self._wbi_at < 3600:
            return self._wbi_keys
        j = self._req("GET", f"{API}/x/web-interface/nav")
        wbi = ((j.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise RuntimeError("拿不到 wbi 密钥（nav 接口异常）")
        self._wbi_keys = (img, sub)
        self._wbi_at = time.time()
        return self._wbi_keys

    def sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        img, sub = self._get_wbi_keys()
        raw = img + sub
        mixin = "".join(raw[i] for i in _MIXIN_TAB)[:32]
        p = {k: str(v) for k, v in params.items()}
        p["wts"] = str(int(time.time()))
        clean = {k: re.sub(r"[!'()*]", "", v) for k, v in p.items()}
        query = urllib.parse.urlencode(sorted(clean.items()))
        p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return p

    # ------------------------------------------------------------ 登录
    def qr_generate(self) -> Dict[str, Any]:
        j = self._req("GET", f"{PASSPORT}/x/passport-login/web/qrcode/generate")
        if j.get("code") != 0:
            raise RuntimeError(j.get("message") or "生成二维码失败")
        d = j["data"]
        return {"url": d.get("url"), "key": d.get("qrcode_key")}

    def qr_poll(self, key: str) -> Dict[str, Any]:
        """返回 status: waiting / scanned / expired / success。"""
        j = self._req(
            "GET", f"{PASSPORT}/x/passport-login/web/qrcode/poll", params={"qrcode_key": key}
        )
        d = j.get("data") or {}
        code = d.get("code")
        if code == 86101:
            return {"status": "waiting"}
        if code == 86090:
            return {"status": "scanned"}
        if code == 86038:
            return {"status": "expired"}
        if code != 0:
            return {"status": "waiting", "detail": str(d)}

        # 2025 年底起扫码是两步：poll 只给 ticket，还要再走一次 crossDomain 才下发 Cookie
        if not self.client.cookies.get("SESSDATA"):
            cross = d.get("url") or ""
            if cross:
                try:
                    self.client.get(cross, timeout=15)
                except httpx.HTTPError:
                    pass
        if not self.client.cookies.get("SESSDATA"):
            return {"status": "scanned", "detail": "已确认但没换到 Cookie，可能需要重扫"}

        who = self.me()
        self.save_cookies(who.get("uname", ""), str(who.get("mid", "")))
        return {"status": "success", "user": who}

    def me(self) -> Dict[str, Any]:
        j = self._req("GET", f"{API}/x/web-interface/nav")
        d = j.get("data") or {}
        return {"uname": d.get("uname", ""), "mid": d.get("mid", 0), "vip": bool(d.get("vipStatus"))}

    # ------------------------------------------------------------ 内容
    def feed(self, count: int = 10) -> List[Dict[str, Any]]:
        params = {
            "web_location": "1430650",
            "y_num": 5,
            "fresh_type": 4,
            "feed_version": "V8",
            "fresh_idx": 1,
            "fresh_idx_1h": 1,
            "fetch_row": 1,
            "brush": 0,
            "homepage_ver": 1,
            "ps": max(6, count),
        }
        signed = self.sign(params)
        j = self._req("GET", f"{API}/x/web-interface/wbi/index/top/feed/rcmd", params=signed)
        items = ((j.get("data") or {}).get("item")) or []
        out = []
        for it in items:
            if not isinstance(it, dict) or not it.get("bvid"):
                continue
            out.append(
                {
                    "bvid": it.get("bvid"),
                    "aid": it.get("id"),
                    "cid": it.get("cid"),
                    "title": it.get("title", ""),
                    "desc": (it.get("desc") or "")[:400],
                    "up": (it.get("owner") or {}).get("name", ""),
                    "tname": it.get("tname", ""),
                    "views": it.get("stat", {}).get("view") if isinstance(it.get("stat"), dict) else it.get("videos"),
                }
            )
        return out

    def video(self, bvid: str) -> Dict[str, Any]:
        j = self._req("GET", f"{API}/x/web-interface/view", params={"bvid": bvid})
        d = j.get("data") or {}
        return {
            "aid": d.get("aid"),
            "cid": d.get("cid"),
            "title": d.get("title", ""),
            "desc": (d.get("desc") or "")[:400],
            "up": (d.get("owner") or {}).get("name", ""),
            "tname": d.get("tname", ""),
            "views": (d.get("stat") or {}).get("view", 0),
            "reply": (d.get("stat") or {}).get("reply", 0),
        }

    def hot_comments(self, aid: int, limit: int = 5) -> List[str]:
        j = self._req(
            "GET",
            f"{API}/x/v2/reply",
            params={"type": 1, "oid": aid, "sort": 2, "ps": limit, "pn": 1},
        )
        reps = ((j.get("data") or {}).get("replies")) or []
        out = []
        for r in reps:
            msg = ((r.get("content") or {}).get("message") or "").strip()
            if msg:
                out.append(msg[:60])
        return out

    def subtitle(self, aid: int, cid: int, limit: int = 600) -> str:
        try:
            j = self._req("GET", f"{API}/x/player/v2", params={"aid": aid, "cid": cid})
            subs = (((j.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
            if not subs:
                return ""
            url = subs[0].get("subtitle_url") or ""
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                return ""
            r = self.client.get(url, timeout=15)
            body = (r.json() or {}).get("body") or []
            text = "".join(str(x.get("content", "")) for x in body)
            return text[:limit]
        except Exception:
            return ""

    # ------------------------------------------------------------ 写操作
    def comment_add(self, aid: int, text: str) -> Dict[str, Any]:
        if not self.csrf:
            raise RuntimeError("缺少 bili_jct，需要重新扫码登录")
        # 评论是写接口，必须带 wbi 签名（w_rid/wts）。
        # 只带 csrf 时接口会返回 code=0，但评论进审核、对外不可见。
        sign = self.sign({"oid": aid, "type": 1})
        data = {
            "oid": aid,
            "type": 1,
            "message": text,
            "plat": 1,
            "csrf": self.csrf,
        }
        j = self._req("POST", f"{API}/x/v2/reply/add", params=sign, data=data)
        if j.get("code") != 0:
            raise RuntimeError(f"发评论失败：{j.get('message')} ({j.get('code')})")
        return j.get("data") or {}

    def reply_add(self, aid: int, root: int, text: str, parent: int | None = None) -> Dict[str, Any]:
        if not self.csrf:
            raise RuntimeError("缺少 bili_jct，需要重新扫码登录")
        sign = self.sign({"oid": aid, "type": 1})
        data = {
            "oid": aid,
            "type": 1,
            "message": text,
            "root": root,
            "parent": parent or root,
            "plat": 1,
            "csrf": self.csrf,
        }
        j = self._req("POST", f"{API}/x/v2/reply/add", params=sign, data=data)
        if j.get("code") != 0:
            raise RuntimeError(f"回复失败：{j.get('message')} ({j.get('code')})")
        return j.get("data") or {}

    # ------------------------------------------------------------ 消息
    def msg_at(self) -> List[Dict[str, Any]]:
        j = self._req(
            "GET",
            f"{API}/x/msgfeed/at",
            params={"build": 0, "mobi_app": "web", "web_location": "333.1387"},
        )
        items = ((j.get("data") or {}).get("items")) or []
        out = []
        for it in items:
            item = it.get("item") or {}
            out.append(
                {
                    "id": str(it.get("id") or ""),
                    "user": (it.get("user") or {}).get("nickname", ""),
                    "mid": (it.get("user") or {}).get("mid"),
                    "title": item.get("title", ""),
                    "source": item.get("source_content", ""),
                    "uri": item.get("uri", ""),
                    "business": it.get("business_id"),
                }
            )
        return out

    def msg_reply(self) -> List[Dict[str, Any]]:
        j = self._req(
            "GET",
            f"{API}/x/msgfeed/reply",
            params={"build": 0, "mobi_app": "web", "web_location": "333.1387"},
        )
        items = ((j.get("data") or {}).get("items")) or []
        out = []
        for it in items:
            item = it.get("item") or {}
            out.append(
                {
                    "id": str(it.get("id") or ""),
                    "user": (it.get("user") or {}).get("nickname", ""),
                    "mid": (it.get("user") or {}).get("mid"),
                    "subject": item.get("subject", ""),
                    "source": ((item.get("source_content") or "")[:120]),
                    "target": ((item.get("target_content") or "")[:120]),
                    "root_id": item.get("root_id"),
                    "source_id": item.get("source_id"),
                    "target_id": item.get("target_id"),
                }
            )
        return out

    def dm_sessions(self) -> List[Dict[str, Any]]:
        j = self._req(
            "GET",
            f"{VC}/session_svr/v1/session_svr/get_sessions",
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "build": 0,
                "mobi_app": "web",
            },
        )
        data = j.get("data") or {}
        out = []
        for s in data.get("session_list") or []:
            out.append(
                {
                    "talker_id": s.get("talker_id"),
                    "unread": s.get("unread_count", 0),
                    "ts": s.get("session_ts", 0),
                }
            )
        return out

    def dm_history(self, uid: int, size: int = 5) -> List[Dict[str, str]]:
        j = self._req(
            "GET",
            f"{VC}/svr_sync/v1/svr_sync/fetch_session_msgs",
            params={"talker_id": uid, "session_type": 1, "size": size, "build": 0, "mobi_app": "web"},
        )
        msgs = ((j.get("data") or {}).get("messages")) or []
        out = []
        for m in reversed(msgs):
            try:
                content = json.loads(m.get("content") or "{}").get("content", "")
            except Exception:
                content = ""
            if content:
                out.append({"from": str(m.get("sender_uid")), "text": content[:200]})
        return out

    def dm_send(self, uid: int, text: str) -> None:
        if not self.csrf:
            raise RuntimeError("缺少 bili_jct，需要重新扫码登录")
        payload = {
            "msg[sender_uid]": self.client.cookies.get("DedeUserID", ""),
            "msg[receiver_id]": uid,
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "msg[timestamp]": int(time.time()),
            "msg[dev_id]": self._dev_id,
            "csrf": self.csrf,
            "csrf_token": self.csrf,
        }
        j = self._req("POST", f"{VC}/web_im/v1/web_im/send_msg", data=payload)
        if j.get("code") != 0:
            raise RuntimeError(f"私信发送失败：{j.get('message')} ({j.get('code')})")


# ====================================================================== 适配器
class BiliAdapter:
    """一轮一轮跑的巡逻体：逛推荐流 → 评论 → 回 @ → 回评论 →（可选）回私信。"""

    def __init__(self, cfg: Dict[str, Any], core, app=None) -> None:
        self.cfg = cfg
        self.core = core
        self.app = app
        self.client = BiliClient(cfg)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pause = threading.Event()
        self.state: Dict[str, Any] = {
            "logged_in": self.client.has_login(),
            "running": False,
            "halted": False,
            "halt_reason": "",
            "comments_today": memory.used_today("bili:comment"),
            "last_action": "",
            "last_error": "",
            "round": 0,
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.client.has_login():
            raise RuntimeError("还没登录 B 站，先扫码")
        self._stop.clear()
        self._pause.clear()
        self.state.update(running=True, halted=False, halt_reason="")
        self._thread = threading.Thread(target=self._loop, name="bili-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.state["running"] = False

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status(self) -> Dict[str, Any]:
        st = dict(self.state)
        st["comments_today"] = memory.used_today("bili:comment")
        st["dry_run"] = bool((self.cfg.get("bili") or {}).get("dry_run", True))
        st["limit"] = int((self.cfg.get("bili") or {}).get("daily_limit", 20))
        return st

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        b = self.cfg.get("bili") or {}
        try:
            while not self._stop.is_set():
                if self.state["halted"]:
                    time.sleep(2)
                    continue
                self.state["round"] = int(self.state.get("round", 0)) + 1
                try:
                    self._one_round()
                except RiskControlError as e:
                    self._halt(f"风控停机：{e}")
                    break
                except Exception as e:
                    self.state["last_error"] = str(e)
                    if self.app:
                        self.app.log_event("bili", f"本轮出错：{e}")
                    log.warning("B 站本轮出错：%s", e)
                    self._nap(60)
                    continue
                if not b.get("loop", True):
                    break
                self._nap(random_between(120, 240))
        finally:
            self.state["running"] = False
            if self.app:
                self.app.log_event("bili", "巡逻已停止")

    def _one_round(self) -> None:
        b = self.cfg.get("bili") or {}
        limit = int(b.get("daily_limit", 20))
        used = memory.used_today("bili:comment")
        if used >= limit:
            self.state["last_action"] = f"今日已达上限 {limit} 条，休息"
            self._nap(1800)
            return

        if b.get("comment_enabled", True):
            self._comment_round(int(b.get("videos_per_round", 15)))

        if b.get("reply_enabled", True):
            self._reply_to_mentions()

        if b.get("dm_enabled", False):
            self._reply_to_dms()

    # ------------------------------------------------------------ 主动评论
    def _comment_round(self, n: int) -> None:
        b = self.cfg.get("bili") or {}
        limit = int(b.get("daily_limit", 20))
        videos = self.client.feed(count=max(6, n))
        if self.app:
            self.app.log_event("bili", f"推荐流拿到 {len(videos)} 个视频")
        done = 0
        for v in videos:
            if self._stop.is_set() or done >= 5:
                break
            if memory.used_today("bili:comment") >= limit:
                break
            key = f"comment:{v['bvid']}"
            if memory.seen("bili", key):
                continue
            if self._skip_video(v):
                memory.mark("bili", key)
                continue

            aid = v.get("aid") or (self.client.video(v["bvid"]).get("aid"))
            if not aid:
                continue
            detail = self.client.video(v["bvid"])
            ctx = self._video_context(v, detail)
            text = self.core.reply(
                "bili",
                f"feed:{v['bvid']}",
                ctx,
                max_chars=int(b.get("max_reply_chars", 40)),
                system_extra="现在你要在评论区留一条公开评论。",
            )
            if not text:
                continue

            if b.get("dry_run", True):
                self.state["last_action"] = f"[演练] {v['bvid']}：{text}"
                memory.record_action("bili", "评论(演练)", v["bvid"], text, ok=False)
                if self.app:
                    self.app.log_event("bili", f"[演练] 将评论 {v['bvid']}：{text}")
                memory.mark("bili", key)  # 演练也标记，避免每轮重复生成
                done += 1
                continue

            self._pace()
            res = self.client.comment_add(int(aid), text)
            rpid = (res or {}).get("rpid") if isinstance(res, dict) else None
            memory.mark("bili", key)
            memory.bump_today("bili:comment")
            # 有 rpid 才算真的落了库；没有 rpid 基本等于"接口收下但不展示"
            if rpid:
                memory.record_action("bili", "评论", f"{v['bvid']}#rpid={rpid}", text, ok=True)
            else:
                memory.record_action("bili", "评论(待审核)", v["bvid"], text, ok=False)
            done += 1
            self.state["last_action"] = f"评论 {v['bvid']}：{text}"
            if self.app:
                self.app.log_event("bili", f"评论 {v['bvid']}：{text}")

    def _video_context(self, v: Dict[str, Any], detail: Dict[str, Any]) -> str:
        b = self.cfg.get("bili") or {}
        parts = [
            "你刚刷到一个视频，想留一条评论。",
            f"标题：{v.get('title') or detail.get('title')}",
            f"UP主：{v.get('up') or detail.get('up')}",
            f"分区：{v.get('tname') or detail.get('tname')}",
            f"播放：{v.get('views') or detail.get('views')}",
        ]
        desc = (v.get("desc") or detail.get("desc") or "").strip()
        if desc:
            parts.append(f"简介：{desc[:400]}")
        aid = detail.get("aid") or v.get("aid")
        if b.get("use_hot_comments", True) and aid:
            try:
                hot = self.client.hot_comments(int(aid), 5)
                if hot:
                    parts.append("热评参考（理解大家在聊什么，别照抄、别复读）：")
                    parts.extend(f"- {h}" for h in hot)
            except RiskControlError:
                raise
            except Exception:
                pass
        if b.get("use_subtitle") and aid and detail.get("cid"):
            sub = self.client.subtitle(int(aid), int(detail["cid"]))
            if sub:
                parts.append(f"字幕片段（可能有错字，只用来理解内容）：{sub[:600]}")
        parts.append("写一条像路人随手打的一句话评论，不要总结视频，不要提问，不要带链接。")
        return "\n".join(parts)

    def _skip_video(self, v: Dict[str, Any]) -> bool:
        b = self.cfg.get("bili") or {}
        text = (v.get("title") or "") + (v.get("desc") or "")
        for kw in (b.get("no_comment_keywords") or []):
            if kw and kw in text:
                return True
        return False

    # ------------------------------------------------------------ 回 @ 和评论
    def _reply_to_mentions(self) -> None:
        b = self.cfg.get("bili") or {}
        limit = int(b.get("daily_limit", 20))
        try:
            ats = self.client.msg_at()
        except RiskControlError:
            raise
        except Exception as e:
            self.state["last_error"] = f"拉 @ 失败：{e}"
            return

        for a in ats[:5]:
            if self._stop.is_set() or memory.used_today("bili:comment") >= limit:
                break
            key = f"at:{a['id']}"
            if not a["id"] or memory.seen("bili", key):
                continue
            if not a.get("source"):
                memory.mark("bili", key)
                continue

            user = a.get("user") or "有人"
            prompt = (
                f"{user} 在评论区 @ 了你，说：{(a.get('source') or '')[:200]}\n"
                f"相关视频：{a.get('title', '')}\n"
                "直接回他一句，像熟人搭话，别客套。"
            )
            out = self.core.reply(
                "bili",
                f"at:{a['id']}",
                prompt,
                name=user,
                max_chars=int(b.get("max_reply_chars", 40)),
            )
            if not out:
                continue
            if b.get("dry_run", True):
                self.state["last_action"] = f"[演练] 回 @{user}：{out}"
                memory.record_action("bili", "回@(演练)", str(a.get("id")), f"回 {user}：{out}", ok=False)
                if self.app:
                    self.app.log_event("bili", f"[演练] 回 @{user}：{out}")
                memory.mark("bili", key)
                continue

            self._pace()
            try:
                self._send_at_reply(a, out)
                memory.mark("bili", key)
                memory.bump_today("bili:comment")
                memory.record_action("bili", "回@", str(a.get("id")), f"回 {user}：{out}", ok=True)
                self.state["last_action"] = f"回 @{user}：{out}"
                if self.app:
                    self.app.log_event("bili", f"回 @{user}：{out}")
            except RiskControlError:
                raise
            except Exception as e:
                self.state["last_error"] = f"回 @ 失败：{e}"

    def _send_at_reply(self, a: Dict[str, Any], text: str) -> None:
        """@ 消息里带 uri（形如 bilibili://video/<aid>?rpid=...），从中还原目标。"""
        uri = a.get("uri") or ""
        m = re.search(r"video/(\d+)", uri)
        if not m:
            raise RuntimeError("这条 @ 不是视频评论，暂不支持")
        aid = int(m.group(1))
        rpid = 0
        mr = re.search(r"rpid=(\d+)", uri)
        if mr:
            rpid = int(mr.group(1))
        if rpid:
            self.client.reply_add(aid, rpid, text)
        else:
            self.client.comment_add(aid, text)

    # ------------------------------------------------------------ 私信
    def _reply_to_dms(self) -> None:
        b = self.cfg.get("bili") or {}
        try:
            sessions = self.client.dm_sessions()
        except RiskControlError:
            raise
        except Exception as e:
            self.state["last_error"] = f"拉私信失败：{e}"
            return
        for s in sessions[:5]:
            if self._stop.is_set():
                break
            uid = s.get("talker_id")
            if not uid or not s.get("unread"):
                continue
            key = f"dm:{uid}:{s.get('ts')}"
            if memory.seen("bili", key):
                continue
            hist = self.client.dm_history(int(uid), 3)
            if not hist:
                memory.mark("bili", key)
                continue
            last = hist[-1]["text"]
            out = self.core.reply(
                "bili",
                f"dm:{uid}",
                f"对方私信你：{last[:200]}\n像朋友一样回一句，别客套。",
                max_chars=int(b.get("max_reply_chars", 40)),
            )
            if not out:
                continue
            if b.get("dry_run", True):
                self.state["last_action"] = f"[演练] 私信回复 {uid}：{out}"
                memory.record_action("bili", "私信(演练)", str(uid), out, ok=False)
                memory.mark("bili", key)
                continue
            self._pace()
            self.client.dm_send(int(uid), out)
            memory.record_action("bili", "私信", str(uid), out, ok=True)
            memory.mark("bili", key)
            self.state["last_action"] = f"私信回复 {uid}：{out}"

    # ------------------------------------------------------------ 工具
    def _pace(self) -> None:
        b = self.cfg.get("bili") or {}
        lo = int(b.get("delay_min", 15))
        hi = int(b.get("delay_max", 45))
        t = random_between(lo, hi)
        self.state["waiting"] = t
        for _ in range(int(t)):
            if self._stop.is_set():
                return
            time.sleep(1)
        self.state.pop("waiting", None)

    def _nap(self, seconds: int) -> None:
        for _ in range(int(seconds)):
            if self._stop.is_set():
                return
            time.sleep(1)

    def _halt(self, reason: str) -> None:
        self.state.update(running=False, halted=True, halt_reason=reason, last_error=reason)
        log.error("B 站停机：%s", reason)
        if self.app:
            self.app.log_event("bili", f"⚠️ {reason}")


def random_between(lo: int, hi: int) -> int:
    import random

    if hi <= lo:
        return max(1, lo)
    return random.randint(lo, hi)
