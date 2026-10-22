"""入口：python -m app.main [run|selftest|doctor|bili-login]

默认什么都不带就是 run：开 Web 控制台，按配置把两个通道拉起来。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import webbrowser
from pathlib import Path

from . import config
from .context import AppContext


def setup_logging() -> None:
    config.ensure_dirs()
    # Windows 控制台默认 GBK，日志里全是中文，不转码会花屏
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(config.LOG_DIR / "feiyu.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------- 命令
def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load()
    web = cfg.get("web") or {}
    host = args.host or web.get("host") or "127.0.0.1"
    port = int(args.port or web.get("port") or 8787)

    if not port_free(host, port):
        print(f"[×] 端口 {port} 被占用，换一个：python -m app.main run --port {port + 1}")
        return 1

    ctx = AppContext()
    from . import webui

    httpd = webui.serve(ctx, host, port)
    url = f"http://{host}:{port}/"
    print("=" * 56)
    print("  🐟 飞鱼双栖 已启动")
    print(f"  控制台：{url}")
    print("=" * 56)

    if not config.ready_for_llm(cfg):
        print("[!] 还没填 DeepSeek API Key，去控制台「设置」里填一个。")
    if cfg.get("qq", {}).get("enabled"):
        try:
            ctx.start_qq()
            print("[+] QQ 通道已启动（等待 NapCat）")
        except Exception as e:
            print(f"[×] QQ 启动失败：{e}")
    if cfg.get("bili", {}).get("enabled"):
        try:
            ctx.start_bili()
            print("[+] B 站巡逻已启动")
        except Exception as e:
            print(f"[×] B 站启动失败：{e}")

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        ctx.shutdown()
        httpd.shutdown()
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    from .adapters.bilibili import BiliClient, _MIXIN_TAB
    from .core import Core

    print("自检中（不联网）…")
    problems = []

    try:
        cfg = config.load()
    except Exception as e:
        print(f"[×] 配置读取失败：{e}")
        return 1

    try:
        core = Core(cfg)
        problems.extend(core.selftest())
    except Exception as e:
        problems.append(f"内核初始化失败：{e}")

    try:
        client = BiliClient(cfg)
        signed = client.sign({"foo": "bar"})
        if "w_rid" not in signed or "wts" not in signed:
            problems.append("wbi 签名没产出 w_rid/wts")
        if len(_MIXIN_TAB) != 64:
            problems.append("mixin 表长度不对")
    except Exception as e:
        # 没网时签名会失败，这不算错误
        print(f"  (wbi 签名跳过：{e})")

    from .adapters.onebot import extract_message

    m = extract_message([{"type": "at", "data": {"qq": "123"}}, {"type": "text", "data": {"text": " 你好"}}])
    if "你好" not in m["text"] or "123" not in m["at"]:
        problems.append("OneBot 数组消息解析异常")
    m2 = extract_message(" hi[CQ:at,qq=9] yo ")
    if "9" not in m2["at"]:
        problems.append("CQ 码解析异常")

    if problems:
        print("\n发现问题：")
        for p in problems:
            print("  -", p)
        return 1

    print("✔ 全部通过")
    from .personas import PERSONAS

    print(f"  人格数：{len(PERSONAS)}")
    print(f"  数据目录：{config.DATA_DIR}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    print("环境检查")
    print(f"  Python：{sys.version.split()[0]}  ({sys.executable})")
    print(f"  工作目录：{Path.cwd()}")
    print(f"  数据目录：{config.DATA_DIR}")
    for mod in ("websockets", "httpx", "yaml", "PIL", "qrcode", "websocket"):
        try:
            __import__(mod)
            print(f"  依赖 {mod}：OK")
        except Exception:
            print(f"  依赖 {mod}：缺失（pip install -r requirements.txt）")

    cfg = config.load()
    print(f"  API Key：{'已填' if config.ready_for_llm(cfg) else '未填'}")
    print(f"  QQ 通道：{'启用' if cfg['qq'].get('enabled') else '未启用'}  目标 {cfg['qq'].get('ws_url')}")
    print(f"  B站：{'已登录' if config.load_cookie().get('cookies', {}).get('SESSDATA') else '未登录'}")

    # 探一下本地 OneBot 端口
    import urllib.parse

    ws = (cfg["qq"].get("ws_url") or "")
    try:
        u = urllib.parse.urlparse(ws)
        host, port = u.hostname or "127.0.0.1", u.port or 3001
        with socket.create_connection((host, port), timeout=2):
            print(f"  OneBot 端口 {host}:{port}：可连接")
    except Exception as e:
        print(f"  OneBot 端口不可达：{e}（NapCat 没开或端口不对）")
    return 0


def cmd_bili_login(_: argparse.Namespace) -> int:
    """没有界面时的兜底：终端里打印链接，用 B 站 App 扫。"""
    from .adapters.bilibili import BiliClient

    cfg = config.load()
    client = BiliClient(cfg)
    data = client.qr_generate()
    print("在手机 B 站 App 里扫这个链接对应的二维码：")
    print("  " + data["url"])
    print("（或用任意二维码工具把上面这行转成二维码再扫）")
    while True:
        r = client.qr_poll(data["key"])
        st = r.get("status")
        if st == "success":
            print("✔ 登录成功：" + str((r.get("user") or {}).get("uname")))
            return 0
        if st == "expired":
            print("× 二维码过期，重新运行命令")
            return 1
        if st == "scanned":
            print("… 已扫码，等待确认")
        time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="feiyu-dual", description="大肥鱼 · QQ + B站 双栖")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="启动控制台与各通道（默认）")
    p_run.add_argument("--host", default="")
    p_run.add_argument("--port", type=int, default=0)
    p_run.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    sub.add_parser("selftest", help="离线自检")
    sub.add_parser("doctor", help="环境检查")
    sub.add_parser("bili-login", help="终端里扫码登录 B 站")

    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    if cmd == "run":
        if not getattr(args, "host", None):
            args.host = ""
        if not getattr(args, "port", None):
            args.port = 0
        if not hasattr(args, "no_open"):
            args.no_open = False
        return cmd_run(args)
    if cmd == "selftest":
        return cmd_selftest(args)
    if cmd == "doctor":
        return cmd_doctor(args)
    if cmd == "bili-login":
        return cmd_bili_login(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
