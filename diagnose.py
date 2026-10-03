#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断工具（只读，绝不删除任何消息）
==================================

用途：采样群消息，导出 restriction_reason 的真实分布，以及"内容是否可见"的
交叉统计，帮助精确判定哪些才是真正"内容看不到"的受限消息。

用法：
  python diagnose.py <群组ID或链接> [--limit 3000] [--proxy socks5://...]

把终端输出（或 reports/diagnose_*.json）发回，即可据此收窄删除判据。
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

# 复用 cleaner 的连接与配置逻辑
from cleaner import (TelegramClient, SESSION_PATH, load_config, resolve_proxy,
                     ensure_login, resolve_chat, log)


def media_kind(msg) -> str:
    m = getattr(msg, "media", None)
    if m is None:
        return "none"
    return type(m).__name__


# Telegram 对 ToS 违规消息会把正文替换成占位提示，含这些片段即"内容不可见"
PLACEHOLDER_MARKERS = (
    "can’t be displayed", "can't be displayed",
    "couldn’t be displayed", "couldn't be displayed",
    "violated telegram", "violates the telegram",
    "terms of service",
)
# restriction_reason 中代表 ToS 违规（内容被隐藏）的 reason 取值
TOS_REASONS = {"terms", "tos"}


def is_placeholder_text(msg) -> bool:
    t = (getattr(msg, "message", "") or "").lower()
    return any(m in t for m in PLACEHOLDER_MARKERS)


def reasons_of(msg):
    rr = getattr(msg, "restriction_reason", None) or []
    return {(getattr(r, "platform", "") or "", getattr(r, "reason", "") or "")
            for r in rr}


def has_tos_reason(msg) -> bool:
    return any(rsn in TOS_REASONS for (_p, rsn) in reasons_of(msg))


def has_visible_content(msg) -> tuple:
    """返回 (有无文字, 有无媒体, 媒体类型名)。"""
    text = (getattr(msg, "raw_text", "") or "").strip()
    mk = media_kind(msg)
    has_media = mk not in ("none", "MessageMediaEmpty")
    return bool(text), has_media, mk


async def diagnose(args) -> None:
    cfg = load_config(getattr(args, "proxy", None))
    proxy = resolve_proxy(args, cfg)
    async with TelegramClient(str(SESSION_PATH), cfg["api_id"],
                              cfg["api_hash"], proxy=proxy) as client:
        await ensure_login(client)
        chat = await resolve_chat(client, args.chat)
        chat_id = getattr(chat, "id", "x")
        chat_title = getattr(chat, "title", None) or chat_id
        log(f"目标群组：{chat_title}（id={chat_id}）")
        log(f"采样上限：{args.limit} 条（只读，不会删除任何消息）\n")

        reason_pairs = Counter()     # 每个 (platform, reason) 出现的消息数
        reason_only = Counter()      # 仅按 reason 统计的消息数
        vis_by_reason = {}           # reason -> Counter{(占位文字?, 有媒体?)}
        candidate = {                # 各候选判据会命中的数量
            "A_任意restriction_reason(当前过宽判据)": 0,
            "B_reason含terms/tos(ToS违规)": 0,
            "C_正文是占位提示文字": 0,
            "D_B且C(双信号,最严格)": 0,
            "E_B或C": 0,
        }
        # 交叉核对：B 与 C 是否一致
        b_not_c = 0                  # reason=terms 但正文不是占位（需人工看）
        c_not_b = 0                  # 正文是占位但 reason 不含 terms/tos
        samples_tos = []             # reason=terms/tos 的样本
        samples_other = []           # 有 restriction_reason 但 reason 非 terms/tos
        scanned = 0
        flagged = 0
        start = time.monotonic()

        async for msg in client.iter_messages(chat):
            scanned += 1
            if args.limit and scanned > args.limit:
                scanned -= 1
                break
            if scanned % 500 == 0:
                log(f"  已采样 {scanned} 条，命中 {flagged} 条"
                    f"（{time.monotonic()-start:.0f}s）")

            rr = getattr(msg, "restriction_reason", None) or []
            restricted_flag = bool(getattr(msg, "restricted", False))
            if not rr and not restricted_flag:
                continue
            flagged += 1

            has_text, has_media, mk = has_visible_content(msg)
            placeholder = is_placeholder_text(msg)
            tos = has_tos_reason(msg)

            reasons = set()
            for r in rr:
                plat = (getattr(r, "platform", "") or "")
                rsn = (getattr(r, "reason", "") or "")
                reason_pairs[(plat, rsn)] += 1
                reasons.add(rsn)
            if not rr and restricted_flag:
                reason_pairs[("<仅restricted标志>", "")] += 1
            for rsn in reasons:
                reason_only[rsn] += 1
                vis_by_reason.setdefault(rsn, Counter())[
                    ("占位文字" if placeholder else "真实正文",
                     "有媒体" if has_media else f"无媒体/{mk}")] += 1

            # 候选判据计数
            candidate["A_任意restriction_reason(当前过宽判据)"] += 1
            if tos:
                candidate["B_reason含terms/tos(ToS违规)"] += 1
            if placeholder:
                candidate["C_正文是占位提示文字"] += 1
            if tos and placeholder:
                candidate["D_B且C(双信号,最严格)"] += 1
            if tos or placeholder:
                candidate["E_B或C"] += 1
            if tos and not placeholder:
                b_not_c += 1
            if placeholder and not tos:
                c_not_b += 1

            sample = {
                "id": msg.id,
                "restriction_reason": [
                    {"platform": getattr(r, "platform", ""),
                     "reason": getattr(r, "reason", ""),
                     "text": getattr(r, "text", "")} for r in rr],
                "restricted_flag": restricted_flag,
                "reason_is_tos": tos,
                "text_is_placeholder": placeholder,
                "has_media": has_media,
                "media": mk,
                "text_preview": (getattr(msg, "message", "") or "")[:70],
                "is_forward": getattr(msg, "fwd_from", None) is not None,
            }
            if tos and len(samples_tos) < args.samples:
                samples_tos.append(sample)
            elif (not tos) and len(samples_other) < args.samples:
                samples_other.append(sample)

        # ---------------- 输出 ----------------
        log("\n" + "=" * 64)
        log(f"采样 {scanned} 条，命中 restriction_reason/restricted 的 {flagged} 条")

        log("\n【1. (platform, reason) 组合分布】—— 看看到底是哪些原因")
        for (plat, rsn), v in reason_pairs.most_common():
            log(f"  {v:7d}   platform={plat!r:12} reason={rsn!r}")

        log("\n【2. 各 reason 下 内容是否可见 的交叉统计】")
        for rsn, c in sorted(vis_by_reason.items(), key=lambda x: -sum(x[1].values())):
            log(f"  reason={rsn!r}:")
            for k, v in c.most_common():
                log(f"      {v:7d}   {k}")

        log("\n【3. 各候选删除判据会命中的数量】—— 对比哪个最贴近你的预期")
        for k, v in candidate.items():
            log(f"  {v:7d}   {k}")
        log(f"\n  交叉核对：reason=terms/tos 但正文非占位的 {b_not_c} 条；"
            f"正文是占位但 reason 非 terms/tos 的 {c_not_b} 条")
        log("  （这两个数都接近 0，说明 B 和 C 高度一致，判据可靠）")

        log("\n【4. reason=terms/tos 的样本（判定为看不到、应删）】")
        for s in samples_tos:
            log("  " + json.dumps(s, ensure_ascii=False))
        log("\n【5. 有 restriction_reason 但 reason 非 terms/tos 的样本（应保留）】")
        for s in samples_other:
            log("  " + json.dumps(s, ensure_ascii=False))

        Path("reports").mkdir(exist_ok=True)
        out = Path("reports") / f"diagnose_{chat_id}.json"
        out.write_text(json.dumps({
            "chat_id": chat_id, "chat_title": chat_title,
            "scanned": scanned, "flagged": flagged,
            "reason_pairs": {f"{p}|{r}": v for (p, r), v in reason_pairs.items()},
            "reason_only": dict(reason_only),
            "vis_by_reason": {r: {str(k): v for k, v in c.items()}
                              for r, c in vis_by_reason.items()},
            "candidate": candidate,
            "b_not_c": b_not_c, "c_not_b": c_not_b,
            "samples_tos": samples_tos,
            "samples_other": samples_other,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"\n完整明细已保存到：{out}")
        log("请把上面【1】【2】【3】三段，或直接把这个 json 文件内容发回。")


def main() -> None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="只读诊断：分析群里受限消息的真实构成")
    p.add_argument("chat", help="群组 ID / @username / t.me 链接")
    p.add_argument("--limit", type=int, default=3000, help="最多采样多少条（默认 3000）")
    p.add_argument("--samples", type=int, default=10, help="每类打印多少条样本明细（默认 10）")
    p.add_argument("--proxy", default=None, help="代理地址，同 cleaner.py")
    args = p.parse_args()
    asyncio.run(diagnose(args))


if __name__ == "__main__":
    main()
