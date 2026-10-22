"""本地 Web 控制台：开箱即用的那半条命。

只用标准库 http.server，不引第三方 Web 框架——用户双击 start.bat 就能看到界面。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from . import config, memory, personas
from .context import AppContext

log = logging.getLogger("feiyu.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

_QR_STATE: Dict[str, Any] = {}



_FALLBACK_PAGE = (
    "<!doctype html><meta charset=\"utf-8\">"
    "<title>页面缺失</title>"
    "<h1 style=\"font:16px sans-serif;padding:24px\">控制台页面文件缺失</h1>"
    "<p style=\"font:14px sans-serif;padding:0 24px;color:#666\">"
    "请确认 app/static/index.html 存在；重新解压或重新 clone 本仓库即可恢复。"
    "</p>"
)


def _index_html() -> str:
    """优先读 static/index.html（改样式不用动 Python），读不到再退回内嵌版本。"""
    p = STATIC_DIR / "index.html"
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("读取 static/index.html 失败（%s），使用内置页面", e)
        return _FALLBACK_PAGE


class Handler(BaseHTTPRequestHandler):
    ctx: AppContext

    server_version = "FeiyuDual/0.1"

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # 屏蔽默认 stderr 噪音
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._send(200, _index_html().encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(self.ctx.status())
            elif path == "/api/personas":
                self._json(
                    {
                        "ok": True,
                        "names": personas.persona_names(),
                        "options": personas.persona_options(self.ctx.cfg),
                    }
                )
            elif path == "/api/config/get":
                self._json({"ok": True, **self.ctx.cfg})
            elif path == "/api/bili/qr/poll":
                key = (qs.get("key") or [""])[0]
                self._json(self._poll_qr(key))
            elif path == "/api/history":
                plat = (qs.get("platform") or [""])[0]
                kind = (qs.get("kind") or [""])[0]
                try:
                    lim = int((qs.get("limit") or ["300"])[0] or 300)
                    off = int((qs.get("offset") or ["0"])[0] or 0)
                except ValueError:
                    lim, off = 300, 0
                self._json(
                    {
                        "ok": True,
                        "items": memory.actions(lim, plat, kind, off),
                        "stats": memory.action_stats(plat),
                    }
                )
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("GET %s 出错", path)
            self._json({"error": str(e)}, 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/config":
                section = body.get("section") or ""
                values = body.get("values") or {}
                if not section:
                    return self._json({"error": "缺少 section"}, 400)
                config.patch(section, values)
                self.ctx.reload()
                return self._json({"ok": True})
            if path == "/api/llm/test":
                return self._json(self.ctx.core.llm.test())
            if path == "/api/qq/start":
                config.patch("qq", {"enabled": True})
                self.ctx.reload()
                return self._json({"ok": True, **self.ctx.start_qq()})
            if path == "/api/qq/stop":
                config.patch("qq", {"enabled": False})
                self.ctx.reload()
                self.ctx.stop_qq()
                return self._json({"ok": True})
            if path == "/api/bili/start":
                config.patch("bili", {"enabled": True})
                self.ctx.reload()
                return self._json({"ok": True, **self.ctx.start_bili()})
            if path == "/api/bili/stop":
                config.patch("bili", {"enabled": False})
                self.ctx.reload()
                self.ctx.stop_bili()
                return self._json({"ok": True})
            if path == "/api/bili/qr":
                return self._json(self._make_qr())
            if path == "/api/bili/logout":
                if self.ctx.bili:
                    self.ctx.bili.stop()
                config.save_cookie({})
                return self._json({"ok": True})
            if path == "/api/memory/clear":
                for plat in ("qq", "bili"):
                    for ch in _channels(plat):
                        memory.clear(plat, ch)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("POST %s 出错", path)
            return self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------------ B站
    def _make_qr(self) -> Dict[str, Any]:
        try:
            adapter = self.ctx.ensure_bili()
            data = adapter.client.qr_generate()
        except Exception as e:
            return {"ok": False, "error": f"生成二维码失败：{e}"}
        _QR_STATE["key"] = data["key"]
        _QR_STATE["client"] = adapter.client
        img = _qr_png(data["url"] or "")
        return {"ok": True, "key": data["key"], "img": img, "url": data["url"]}

    def _poll_qr(self, key: str) -> Dict[str, Any]:
        client = _QR_STATE.get("client")
        if not key or not client:
            return {"status": "waiting", "detail": "没有可用的二维码，重新生成"}
        try:
            res = client.qr_poll(key)
        except Exception as e:
            return {"status": "waiting", "detail": f"轮询失败：{e}"}
        if res.get("status") == "success":
            self.ctx.ensure_bili()
            self.ctx.log_event("bili", "扫码登录成功")
        return res


def _channels(platform: str) -> list[str]:
    """把库里出现过的会话都找出来（清记忆用）。"""
    import sqlite3

    try:
        con = sqlite3.connect(str(config.MEMORY_PATH))
        rows = con.execute(
            "SELECT DISTINCT channel FROM messages WHERE platform=?", (platform,)
        ).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _qr_png(text: str) -> str:
    """把登录 URL 画成 PNG，返回 data URI。没装 qrcode 库时返回空串。"""
    if not text:
        return ""
    try:
        import qrcode  # type: ignore

        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def serve(ctx: AppContext, host: str, port: int) -> ThreadingHTTPServer:
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="web", daemon=True)
    t.start()
    return httpd
