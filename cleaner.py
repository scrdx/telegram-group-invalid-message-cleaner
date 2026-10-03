#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 群组无效消息批量清理工具
=================================

用于群组管理员批量删除群内"内容无法显示"的消息：
  restricted —— 被 Telegram 官方限制的消息，客户端显示
     "This message couldn't be displayed on your device because it
     violates the Telegram Terms of Service."，内容不可见。

注意：只处理"内容看不到"的受限消息。对于内容能正常看到、仅转发来源
频道/群消失的普通转发消息，一律保留、不删除。

两步使用（先扫描后删除，删除前需人工确认）：
  python cleaner.py scan   <群组ID或链接>     # 第一步：扫描并生成报告
  python cleaner.py delete <报告.json路径>    # 第二步：确认后批量删除

依赖：pip install -r requirements.txt
首次使用需要在 https://my.telegram.org 申请 api_id / api_hash。

网络无法直连 Telegram 时，可配置代理（支持 socks5 / socks4 / http）：
  python cleaner.py scan <群组> --proxy socks5://127.0.0.1:1080
  python cleaner.py scan <群组> --proxy http://user:pass@127.0.0.1:8080
也可以把代理写入 config.json 的 "proxy" 字段，之后无需重复输入。
"""

import argparse
import asyncio
import csv
import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.types import User

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_PATH = BASE_DIR / "cleaner.session"
REPORTS_DIR = BASE_DIR / "reports"

# 消息分类：只识别"内容不可见"的官方受限消息
CAT_RESTRICTED = "restricted"  # 违反 ToS，客户端显示占位文字，内容不可见

CATEGORY_DESC = {
    CAT_RESTRICTED: "被 Telegram 官方限制、内容无法显示的消息（违反 ToS）",
}

DELETE_BATCH_SIZE = 100  # Telegram 单次删除上限


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def log(msg: str = "") -> None:
    print(msg, flush=True)


def now_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_config(cli_proxy=None) -> dict:
    """
    读取 api_id / api_hash，不存在则交互式询问并保存到 config.json。
    cli_proxy: 命令行 --proxy 传入的代理地址；首次配置时若已指定，
    则直接使用它（并保存进 config.json），不再重复询问。
    """
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("api_id") and cfg.get("api_hash"):
            cfg["api_id"] = int(cfg["api_id"])
            return cfg
        log("config.json 缺少 api_id/api_hash，将重新询问。")

    log("=" * 60)
    log("首次使用需要 Telegram API 凭据（免费）：")
    log("  1. 打开 https://my.telegram.org 并用你的账号登录")
    log("  2. 进入 API development tools，创建一个应用")
    log("  3. 复制 api_id 和 api_hash 填入下方")
    log("=" * 60)
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    cfg = {"api_id": api_id, "api_hash": api_hash}

    # 命令行已指定 --proxy 时直接使用，不再重复询问
    if cli_proxy:
        try:
            cfg["proxy"] = normalize_proxy(cli_proxy)
        except ValueError as e:
            log(f"错误：--proxy 参数无效：{e}")
            sys.exit(1)
        log(f"已使用命令行指定的代理：{proxy_display(cfg['proxy'])}")
    else:
        proxy = input("代理地址（可选，直接回车跳过；"
                      "如 socks5://127.0.0.1:1080 或 http://127.0.0.1:8080）: ").strip()
        if proxy:
            try:
                cfg["proxy"] = normalize_proxy(proxy)
            except ValueError as e:
                log(f"错误：代理地址无效：{e}")
                sys.exit(1)

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    log(f"凭据已保存到 {CONFIG_PATH.name}（请勿泄露该文件）")
    return cfg


# Telethon 1.45 的 _parse_proxy 仅支持这三种（不支持 https）
VALID_PROXY_SCHEMES = ("socks5", "socks4", "http")


def normalize_proxy(proxy):
    """
    校验并规范化代理配置，返回 Telethon 需要的 dict 形式：
      {'proxy_type': 'socks5', 'addr': ..., 'port': ...,
       'username': ..., 'password': ..., 'rdns': True}
    输入可以是 URL 字符串（socks5://user:pass@host:port）或已解析的 dict。
    """
    from urllib.parse import urlparse, unquote

    if isinstance(proxy, dict):  # config.json 中已是 dict 形式
        if not all(k in proxy for k in ("proxy_type", "addr", "port")):
            raise ValueError(f"代理配置缺少必填字段 proxy_type/addr/port：{proxy}")
        return proxy

    proxy = str(proxy).strip().strip('"').strip("'")
    if not proxy:
        return {}
    if "://" not in proxy:
        proxy = "socks5://" + proxy  # 未写协议时默认按 socks5 处理
    parsed = urlparse(proxy)
    scheme = parsed.scheme.lower()
    if scheme not in VALID_PROXY_SCHEMES:
        raise ValueError(
            f"不支持的代理类型 '{scheme or '(空)'}'，仅支持: "
            f"{' / '.join(VALID_PROXY_SCHEMES)}")
    if not parsed.hostname:
        raise ValueError(f"代理地址缺少主机名：{proxy}")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"代理端口无效：{proxy}")
    if not port:
        raise ValueError(f"代理地址缺少端口：{proxy}")
    return {
        "proxy_type": scheme,
        "addr": parsed.hostname,
        "port": port,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "rdns": True,  # 远程解析域名（翻墙场景必需）
    }


def proxy_display(proxy: dict) -> str:
    """把代理 dict 转成可读字符串（隐去密码）。"""
    if not proxy:
        return ""
    auth = ""
    if proxy.get("username"):
        auth = proxy["username"] + (":***@" if proxy.get("password") else "@")
    return f"{proxy.get('proxy_type')}://{auth}{proxy.get('addr')}:{proxy.get('port')}"


def resolve_proxy(args, cfg: dict):
    """代理优先级：命令行 --proxy > config.json 中的 proxy 字段。"""
    raw = getattr(args, "proxy", None) or cfg.get("proxy") or ""
    if not raw:
        return None
    try:
        proxy = normalize_proxy(raw)
    except ValueError as e:
        log(f"错误：{e}")
        sys.exit(1)
    if not proxy:
        return None
    log(f"使用代理：{proxy_display(proxy)}")
    return proxy


async def ensure_login(client: TelegramClient) -> None:
    """如会话未授权，引导完成登录（验证码 + 可能的两步验证密码）。"""
    if await client.is_user_authorized():
        return
    log("当前会话未登录，开始登录流程……")
    phone = input("手机号（含国家码，如 +8613800000000）: ").strip()
    sent = await client.send_code_request(phone)
    code = input(f"验证码（已发送到 {phone}）: ").strip()
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except errors.SessionPasswordNeededError:
        pwd = getpass.getpass("两步验证密码: ")
        await client.sign_in(password=pwd)
    log("登录成功。")


async def resolve_chat(client: TelegramClient, chat_arg: str):
    """
    支持 群组ID / @username / t.me 链接 三种形式。

    数字 ID 自动兼容常见写法：
      -1001234567890  超级群/频道标准写法
      1234567890      裸数字（部分工具/机器人显示时去掉了 -100 前缀）
      -1234567890     负号但缺 100 前缀
    会按「原样 → 补全 -100 前缀」的顺序尝试，仍失败则遍历对话框列表查找。
    """
    s = str(chat_arg).strip()
    if not s.lstrip("-").isdigit():
        # @username 或 t.me 链接
        try:
            return await client.get_entity(s)
        except Exception as e:
            raise ValueError(
                f"无法解析 '{s}'：{type(e).__name__}: {e}\n"
                f"请检查用户名/链接是否正确，或改用群组的数字 ID") from e

    raw = int(s)
    candidates = [raw]
    abs_str = str(abs(raw))
    # 负数且绝对值以 100 开头 => 已是标准 -100xxx 形式，无需补前缀
    already_marked = raw < 0 and abs_str.startswith("100")
    if not already_marked:
        candidates.append(int("-100" + abs_str))

    entity_errors = (ValueError, TypeError,
                     errors.ChatIdInvalidError, errors.ChannelInvalidError,
                     errors.PeerIdInvalidError)
    last_err = None
    for cid in candidates:
        try:
            return await client.get_entity(cid)
        except entity_errors as e:
            last_err = e

    # get_entity 均失败（通常是本地无该实体缓存）：遍历对话框列表查找
    wanted = set(candidates)
    async for d in client.iter_dialogs():
        if d.id in wanted:
            return d.entity

    raise ValueError(
        f"找不到群组 {chat_arg}（已尝试 ID: {candidates}）。\n"
        f"请确认：1) 账号已加入该群组；2) ID 抄写正确；"
        f"3) 也可改用 @username 或 t.me 链接。\n"
        f"最后一次错误：{type(last_err).__name__ if last_err else '-'}: {last_err}")


async def check_delete_permission(client: TelegramClient, chat, strict: bool) -> None:
    """
    检查当前账号在该群是否拥有【删除消息】权限。

    说明：必须调用 get_permissions(chat, me) 并传入当前用户，才能拿到本人权限；
    不传 user 时返回的是群组"默认受限权限"，与本人身份无关。
    群主（creator）拥有全部权限，单独放行。
    """
    try:
        me = await client.get_me()
        perms = await client.get_permissions(chat, me)
        if perms is None:
            raise ValueError("get_permissions 返回空")

        if perms.is_creator:
            return  # 群主拥有全部权限
        if perms.is_admin and perms.delete_messages:
            return  # 管理员且具备删除消息权限
        if perms.is_admin:
            log("[警告] 当前账号是管理员，但缺少【删除消息】权限，删除操作可能失败。")
        else:
            log("[警告] 当前账号不是该群的管理员，删除他人消息的操作可能会失败。")
        if strict:
            if input("仍要继续吗？(y/N): ").strip().lower() != "y":
                sys.exit(1)
    except errors.UserNotParticipantError:
        log("[警告] 当前账号似乎不是该群成员。")
        if strict:
            if input("仍要继续吗？(y/N): ").strip().lower() != "y":
                sys.exit(1)
    except Exception as e:  # 检查失败不阻塞主流程
        log(f"（无法验证删除权限：{type(e).__name__}: {e}；将照常继续）")


def sender_display(msg) -> str:
    s = msg.sender
    if s is None:
        return ""
    if isinstance(s, User):
        name = " ".join(x for x in (s.first_name, s.last_name) if x)
        return name or (s.username or "")
    return getattr(s, "title", "") or ""


def text_preview(msg, limit: int = 80) -> str:
    try:
        t = (msg.raw_text or "").replace("\n", " ")
    except Exception:
        t = ""
    return t[:limit]


# ---------------------------------------------------------------------------
# scan：扫描群组，识别"内容不可见"的 ToS 受限消息，生成报告
# ---------------------------------------------------------------------------

# restriction_reason 中代表"内容被 Telegram 抹除、无法显示"的 reason 取值。
# 实测结论（重要）：
#   reason='terms'（platform=all）—— 正文被替换成占位提示、媒体被清空 => 真正看不到 => 删
#   reason='porn' / 'sensitive' 等 —— 仅是 iOS/Android 平台级敏感标记，
#                                     桌面端内容照常可见 => 必须保留，绝不能删
TOS_REASONS = {"terms", "tos"}

# Telegram 对 ToS 违规消息替换的占位正文特征（用于报告中标注，非硬性条件）
PLACEHOLDER_MARKERS = (
    "can’t be displayed", "can't be displayed",
    "couldn’t be displayed", "couldn't be displayed",
    "violated telegram", "violates the telegram", "terms of service",
)


def tos_entries(msg):
    """返回 restriction_reason 中 reason 属于 ToS 违规的条目列表。"""
    rr = getattr(msg, "restriction_reason", None) or []
    return [r for r in rr
            if (getattr(r, "reason", "") or "").lower() in TOS_REASONS]


def is_placeholder_text(msg) -> bool:
    t = (getattr(msg, "message", "") or "").lower()
    return any(m in t for m in PLACEHOLDER_MARKERS)


def build_row(msg, category: str, restriction_reason: str = "",
              text_is_placeholder: bool = False) -> dict:
    date = getattr(msg, "date", None)
    media = getattr(msg, "media", None)
    return {
        "message_id": msg.id,
        "date": date.isoformat() if date else "",
        "category": category,
        "sender_id": msg.sender_id or "",
        "sender_name": sender_display(msg),
        "restriction_reason": restriction_reason,
        # 真正看不到的消息：正文为占位提示、无媒体。以下两列供人工复核：
        "text_is_placeholder": text_is_placeholder,
        "has_media": media is not None and type(media).__name__ != "MessageMediaEmpty",
        "text_preview": text_preview(msg),
    }


def classify_message(msg):
    """
    判断一条消息是否为"内容不可见"的 ToS 受限消息。是则返回报告行，否则 None。

    判据（经真实数据验证）：restriction_reason 中存在 reason='terms'/'tos' 的条目。
    这类消息正文被 Telegram 替换为占位提示、媒体被清空，客户端显示
    "This message couldn't be displayed ... violates the Telegram Terms of Service."

    而 reason='porn'/'sensitive' 等只是平台级敏感标记，桌面端内容仍可见，
    一律保留、不删除。内容能看到的普通转发消息同样不会被匹配。
    """
    # 服务消息（入群/退群/改名等）直接跳过
    if getattr(msg, "action", None) is not None:
        return None

    tos = tos_entries(msg)
    if not tos:
        return None  # 无 ToS 受限（含 porn/sensitive 等平台标记）=> 保留

    rr = getattr(msg, "restriction_reason", None) or []
    reasons = ";".join(f"{r.platform}:{r.reason}" for r in rr) or "terms"
    return build_row(msg, CAT_RESTRICTED, restriction_reason=reasons,
                     text_is_placeholder=is_placeholder_text(msg))


async def cmd_scan(args) -> None:
    cfg = load_config(getattr(args, "proxy", None))
    proxy = resolve_proxy(args, cfg)
    async with TelegramClient(str(SESSION_PATH), cfg["api_id"],
                              cfg["api_hash"], proxy=proxy) as client:
        await ensure_login(client)
        chat = await resolve_chat(client, args.chat)
        chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) \
            or str(args.chat)
        log(f"目标群组：{chat_title}（id={chat.id}）")
        await check_delete_permission(client, chat, strict=not args.force)

        log("\n开始扫描全部历史消息（大群可能需要较长时间，请耐心等待）……")
        log("识别目标：内容无法显示的官方受限消息（内容可见的普通转发一律保留）")
        rows: list[dict] = []
        scanned = 0
        start = time.monotonic()

        async for msg in client.iter_messages(chat):
            scanned += 1
            if args.limit and scanned > args.limit:
                scanned -= 1
                break
            if scanned % 200 == 0:
                elapsed = time.monotonic() - start
                log(f"  已扫描 {scanned} 条，发现受限消息 {len(rows)} 条"
                    f"（{elapsed:.0f}s）")
            row = classify_message(msg)
            if row:
                rows.append(row)

        # 统计
        stats: dict = {}
        for r in rows:
            stats[r["category"]] = stats.get(r["category"], 0) + 1

        log("\n" + "=" * 60)
        log("扫描完成")
        log(f"  共扫描消息：{scanned} 条")
        for cat, n in sorted(stats.items()):
            log(f"  [{cat}] {n} 条 —— {CATEGORY_DESC.get(cat, '')}")
        if not rows:
            log("  未发现内容不可见的 ToS 受限消息。")
            return

        # 自检：命中的消息应当"正文是占位提示、无媒体"，若出现异常需人工留意
        n_placeholder = sum(1 for r in rows if r.get("text_is_placeholder"))
        n_with_media = sum(1 for r in rows if r.get("has_media"))
        n_with_text = sum(1 for r in rows if (r.get("text_preview") or "").strip())
        log("\n  删除列表自检（正常情况：占位文字=全部，有媒体=0）：")
        log(f"    正文为占位提示：{n_placeholder}/{len(rows)}")
        log(f"    仍带媒体文件　：{n_with_media}/{len(rows)}")
        log(f"    text_preview 非空：{n_with_text}/{len(rows)}（占位提示也算非空，属正常）")
        if n_with_media > 0 or n_placeholder < len(rows):
            log("    [注意] 存在非典型样本，请在 CSV 中核对 text_is_placeholder/"
                "has_media 两列后再删除。")

        # 删除列表：仅 ToS 受限消息（reason=terms/tos，内容不可见）
        delete_ids = [r["message_id"] for r in rows
                      if r["category"] == CAT_RESTRICTED]

        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = now_str()
        base = REPORTS_DIR / f"scan_{chat.id}_{stamp}"
        json_path = base.with_suffix(".json")
        csv_path = base.with_suffix(".csv")

        report = {
            "chat_id": chat.id,
            "chat_title": chat_title,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "total_scanned": scanned,
            "stats": stats,
            "delete_ids": delete_ids,
            "messages": rows,
        }
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        # CSV 用 utf-8-sig，方便 Excel 直接打开
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        log("=" * 60)
        log(f"报告已生成：")
        log(f"  人工审查用（Excel 可打开）：{csv_path}")
        log(f"  删除步骤输入文件：        {json_path}")
        log(f"计划删除 {len(delete_ids)} 条受限消息（内容不可见）")
        log("提示：删除列表每行都应满足 restriction_reason 含 terms、")
        log("     text_is_placeholder=True、has_media=False；其中 text_preview 是")
        log("     占位提示文字（如 This channel can't be displayed ...）属正常。")
        log("     若某行 has_media=True 或 text_is_placeholder=False，说明其内容")
        log("     可能仍可见，请核对后从 JSON 的 delete_ids 中移除该行再删。")
        log("\n请先打开 CSV 报告核对，确认无误后执行：")
        log(f"  python cleaner.py delete \"{json_path}\"")


# ---------------------------------------------------------------------------
# delete：读取报告，二次确认后批量删除
# ---------------------------------------------------------------------------

async def cmd_delete(args) -> None:
    report_path = Path(args.report)
    if not report_path.exists():
        log(f"错误：报告文件不存在：{report_path}")
        sys.exit(1)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ids: list[int] = [int(i) for i in report.get("delete_ids", [])]
    if not ids:
        log("报告中的删除列表为空，没有需要删除的消息。")
        return

    log("=" * 60)
    log("删除任务摘要（来自扫描报告）")
    log(f"  群组：{report.get('chat_title')}（id={report.get('chat_id')}）")
    log(f"  扫描时间：{report.get('scanned_at')}")
    for cat, n in sorted(report.get("stats", {}).items()):
        log(f"  [{cat}] {n} 条")
    log(f"  本次将删除：{len(ids)} 条消息")
    log("=" * 60)
    log("警告：删除操作不可恢复！")

    if not args.yes:
        answer = input('确认删除请输入 yes（其他任意输入取消）: ').strip().lower()
        if answer != "yes":
            log("已取消，未删除任何消息。")
            return

    cfg = load_config(getattr(args, "proxy", None))
    proxy = resolve_proxy(args, cfg)
    async with TelegramClient(str(SESSION_PATH), cfg["api_id"],
                              cfg["api_hash"], proxy=proxy) as client:
        await ensure_login(client)
        chat_target = args.chat if args.chat else report["chat_id"]
        chat = await resolve_chat(client, str(chat_target))
        await check_delete_permission(client, chat, strict=not args.force)

        deleted = 0
        failed: list[dict] = []
        batches = [ids[i:i + DELETE_BATCH_SIZE]
                   for i in range(0, len(ids), DELETE_BATCH_SIZE)]
        log(f"\n开始删除，共 {len(batches)} 批（每批最多 {DELETE_BATCH_SIZE} 条）……")

        for bi, batch in enumerate(batches, 1):
            ok = False
            for attempt in range(5):
                try:
                    await client.delete_messages(chat, batch, revoke=True)
                    ok = True
                    break
                except errors.FloodWaitError as e:
                    wait = e.seconds + 1
                    log(f"  第 {bi} 批触发 FloodWait，等待 {wait} 秒后重试"
                        f"（第 {attempt + 1} 次）……")
                    await asyncio.sleep(wait)
                except Exception as e:
                    log(f"  第 {bi} 批删除失败：{type(e).__name__}: {e}")
                    failed.append({"batch": bi, "ids": batch,
                                   "error": f"{type(e).__name__}: {e}"})
                    break
            if ok:
                deleted += len(batch)
            log(f"  进度：{bi}/{len(batches)} 批，已成功删除 {deleted} 条")
            if bi < len(batches):
                await asyncio.sleep(args.batch_delay)

        # 写入删除结果日志
        REPORTS_DIR.mkdir(exist_ok=True)
        result_path = REPORTS_DIR / f"deleted_{report.get('chat_id')}_{now_str()}.json"
        result_path.write_text(json.dumps({
            "source_report": str(report_path),
            "chat_id": report.get("chat_id"),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "requested": len(ids),
            "deleted": deleted,
            "failed_batches": failed,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        log("\n" + "=" * 60)
        log(f"删除完成：成功 {deleted}/{len(ids)} 条")
        if failed:
            log(f"有 {len(failed)} 批失败，详见日志：{result_path}")
        else:
            log(f"结果日志：{result_path}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Telegram 群组无效消息批量清理工具（管理员专用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python cleaner.py scan -1001234567890\n"
               "  python cleaner.py scan @mygroup --limit 2000\n"
               "  python cleaner.py delete reports/scan_xxx.json\n")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="扫描群组，识别内容无法显示的受限消息并生成报告（不删除任何东西）")
    sp.add_argument("chat", help="群组 ID（如 -1001234567890）、@username 或 t.me 链接")
    sp.add_argument("--limit", type=int, default=None,
                    help="最多扫描多少条消息（默认全部，可先用小值测试）")
    sp.add_argument("--force", action="store_true",
                    help="跳过管理员权限检查失败时的询问，强制继续")
    sp.add_argument("--proxy", default=None,
                    help="代理地址，支持 socks5/socks4/http，如 "
                         "socks5://127.0.0.1:1080 或 http://user:pass@host:port"
                         "（也可写入 config.json 的 proxy 字段）")

    dp = sub.add_parser("delete", help="读取扫描报告，确认后批量删除")
    dp.add_argument("report", help="scan 生成的 JSON 报告路径")
    dp.add_argument("--chat", default=None,
                    help="覆盖报告中的群组 ID（一般不需要）")
    dp.add_argument("--yes", action="store_true",
                    help="跳过交互式确认（谨慎使用！）")
    dp.add_argument("--batch-delay", type=float, default=1.0,
                    help="每批删除之间的间隔秒数（默认 1.0，防触发限速）")
    dp.add_argument("--force", action="store_true",
                    help="跳过管理员权限检查失败时的询问，强制继续")
    dp.add_argument("--proxy", default=None,
                    help="代理地址，支持 socks5/socks4/http，如 "
                         "socks5://127.0.0.1:1080（也可写入 config.json 的 proxy 字段）")
    return p


def main() -> None:
    # Windows 输出编码兼容处理：
    #  - 真实控制台(cmd/PowerShell)：保留原生编码，仅防止特殊字符崩溃
    #  - 管道/重定向(Git Bash 等)：强制 UTF-8
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = build_parser().parse_args()
    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "delete":
        asyncio.run(cmd_delete(args))


if __name__ == "__main__":
    main()
