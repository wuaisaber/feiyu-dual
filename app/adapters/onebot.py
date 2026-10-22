"""QQ 通道：OneBot 11 正向 WebSocket。

为什么要这层：QQ 本身没有可供第三方调用的接口，社区通行做法是让一个"协议端"
（NapCat / LLOneBot / go-cqhttp）登录 QQ，再由它用 OneBot 协议把事件推给我们。
所以 QQ 侧只需要会连 WS、会收事件、会发 action 就够了。

连接方式（正向）：本程序作为客户端，去连 NapCat 的 ws://127.0.0.1:3001。
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import websocket  # websocket-client

from .. import config as app_config
from .. import memory

log = logging.getLogger("feiyu.qq")

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# 模型用这个标记点表情包，例如：在呢 [表情:歪头]
_STICKER_RE = re.compile(r"[\[【](?:表情|表情包|贴纸|图)[:：]\s*([^\]】\s]{1,16})\s*[\]】]")


def list_stickers(sticker_dir: str = "") -> List[str]:
    """列出表情包名字（文件名去掉扩展名）。"""
    d = Path(sticker_dir or "data/stickers")
    if not d.is_absolute():
        d = app_config.ROOT / d
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXT)


def split_sticker(text: str) -> tuple[str, str]:
    """从回复里抽出表情标记，返回（去掉标记的正文, 表情名）。"""
    if not text:
        return text, ""
    m = _STICKER_RE.search(text)
    if not m:
        return text, ""
    return _STICKER_RE.sub("", text).strip(), m.group(1).strip()


def sticker_image(name: str, sticker_dir: str = "") -> str:
    """把表情文件读成 base64://，OneBot 各实现都认这个格式。"""
    if not name:
        return ""
    d = Path(sticker_dir or "data/stickers")
    if not d.is_absolute():
        d = app_config.ROOT / d
    if not d.is_dir():
        return ""
    for p in d.iterdir():
        if p.is_file() and p.stem == name and p.suffix.lower() in _IMAGE_EXT:
            try:
                return "base64://" + base64.b64encode(p.read_bytes()).decode()
            except Exception as e:
                log.warning("读取表情 %s 失败：%s", p, e)
                return ""
    return ""

_CQ_RE = re.compile(r"\[CQ:(\w+)((?:,[^\]]*)?)\]")


