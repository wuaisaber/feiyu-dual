"""首次运行辅助：下载并解压 NapCat。

为什么单独做成模块而不是写在 start.bat 里：
  * .bat 在 cmd 里按 OEM 码页解析，放中文会乱码；
  * 这里只用标准库（urllib / zipfile），所以在 pip 依赖装好之前就能跑。

用法：
    python -m app.setup_napcat
    python -m app.setup_napcat --yes      # 不询问直接下载
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAPCAT_DIR = ROOT / "napcat"
TARGET = NAPCAT_DIR / "NapCat.Shell" / "NapCatWinBootMain.exe"

# 想升级 NapCat 时只改这一行
VERSION = "v4.18.41"
FILENAME = "NapCat.Shell.zip"
SIZE_HINT = "约 35 MB"

OFFICIAL = f"https://github.com/NapNeko/NapCatQQ/releases/download/{VERSION}/{FILENAME}"
# 国内镜像，按顺序重试
MIRRORS = [
    f"https://ghfast.top/{OFFICIAL}",
    f"https://gh-proxy.com/{OFFICIAL}",
    f"https://ghproxy.net/{OFFICIAL}",
]


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def download(url: str, dest: Path) -> bool:
    """流式下载并打印百分比。返回是否成功。"""
    req = urllib.request.Request(url, headers={"User-Agent": "feiyu-dual-setup"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    sys.stdout.write(
                        f"\r  下载中 {pct:3d}%   {done / 1048576:.1f} / {total / 1048576:.1f} MB"
                    )
                    sys.stdout.flush()
        sys.stdout.write("\n")
        return dest.exists() and dest.stat().st_size > 1024 * 1024
    except Exception as e:
        sys.stdout.write("\n")
        _say(f"  失败：{e}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def extract(zip_path: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(NAPCAT_DIR)
        return TARGET.exists()
    except Exception as e:
        _say(f"  解压失败：{e}")
        return False


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="下载 NapCat")
    parser.add_argument("--yes", "-y", action="store_true", help="不询问，直接下载")
    args = parser.parse_args(argv)

    if TARGET.exists():
        _say(f"NapCat 已存在：{TARGET.parent}")
        return 0

    _say("")
    _say("=" * 54)
    _say("  QQ 接入需要一个第三方协议端 NapCat")
    _say("=" * 54)
    _say(f"  版本：{VERSION}  体积：{SIZE_HINT}")
    _say("  只用来把 QQ 消息转成 OneBot 协议，不碰你的聊天记录。")
    _say("")

    if not args.yes:
        try:
            ans = input("  现在下载吗？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "不了"):
            _say("")
            _say("  已跳过。QQ 通道暂不可用；B 站功能不受影响。")
            _say("  之后随时可以重新运行：python -m app.setup_napcat")
            return 0

    NAPCAT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / FILENAME

    _say("")
    _say("  开始下载，国内镜像优先……")
    for i, url in enumerate([OFFICIAL] + MIRRORS, 1):
        host = url.split("/")[2]
        _say(f"  [{i}/{len(MIRRORS) + 1}] {host}")
        if download(url, tmp):
            _say("  下载完成，正在解压……")
            if extract(tmp):
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                _say("")
                _say(f"  ✔ 装好了：{TARGET.parent}")
                _say("")
                return 0
            _say("  解压后没找到 NapCatWinBootMain.exe，换下一个源")
        # 清理半成品
        try:
            (NAPCAT_DIR / "NapCat.Shell").exists() and shutil.rmtree(NAPCAT_DIR / "NapCat.Shell", ignore_errors=True)
        except Exception:
            pass

    _say("")
    _say("  全部源都失败了。可以手动下载后解压到 napcat/ 目录：")
    _say(f"    {OFFICIAL}")
    _say("  解压后目录应为 napcat/NapCat.Shell/NapCatWinBootMain.exe")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
