"""端到端链路自测（不联网）：用假的模型和假的发送器，验证"事件进 → 回复出"。

运行：python -m tests.e2e_mock
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，会把 ✔ 和中文打崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.onebot import OneBotAdapter  # noqa: E402
from app.core import Core  # noqa: E402

CALLS = []


def fake_chat(messages, **kw):
    # 把 system prompt 和最后一条用户消息都记下来，方便断言
    CALLS.append({"system": messages[0]["content"], "user": messages[-1]["content"]})
    return "**好耶**，在的在的。"


def make_adapter(**qq):
    cfg = {
        "llm": {"api_key": "sk-test"},
        "qq": {
            "ws_url": "ws://127.0.0.1:3001",
            "at_only": True,
            "auto_chat_every": 0,
            "allowed_groups": [],
            "allowed_friends": [],
            "max_reply_chars": 60,
            **qq,
        },
        "persona": {"active": "default", "custom": {}},
        "safety": {"banned_words": [], "history_rounds": 4, "cooldown_seconds": 0},
    }
    core = Core(cfg)
    core.llm.chat = fake_chat  # type: ignore[assignment]
    ad = OneBotAdapter(cfg, core)
    sent = []

    def send_group(gid, text, image=""):
        sent.append(("group", gid, text))
        return True

    def send_private(uid, text, image=""):
        sent.append(("private", uid, text))
        return True

    ad.send_group = send_group  # type: ignore[assignment]
    ad.send_private = send_private  # type: ignore[assignment]
    return ad, sent, cfg


def group_event(text, at_me=True):
    msg = []
    if at_me:
        msg.append({"type": "at", "data": {"qq": "10000"}})
    msg.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10000,
        "group_id": 123456,
        "user_id": 20001,
        "sender": {"card": "小明"},
        "message": msg,
    }


def main() -> int:
    fails = []

    # 1) @ 我 → 应该回，且 markdown 被清掉
    ad, sent, _ = make_adapter()
    ad._handle(group_event(" 在吗"))
    if not sent:
        fails.append("被 @ 时没有回复")
    else:
        kind, gid, text = sent[0]
        if kind != "group" or gid != 123456:
            fails.append(f"发到了错误的通道：{kind},{gid}")
        if "*" in text:
            fails.append(f"markdown 没清理：{text}")

    # 2) 没 @ 且 auto_chat_every=0 → 不回
    ad2, sent2, _ = make_adapter()
    ad2._handle(group_event("今天天气不错", at_me=False))
    if sent2:
        fails.append("没 @ 也回复了")

    # 3) 没 @ 但每 2 条接一次 → 第 2 条要回
    ad3, sent3, _ = make_adapter(auto_chat_every=2)
    ad3._handle(group_event("第一条", at_me=False))
    ad3._handle(group_event("第二条", at_me=False))
    if len(sent3) != 1:
        fails.append(f"每 N 条接话逻辑异常，发了 {len(sent3)} 条")

    # 4) 群白名单拦截
    ad4, sent4, _ = make_adapter(allowed_groups=[999])
    ad4._handle(group_event(" 在吗"))
    if sent4:
        fails.append("白名单没生效")

    # 5) 私聊 → 要回
    ad5, sent5, _ = make_adapter()
    ad5._handle(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 10000,
            "user_id": 20002,
            "sender": {"nickname": "小红"},
            "message": "你好呀",
        }
    )
    if not sent5 or sent5[0][0] != "private":
        fails.append("私聊没回复")

    # 6) 自己说的 → 不回
    ad6, sent6, _ = make_adapter()
    ev = group_event(" 我自己", at_me=True)
    ev["user_id"] = 10000
    ad6._handle(ev)
    if sent6:
        fails.append("回复了自己的消息")

    # 7) system prompt 里应该是 QQ 规则，不是 B 站规则
    if CALLS and "你在 B 站评论区" in CALLS[0]["system"]:
        fails.append("QQ 通道用错了平台规则")

    # 8) B 站 context 拼装（纯字符串，不联网）
    from app.adapters.bilibili import BiliAdapter

    bcfg = {
        "llm": {"api_key": "sk-test"},
        "bili": {"use_hot_comments": False, "use_subtitle": False, "no_comment_keywords": ["抽奖"]},
        "persona": {"active": "default", "custom": {}},
        "safety": {"banned_words": []},
    }
    ba = BiliAdapter(bcfg, Core(bcfg))
    ctx = ba._video_context(
        {"bvid": "BV1xx", "title": "测试视频", "up": "UP主", "tname": "生活", "views": 100},
        {"aid": 1, "desc": "简介内容", "cid": 2},
    )
    for must in ("测试视频", "UP主", "简介内容"):
        if must not in ctx:
            fails.append(f"B站上下文缺少 {must}")
    if not ba._skip_video({"title": "抽奖活动", "desc": ""}):
        fails.append("跳过词没生效")

    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print(f"✔ 端到端链路通过（{len(CALLS)} 次模型调用，{len(sent) + len(sent3) + len(sent5)} 次发送）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