def _parse_cq_params(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in raw.lstrip(",").split(","):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def extract_message(msg: Any) -> Dict[str, Any]:
    """把 message 字段（数组或 CQ 码字符串）拆成 文本 / @列表 / 图片数 / 引用id。"""
    texts: List[str] = []
    ats: List[str] = []
    images = 0
    reply_id = ""

    if isinstance(msg, list):
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            data = seg.get("data") or {}
            if t == "text":
                texts.append(str(data.get("text", "")))
            elif t == "at":
                qq = str(data.get("qq", ""))
                if qq:
                    ats.append(qq)
                texts.append(" ")  # @ 后面通常跟文字，留个空格
            elif t == "image":
                images += 1
                texts.append(" [图片] ")
            elif t == "reply":
                reply_id = str(data.get("id", ""))
            elif t == "face":
                pass
    else:
        s = str(msg or "")
        pos = 0
        for m in _CQ_RE.finditer(s):
            texts.append(s[pos : m.start()])
            pos = m.end()
            kind, params = m.group(1), _parse_cq_params(m.group(2))
            if kind == "at":
                qq = params.get("qq", "")
                if qq and qq != "all":
                    ats.append(qq)
            elif kind == "image":
                images += 1
                texts.append(" [图片] ")
            elif kind == "reply":
                reply_id = params.get("id", "")
        texts.append(s[pos:])

    text = "".join(texts)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return {"text": text, "at": ats, "images": images, "reply_id": reply_id}


class OneBotAdapter:
    def __init__(self, cfg: Dict[str, Any], core, app=None) -> None:
        self.cfg = cfg
        self.core = core
        self.app = app  # AppContext，用来共享状态
        self._ws: Optional[websocket.WebSocket] = None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._responses: Dict[str, Any] = {}
        self._seq = 0
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)
        self._last_speak: Dict[str, float] = {}
        self._group_counter: Dict[str, int] = {}
        self.self_id: int = int((cfg.get("qq") or {}).get("self_id") or 0)
        self.state: Dict[str, Any] = {
            "connected": False,
            "self_id": self.self_id,
            "nickname": "",
            "error": "",
            "events": 0,
            "replies": 0,
            "since": 0.0,
        }

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        t = threading.Thread(target=self._reader_loop, name="qq-reader", daemon=True)
        w = threading.Thread(target=self._worker_loop, name="qq-worker", daemon=True)
        for x in (t, w):
            x.start()
            self._threads.append(x)
        log.info("QQ 通道已启动，目标 %s", (self.cfg.get("qq") or {}).get("ws_url"))

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self.state["connected"] = False
        self._threads.clear()

    def status(self) -> Dict[str, Any]:
        return dict(self.state)

    # ------------------------------------------------------------------ 连接
    def _url(self) -> str:
        qq = self.cfg.get("qq") or {}
        url = (qq.get("ws_url") or "ws://127.0.0.1:3001").strip()
        token = (qq.get("access_token") or "").strip()
        if token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={token}"
        return url

    def _reader_loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    self._url(), timeout=10, enable_multithread=True, skip_utf8_validation=True
                )
                ws.settimeout(1.0)
                self._ws = ws
                self.state.update(connected=True, error="", since=time.time())
                log.info("QQ 已连接到 OneBot")
                backoff = 2.0
                self._read_loop(ws)
            except Exception as e:
                msg = str(e)
                if not self._stop.is_set():
                    log.warning("QQ 连接断开：%s", msg)
                    self.state.update(connected=False, error=msg)
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 15.0)
            finally:
                self._ws = None
                self.state["connected"] = False

    def _read_loop(self, ws: websocket.WebSocket) -> None:
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if "echo" in obj and obj.get("echo"):
                with self._cv:
                    self._responses[str(obj["echo"])] = obj
                    self._cv.notify_all()
                continue
            if obj.get("post_type"):
                self.state["events"] = int(self.state.get("events", 0)) + 1
                try:
                    self._q.put_nowait(obj)
                except queue.Full:
                    log.warning("QQ 事件队列已满，丢弃一条")

    # ------------------------------------------------------------------ 调用
    def _call(self, action: str, params: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        ws = self._ws
        if ws is None:
            return None
        with self._send_lock:
            self._seq += 1
            echo = f"f{self._seq}"
            try:
                ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            except Exception as e:
                log.warning("发送 action 失败：%s", e)
                return None
        with self._cv:
            got = self._cv.wait_for(lambda: echo in self._responses, timeout=timeout)
            data = self._responses.pop(echo, None)
        if not got:
            return None
        return data

    def _send_text(self, kind: str, target: int, text: str, image: str = "") -> bool:
        if not text and not image:
            return False
        segs: List[Dict[str, Any]] = []
        if image:
            segs.append({"type": "image", "data": {"file": image}})
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        key = "group_id" if kind == "group" else "user_id"
        resp = self._call(action, {key: target, "message": segs}, timeout=20)
        ok = bool(resp and resp.get("status") in ("ok", "async", "success"))
        if ok:
            self.state["replies"] = int(self.state.get("replies", 0)) + 1
        else:
            log.warning("发送失败：%s", (resp or {}).get("message") or resp)
        return ok

    def send_group(self, group_id: int, text: str, image: str = "") -> bool:
        return self._send_text("group", int(group_id), text, image)

    def send_private(self, user_id: int, text: str, image: str = "") -> bool:
        return self._send_text("private", int(user_id), text, image)

    # ------------------------------------------------------------------ 表情包
    def _sticker_hint(self) -> str:
        """启用表情包时，把可用名字告诉模型。"""
        qq = self.cfg.get("qq") or {}
        if not qq.get("sticker_enabled"):
            return ""
        names = list_stickers(qq.get("sticker_dir") or "")
        if not names:
            return ""
        return (
            "你可以在回复结尾加一个表情标记，格式写作 [表情:名字]，程序会自动换成表情包。"
            "可用名字只有这些：" + "、".join(names[:40]) + "。"
            "不要每句都加，大概三四句里偶尔带一个，而且只在情绪明显的时候用。"
        )

    def _prepare_outgoing(self, text: str) -> tuple[str, str]:
        """把模型输出切成（正文, 图片资源）。"""
        qq = self.cfg.get("qq") or {}
        if not qq.get("sticker_enabled"):
            return text, ""
        clean, name = split_sticker(text)
        img = sticker_image(name, qq.get("sticker_dir") or "")
        if not img:
            return clean, ""
        return clean, img

    # ------------------------------------------------------------------ 事件
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(ev)
            except Exception as e:
                log.exception("处理 QQ 事件出错：%s", e)

    def _handle(self, ev: Dict[str, Any]) -> None:
        if ev.get("post_type") != "message":
            return
        qq = self.cfg.get("qq") or {}
        self_id = int(ev.get("self_id") or self.self_id or 0)
        if self_id and not self.self_id:
            self.self_id = self_id
            self.state["self_id"] = self_id

        mtype = ev.get("message_type")
        sender = ev.get("sender") or {}
        user_id = int(ev.get("user_id") or 0)
        name = str(sender.get("card") or sender.get("nickname") or user_id)

        if user_id and self_id and user_id == self_id:
            return  # 自己说的

        info = extract_message(ev.get("message"))
        text = info["text"]
        if not text:
            if info["images"] and not info["text"]:
                text = "[图片]"
            else:
                return

        at_me = bool(self_id and str(self_id) in info["at"])
        is_at_all = "all" in [str(x) for x in info["at"]]

        if mtype == "group":
            gid = int(ev.get("group_id") or 0)
            allow = [int(x) for x in (qq.get("allowed_groups") or [])]
            if allow and gid not in allow:
                return
            if qq.get("at_only", True):
                if not at_me:
                    # 没 @ 就只在"每 N 条抽一次"时才接话
                    every = int(qq.get("auto_chat_every") or 0)
                    if every > 0:
                        key = str(gid)
                        self._group_counter[key] = self._group_counter.get(key, 0) + 1
                        if self._group_counter[key] % every != 0:
                            return
                    else:
                        return
            elif is_at_all:
                return
            channel = f"group:{gid}"
            if not self._allow_rate(channel):
                return
            prompt = self._build_prompt(name, text, group=True, at_me=at_me)
            out = self.core.reply(
                "qq",
                channel,
                prompt,
                name=name,
                max_chars=int(qq.get("max_reply_chars", 60)),
                system_extra=self._sticker_hint(),
            )
            if out:
                body, img = self._prepare_outgoing(out)
                if not body and not img:
                    return
                time.sleep(random.uniform(0.4, 1.2))  # 别秒回，像个人
                ok = self.send_group(gid, body, image=img)
                memory.record_action(
                    "qq", "群回复", f"群 {gid}", f"回 {name}：{body}" + (" [表情]" if img else ""), ok=ok
                )
                self._last_speak[channel] = time.time()
                if self.app:
                    self.app.log_event("qq", f"群 {gid} 回复 {name}：{body[:40]}")
        elif mtype == "private":
            allow = [int(x) for x in (qq.get("allowed_friends") or [])]
            if allow and user_id not in allow:
                return
            channel = f"private:{user_id}"
            if not self._allow_rate(channel):
                return
            prompt = self._build_prompt(name, text, group=False, at_me=True)
            out = self.core.reply(
                "qq",
                channel,
                prompt,
                name=name,
                max_chars=int(qq.get("max_reply_chars", 60)),
                system_extra=self._sticker_hint(),
            )
            if out:
                body, img = self._prepare_outgoing(out)
                if not body and not img:
                    return
                time.sleep(random.uniform(0.4, 1.2))
                ok = self.send_private(user_id, body, image=img)
                memory.record_action(
                    "qq", "私聊回复", f"QQ {user_id}", f"回 {name}：{body}" + (" [表情]" if img else ""), ok=ok
                )
                self._last_speak[channel] = time.time()
                if self.app:
                    self.app.log_event("qq", f"私聊回复 {name}：{body[:40]}")

    def _allow_rate(self, channel: str) -> bool:
        """同一个会话最少隔 3 秒，防刷屏。"""
        last = self._last_speak.get(channel, 0.0)
        return (time.time() - last) >= 3.0

    @staticmethod
    def _build_prompt(name: str, text: str, *, group: bool, at_me: bool) -> str:
        if group:
            prefix = f"{name} @你 说：" if at_me else f"{name} 在群里说："
        else:
            prefix = f"{name} 私聊你："
        return f"{prefix}{text}"
