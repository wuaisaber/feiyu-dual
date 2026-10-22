"""从日志回填行为记录。

只用于修复历史版本：早期代码发出了消息但不写 actions 表，日志里有痕迹、
数据库里没有。正常运行时不需要跑这个。

用法：python -m app.backfill
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

from . import config, memory

# 日志行首是 HH:MM:SS，没有日期，所以回填的时间统一按"今天"算
PATTERNS: List[Tuple[re.Pattern, str, str, bool]] = [
    (re.compile(r"\[bili\] \[演练\] 将评论 (BV[0-9A-Za-z]+)[:：](.+)$"), "bili", "评论(演练)", False),
    (re.compile(r"\[bili\] 评论 (BV[0-9A-Za-z]+)[:：](.+)$"), "bili", "评论", True),
    (re.compile(r"\[bili\] \[演练\] 回 @(\S+?)[:：](.+)$"), "bili", "回@(演练)", False),
    (re.compile(r"\[bili\] 回 @(\S+?)[:：](.+)$"), "bili", "回@", True),
    (re.compile(r"\[qq\] 群 (\d+) 回复 (\S+?)[:：](.+)$"), "qq", "群回复", True),
    (re.compile(r"\[qq\] 私聊回复 (\S+?)[:：](.+)$"), "qq", "私聊回复", True),
]


def scan(log_path: Path) -> List[Tuple[str, str, str, str, bool]]:
    found: List[Tuple[str, str, str, str, bool]] = []
    if not log_path.exists():
        return found
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        for pat, plat, kind, ok in PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            if plat == "qq" and kind == "群回复":
                target, who, content = "群 " + m.group(1), m.group(2), m.group(3)
                text = f"回 {who}：{content}"
            elif plat == "qq":
                target, who, content = "QQ ?", m.group(1), m.group(2)
                text = f"回 {who}：{content}"
            else:
                target, text = m.group(1), m.group(2)
            found.append((plat, kind, target, text.strip(), ok))
            break
    return found


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    log_path = config.LOG_DIR / "feiyu.log"
    rows = scan(log_path)
    if not rows:
        print("日志里没有可回填的记录")
        return 0

    existing = memory.actions(2000)
    seen = {(a["platform"], a["kind"], a["target"], a["content"]) for a in existing}

    added = skipped = 0
    for plat, kind, target, content, ok in reversed(rows):  # 按时间正序写入
        key = (plat, kind, target, content)
        if key in seen:
            skipped += 1
            continue
        memory.record_action(plat, kind, target, content, ok=ok)
        seen.add(key)
        added += 1

    print(f"扫描 {len(rows)} 条，新增 {added} 条，跳过重复 {skipped} 条")
    print("现在去控制台「QQ 轨迹 / B站轨迹」应该能看到了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
