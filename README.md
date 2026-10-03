# Telegram 群组无效消息批量清理工具

批量删除群组中**内容无法显示**的受限消息，适用于群管理员清理大量因原频道/群
被 Telegram 官方封禁、导致消息内容变成占位提示的情况。

## 识别范围（重要）

本工具**只删除"内容被 Telegram 抹除、无法显示"的 ToS 违规消息**，判定依据是
消息的 `restriction_reason` 中含有 **`reason='terms'`（或旧拼写 `'tos'`）**——
这正是客户端显示下面这句提示、且正文被替换成占位文字、媒体被清空的那类消息：

> "This message couldn't be displayed on your device because it violates the Telegram Terms of Service."

| restriction_reason | 客户端表现 | 内容能否看到 | 是否删除 |
|---|---|---|---|
| **`reason='terms'`**（platform=all） | 占位提示，媒体被清空 | ❌ 看不到 | ✅ **删除** |
| `reason='porn'`（platform=ios/android） | 仅移动端隐藏 | ✅ 桌面端能看到 | ❌ **保留** |
| `reason='sensitive'`（platform=ios/android） | 仅移动端隐藏 | ✅ 桌面端能看到 | ❌ **保留** |
| 无 restriction_reason（普通消息/普通转发） | 正常 | ✅ 能看到 | ❌ 保留 |

> **关键**：`porn` / `sensitive` 只是 Telegram 对敏感媒体在 **iOS/Android** 上的
> 平台级标记，你在桌面端照常能看到内容，因此**一律保留、绝不删除**。
> 只有 `reason='terms'`（内容真的被抹除、变成占位提示）才会进入删除列表。

扫描后脚本还会做**自检**并打印：命中消息中"正文为占位提示"和"仍带媒体"的数量。
正常情况下应当是「占位提示 = 全部、带媒体 = 0」；若不符会在 CSV 中标出，供你核对。

## 准备工作

1. **申请 API 凭据**（免费，约 2 分钟）：
   - 打开 <https://my.telegram.org> 并用你的账号登录
   - 进入 "API development tools"，创建一个应用
   - 记下 `api_id` 和 `api_hash`
2. **安装依赖**（需要 Python 3.9+）：
   ```bash
   pip install -r requirements.txt
   ```
