# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose CLI tool for a Telegram **group admin** to bulk-delete messages whose
content has been erased by Telegram for Terms-of-Service violations (the client shows
"This message couldn't be displayed ... violates the Telegram Terms of Service.").
Built on Telethon (MTProto user account, not a bot — bots can't reliably see/delete these).
User-facing docs are in `README.md` (Chinese).

## Commands

```bash
pip install -r requirements.txt          # telethon + python-socks[asyncio]

python cleaner.py scan   <chat> [--limit N] [--proxy URL] [--force]   # read-only, writes a report
python cleaner.py delete <report.json> [--yes] [--batch-delay S] [--proxy URL]
python diagnose.py       <chat> [--limit N] [--samples N] [--proxy URL]  # read-only analysis
```

`<chat>` accepts a numeric ID, `@username`, or `t.me` link. IDs may be given bare
(`1578502054`) or marked (`-1001578502054`); `resolve_chat` normalizes both.

There is **no formal test suite / linter / build step**. During development, verification
was done with ad-hoc inline Python (`python - <<'EOF' ... EOF`) that stubs `TelegramClient`
and feeds fake message objects to `classify_message`. When running such scripts under Git
Bash on Windows, prefix with `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` or Chinese/unicode
output crashes on the GBK pipe. Syntax-check with `python -m py_compile cleaner.py`.

## Architecture

### Two-phase workflow with a report contract

`scan` (read-only) → writes two files to `reports/` → `delete` consumes the JSON.

- `reports/scan_<chatid>_<ts>.json` — machine input for delete. The **`delete_ids` array
  is the sole authority** for what gets deleted; `delete` never re-derives it. `messages[]`
  carries per-message detail (including `text_is_placeholder`, `has_media`) for review.
- `reports/scan_<chatid>_<ts>.csv` — same rows, `utf-8-sig` for Excel, human review only.
- `reports/deleted_<chatid>_<ts>.json` — post-delete result log (requested/deleted/failed batches).

`delete` batches IDs ≤100 per `delete_messages` call, retries on `FloodWaitError`
(sleeping `e.seconds`), and requires typing `yes` unless `--yes`. Deletion is irreversible.
`scan` prints a self-check (all rows should be placeholder-text + no-media); treat a
non-zero "has_media" count as a red flag before deleting.

### THE core detection rule — do not regress this

`classify_message` flags a message **only if `restriction_reason` contains an entry whose
`reason` is in `TOS_REASONS = {"terms", "tos"}`.** This is the crux of the whole tool and
was gotten wrong twice before being pinned down against real data:

- `reason='terms'` (platform=`all`) → content genuinely erased, body replaced with the
  placeholder, media stripped → **DELETE**.
- `reason='porn'` / `reason='sensitive'` (platform=`ios`/`android`) → platform-level
  sensitive markers; content is **still fully visible on desktop** → **MUST KEEP**.
  Flagging on "any restriction_reason" (the naive approach) over-matched ~66% of a real
  73k-message group vs. the correct ~8%, i.e. it would have mass-deleted visible media.

The legacy boolean `restricted` field is **not** used: it no longer exists on Telethon
1.45's `Message` type. `restriction_reason` is the only signal. `PLACEHOLDER_MARKERS`
matching is recorded into the report for cross-checking but is **not** part of the
delete decision (placeholder text can be localized; `reason` is language-independent).

When onboarding a new group with surprising counts, run `diagnose.py` first: it dumps the
`(platform, reason)` distribution, a reason × content-visibility cross-tab, and how many
messages each candidate rule would match — that's how the `terms`-only rule was derived.

### Telethon 1.45 gotchas (each caused a real bug here)

- **Proxy must be a dict**, not a URL string. Passing `"socks5://host:port"` raises
  `TypeError: Proxy of unknown format` at connect time. `normalize_proxy()` parses the
  user-facing URL into `{proxy_type, addr, port, username, password, rdns}`. Only
  `socks5`/`socks4`/`http` are supported (`https` is not, by `_parse_proxy`). Proxy source
  precedence: `--proxy` > `config.json`.
- **`get_permissions(chat)` without a user returns the chat's `default_banned_rights`, NOT
  the caller's permissions.** Must call `get_permissions(chat, await client.get_me())`.
- **The group creator's `admin_rights` is `None`** (owners have implicit full rights) and
  `ChannelFull` has no `admin_rights` field at all. `check_delete_permission` therefore
  keys off `perms.is_creator` / `perms.is_admin` / `perms.delete_messages`, never off a
  `full_chat.admin_rights` lookup (that always read `None` and warned even for the owner).

### Runtime state (all gitignored — never commit)

`config.json` (`api_id`, `api_hash`, optional `proxy`), `cleaner.session` (login state),
and `reports/` are runtime artifacts living in the repo root. `load_config()` interactively
creates `config.json` on first run; a `--proxy` given then is persisted into it so later
`scan`/`delete` calls don't need to repeat it.

### diagnose.py

Read-only sibling tool. It **imports connection helpers from `cleaner.py`**
(`load_config`, `resolve_proxy`, `ensure_login`, `resolve_chat`, `log`, `TelegramClient`,
`SESSION_PATH`) rather than duplicating them — keep those importable/stable.