3. 你的账号必须是目标群的**管理员**，且拥有**删除消息**权限。
4. 如果网络无法直连 Telegram，请准备好代理（见下方[配置代理](#配置代理socks5--http)）。

> 首次运行脚本时会要求输入 api_id / api_hash（保存到 `config.json`），
> 然后进行登录（手机号 + 验证码，如开启了两步验证还需密码）。
> 登录状态保存在 `cleaner.session`，之后无需重复登录。

## 配置代理（SOCKS5 / HTTP）

支持 `socks5`、`socks4`、`http` 三种代理（可带用户名/密码认证），三种配置方式（优先级从高到低）：

**方式一：命令行参数**

```bash
python cleaner.py scan -1001234567890 --proxy socks5://127.0.0.1:1080
python cleaner.py delete reports/scan_xxx.json --proxy socks5://127.0.0.1:1080

# HTTP 代理（带认证）
python cleaner.py scan -1001234567890 --proxy http://user:pass@127.0.0.1:8080

# SOCKS5 带认证
python cleaner.py scan -1001234567890 --proxy socks5://user:pass@127.0.0.1:1080

# 不写协议头时默认按 socks5 处理
python cleaner.py scan -1001234567890 --proxy 127.0.0.1:1080
```

**方式二：写入 config.json**（一次配置，长期生效）

```json
{
  "api_id": 123456,
  "api_hash": "xxxxxxxx",
  "proxy": {
    "proxy_type": "socks5",
    "addr": "127.0.0.1",
    "port": 1080,
    "username": null,
    "password": null,
    "rdns": true
  }
}
```

（也可以直接写字符串形式 `"proxy": "socks5://127.0.0.1:1080"`，脚本会自动解析）

**方式三：首次运行配置 api_id/api_hash 时，按提示输入代理地址**（会保存到 config.json）

> 注意：`scan` 和 `delete` 两个命令都要能连上 Telegram，如果代理只写在
> 命令行参数里，两个命令都需要带上 `--proxy`；写入 config.json 则一劳永逸。

## 使用方法（两步，先扫描后删除）

### 第一步：扫描

```bash
# 用群组 ID（推荐，可从群组链接或转发消息中获取）
python cleaner.py scan -1001234567890

# 也支持 @username 或 t.me 链接
python cleaner.py scan @your_group
python cleaner.py scan https://t.me/your_group

# 无法直连 Telegram 时，加上代理（或把代理写进 config.json，见"配置代理"一节）
python cleaner.py scan -1001234567890 --proxy socks5://127.0.0.1:1080
```

可选参数：

- `--proxy <地址>`：指定代理，支持 `socks5://`、`socks4://`、`http://`，
  可带认证（如 `socks5://user:pass@host:port`）；不写协议头默认按 socks5 处理。
  详见[配置代理](#配置代理socks5--http)
- `--limit 2000`：只扫描最新的 2000 条消息（**建议先用小数值测试一遍流程**）
- `--force`：管理员权限检查不通过时不再询问、强制继续

扫描完成后在 `reports/` 目录生成两个文件：

- `scan_<群ID>_<时间>.csv` —— 人工审查用，可用 Excel 打开
  （列：消息ID、日期、分类、发送者、`restriction_reason`、`text_is_placeholder`、
  `has_media`、`text_preview`）
- `scan_<群ID>_<时间>.json` —— 删除步骤的输入文件（含待删除 ID 列表）

> 核对小技巧：删除列表里的每一行都应满足 **`restriction_reason` 含 `terms`、
> `text_is_placeholder=True`、`has_media=False`**，且 `text_preview` 是
> "This channel can't be displayed..." 这类占位提示。若发现某行 `has_media=True`
> 或 `text_preview` 是真实正文，说明它内容其实可见，请从 JSON 的 `delete_ids`
> 中移除后再删（正常不会出现，脚本扫描结束时也会自检并提示）。

### 第二步：确认并删除

**先打开 CSV 报告核对**，确认列表中没有误判后执行：

```bash
python cleaner.py delete reports/scan_xxx.json

# delete 同样需要能连上 Telegram：代理若没写进 config.json，这里也要带上
python cleaner.py delete reports/scan_xxx.json --proxy socks5://127.0.0.1:1080
```

- 脚本会先显示删除摘要（群组、各分类数量、总数）
- 需要你手动输入 `yes` 才会真正开始删除（不可恢复！）
- 每批最多删除 100 条，批间默认间隔 1 秒防触发限速；
  遇到 FloodWait 会自动等待并重试
- 完成后在 `reports/` 生成删除结果日志（成功数、失败批次详情）

可选参数：

- `--proxy <地址>`：指定代理，格式同 scan 命令
- `--batch-delay 2`：调整批间隔秒数
- `--chat -1001234567890`：覆盖报告中的群组 ID（一般不需要）
- `--yes`：跳过交互确认（自动化场景，谨慎使用）
- `--force`：管理员权限检查不通过时不再询问、强制继续

## 安全说明

- `scan` 命令**只读不删**，可放心反复运行
- **只删除 `reason='terms'` 的 ToS 违规消息**（内容被抹除）；`porn`/`sensitive`
  等平台级敏感标记、普通转发、正常消息一律不动
- `delete` 只删除报告 JSON 中 `delete_ids` 列出的消息，且需要二次确认
- `config.json`、`cleaner.session` 包含你的凭据和登录状态，**切勿泄露或提交到 git**
  （已在 `.gitignore` 中排除）
- 删除操作不可恢复，请务必先审查 CSV 报告

## 常见问题

- **群组 ID 怎么写**：脚本会自动兼容 `-1001234567890`（标准形式）、
  `1234567890` / `-1234567890`（缺 `-100` 前缀的裸数字）等写法，
  按「原样 → 补全 -100 前缀 → 遍历对话框列表」的顺序解析；
  实在找不到时，请改用 `@username` 或 t.me 链接，或先在客户端里打开一次该群组。
- **首次运行已通过 `--proxy` 指定代理，还会再问一遍吗**：不会。
  命令行指定的代理会自动写入 `config.json`，后续 scan / delete 无需再带 `--proxy`。
- **连接一直失败 / "Could not connect to proxy"**：检查代理软件是否已开启、
  端口是否正确（本地代理常见端口：1080/7890/10809）。注意 Telegram 走的是
  TCP，代理软件需支持 SOCKS5 或 HTTP CONNECT 协议；仅支持"系统代理/PAC 模式"
  的工具可能不监听本地 SOCKS 端口，请切换为全局模式或开启"允许局域网连接"。
- **扫描很慢**：正常现象，Telegram 拉取历史消息有限速；大群可能需要几十分钟，
  进度会实时显示。走代理时速度还受代理带宽影响。
- **会不会误删能看到内容的消息？** 不会。只有 `restriction_reason` 含
  `reason='terms'`（内容被抹除、正文变占位提示）的消息才会进入删除列表；
  `porn`/`sensitive` 等平台级敏感标记（桌面端能看到内容）一律保留。
  删除前可打开 CSV，确认每行 `text_is_placeholder=True` 且 `has_media=False`。
- **不确定群里到底是哪些原因被标记？** 用只读诊断工具 `diagnose.py` 采样分析：
  ```bash
  python diagnose.py <群组ID> --limit 3000
  ```
  它会打印各 `(platform, reason)` 的分布、内容可见性交叉统计，以及几种候选判据
  各自命中的数量，帮助你确认删除范围（全程只读，不删任何消息）。
