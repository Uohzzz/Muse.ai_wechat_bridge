---
name: "wechat_ilink_bridge"
title: "微信 iLink Bot 桥"
description: "把 AI 助手接入用户微信：走微信官方 iLink / Clawbot Bot 通道（ilinkai.weixin.qq.com），扫码登录后实现一对一私聊的收发、自动回复与健康看门狗。包含 bridge.py（仅 Python 标准库 + qrcode）、inbox/outbox 文件队列、5 秒自动回复 hook 与纯脚本看门狗 hook（健康时零唤醒、零 token）的完整部署流程。"
---

# 微信 iLink Bot 桥

通过微信**官方** iLink / Clawbot Bot 通道，把助手接入用户本人的微信，实现一对一私聊：用户在微信里给 Bot 发消息，助手能收到并回复。协议依据腾讯 openclaw-weixin 仓库的 `docs/protocol_zh_CN.md`。

## 架构

- `bridge.py serve` 常驻进程：用 `getupdates` 长轮询收消息 → 写入 `inbox/`（JSON 文件队列）；把 `outbox/pending/` 里的回复用 `sendmessage` 发出 → 移到 `outbox/sent/`。
- 自动回复 hook（每 5 秒轮询 `inbox/`）：发现未处理消息就唤醒一个 worker 按助手口吻生成回复、写入 `outbox/pending/`，serve 负责真正发送。
- 看门狗 hook（纯脚本，每 5 分钟）：脚本直接检查 serve 存活并自动拉起、处理登录失效标记，健康时 silent 不唤醒任何 agent（零 token）；只有拉起成功/失败或登录失效时才唤醒通知 worker 给用户发一条消息（登录失效只通知一次）。收件箱轮询发现新消息时顺带刷新 `state/last_activity.json`（用户最后活跃时间），供闲置判断使用。
- 所有状态都在安装目录的 `state/` 下，其中 `session.json` 是登录凭据。

## 前置条件

- Python 3；依赖：`pip install qrcode pillow`（系统若报 PEP 668 externally-managed，加 `--break-system-packages`）。
- 用户本人微信在手边，能扫码并在手机上确认登录。二维码有效期只有约 2 分钟。
- 仅支持一对一私聊；群消息一律忽略。

## 部署步骤

安装目录默认 `~/workspace/wechat-bridge/`（下文记作 `$BRIDGE`），可按用户意愿更改（hook 脚本与 cron 里的路径要同步改）。

### 1. 落地文件

```bash
mkdir -p $BRIDGE/state $BRIDGE/inbox $BRIDGE/outbox/pending $BRIDGE/outbox/sent
cp scripts/bridge.py $BRIDGE/bridge.py
cp templates/gitignore $BRIDGE/.gitignore   # 忽略 state/，凭据绝不进仓库
cd $BRIDGE && python3 bridge.py status      # 应显示“未登录”
```

### 2. 扫码登录（时效敏感，全程盯着这一步）

1. **先让用户拿起手机、打开微信扫一扫，并明确说“好了”，再运行 login**，否则二维码大概率在用户看到之前就过期。
2. `python3 bridge.py login` 生成二维码图片（`~/workspace/your_files/wechat-login-qr.png`），**立刻把图片作为附件发到对话里让用户看到，并请他确认能看到图**；看不到就换方式重发。紧接着在后台启动 `python3 bridge.py wait-login`（nohup，日志 `state/wait-login.log`）。
3. wait-login 成功后 `state/session.json` 落盘（原子写入），旧的 `NEEDS_RELOGIN`、`notified_relogin` 标记被自动清除。
4. 若二维码过期：杀掉旧 wait-login（注意 pkill 模式别匹配到自己的 shell，用 `bridge[.]py` 这类写法），重新 login 取码，并把新图换个文件名再发，避免客户端缓存旧图。

### 3. 启动 serve

```bash
cd $BRIDGE && nohup python3 bridge.py serve > state/serve.out 2>&1 &
python3 bridge.py status   # serve: 运行中
```

### 4. 收发链路验证（顺序不能反）

官方限制：**Bot 不能主动发起对话，必须用户先发一条消息**，拿到那条消息的 `context_token` 才能回复。

1. 让用户在微信里找到 Bot 发一条消息（如“你好”）。
2. 确认 `$BRIDGE/inbox/` 出现对应 JSON、`state/last_context.json` 已缓存 context_token。
3. `python3 bridge.py send --text "<一条友好的测试回复>"`，请用户确认在微信里收到。双向都通才算接好。

### 5. 配置自动回复 hook

1. 把 `scripts/wechat-inbox-watch.sh` 放到 `~/hooks/scripts/`（脚本比较 `inbox/` 与 `state/processed.json`，有未处理消息才 wake）。
2. **启用前先把当时 inbox 里已有的消息 ID 全部写入 `state/processed.json`（JSON 数组）**，避免重复回复手动测试过的消息。
3. 用 hook 工具创建 hook：轮询间隔 5 秒，worker 指令用 `templates/hook-prompt.md` 的内容。先 dry-run（预期 silent 或只报已知消息），再 enable。

### 6. 配置看门狗 hook（纯脚本版，不要用 cron）

1. 把 `scripts/wechat-bridge-watchdog.sh` 放到 `~/hooks/scripts/`（脚本自己完成 serve 存活检查、拉起、NEEDS_RELOGIN 标记处理与 watchdog.log 记录；目录可用环境变量 `WECHAT_BRIDGE_DIR` 覆盖，便于沙箱测试）。
2. 用 hook 工具创建 hook：轮询间隔 300 秒，worker 指令用 `templates/watchdog-hook-prompt.md` 的内容。先 dry-run（健康时预期 silent 并报出 serve pid），再 enable。
3. 上线前建议在沙箱目录用 stub 版 bridge.py 实测两条故障路径：serve 已挂 → 脚本拉起并 wake（kind=restarted）；存在 NEEDS_RELOGIN 且未通知 → wake（kind=relogin）并生成 notified_relogin 标记，第二次运行应 silent。
4. 不要再建 cron 版看门狗——cron 每跑一次都要唤醒一个 agent，全天 288 次，是纯浪费；hook 脚本健康时零 token。

## 协议关键细节（踩过的坑，照做）

- **连接层重试**：所有 API 调用遇到 RemoteDisconnected、超时等自动重试 3 次，退避 1s/2s——微信服务端会偶发直接断连接。`bridge.py` 的 `http_json` 已实现。
- **取二维码**：`POST /ilink/bot/get_bot_qrcode?bot_type=3`，请求头带 `AuthorizationType: ilink_bot_token`、随机 `X-WECHAT-UIN`（随机 uint32 → 十进制字符串 → base64）、`iLink-App-Id: bot`、`iLink-App-ClientVersion: 132102`；请求体 `{"local_token_list": []}`；**不带 Authorization 头、不带 base_info**。返回的 `qrcode_img_content` 是个 HTTPS 链接，把这个链接编码成二维码图片给用户扫。
- **轮询扫码状态**：`GET /ilink/bot/get_qrcode_status?qrcode=...` 只带 `iLink-App-Id` 与 `iLink-App-ClientVersion` 两个头，长轮询超时 35 秒。状态处理：`wait`/`scaned` 继续等；`confirmed` 保存凭据；`expired` 重新取码；`need_verifycode` 需用户提供手机上的验证码（用 `--verify-code` 续轮询）；`verify_code_blocked` 重新取码；`scaned_but_redirect` 切到返回的 `redirect_host` 继续轮询；`binded_redirect` 视为已绑定。请求失败按 `wait` 处理、继续等。
- **收消息**：`POST /ilink/bot/getupdates`，带 `Authorization: Bearer <bot_token>`、`get_updates_buf` 游标（要持久化，重启后续传）和 `base_info`，超时 45 秒。只处理 `ilink_user_id` 本人发来的消息；`group_id` 非空的群消息忽略；语音消息带转写文本时按文本处理，纯图片/视频/文件写入 inbox 并标记类型。
- **入站媒体下载**：图片/视频/文件消息带 CDN 媒体引用（`media{encrypt_query_param, aes_key, full_url}`，可能挂在 item 或子对象上，要多处找）。下载优先用 `full_url`，否则拼 `<CDN 基址>/download?encrypted_query_param=<URL 编码>`（默认基址 `https://novac2c.cdn.weixin.qq.com/c2c`）。解密为 AES-128-ECB + PKCS#7：密钥优先级 `image_item.aeskey`（32 位十六进制字符串）> `media.aes_key`（base64，解开后或为 16 字节原始密钥、或为 32 位十六进制字符串的 ASCII）；两者都没有则下载内容即明文。Python 标准库没有 AES，`bridge.py` 借系统 `openssl enc -d -aes-128-ecb -K <hex>` 解密（需系统有 openssl）。下载成功落 `inbox/media/<message_id>-<序号>.<按魔数判定的扩展名>`，路径记入 inbox JSON 的 `media_files`；失败记 `media_errors`，消息本体照常入 inbox。下载前先把媒体引用存一份到 `state/media_refs/<message_id>.json`（成功后删除）——CDN 偶发抽风（中途断流、连接被关）时可以拿存底引用重试，不必让用户重发；下载走 Range 断点续传，不要每次从头拉。有了 `media_files`，自动回复 worker 对纯图片回「收到，我看看，马上回你。」并把路径转主对话看图后正式回复；没有（下载失败）才回「这条我看不了图片」。
- **发消息（字段错了不报错但发不出去）**：`sendmessage` 的 `msg` 里 `from_user_id` 必须传空字符串 `""`、`client_id` 每次随机唯一、`message_type=2`、`message_state=2`、`context_token` 原样回传用户那条消息里的值；顶层必须带 `base_info{channel_version, bot_agent}`。
- **-14 处理**：`ret`/`errcode` 为 `-14` 时先暂停 5 分钟重试一次（可能是限流而非过期）；仍失败才判 token 死亡：token **没有 refresh 机制**，在 `state/` 写 `NEEDS_RELOGIN` 标记，等用户重新扫码；看门狗负责通知（只通知一次，重新登录成功后标记自动清除、下次过期再通知）。
- **防重复回复（两个闸口，缺一不可）**：worker 把消息 ID 记入 `processed.json` 是在回复生成**之后**，而 hook 每 5 秒轮询一次——只要一次回复生成超过 5 秒（如需要联网查资料），下一轮轮询就会把同一条消息再唤醒一个 worker，两个 worker 各写一份同名 pending，serve 依次发出，用户就会收到两条内容不同的回复。因此：①轮询脚本唤醒前先把待处理 ID 写入 `state/claimed.json`（带时间戳，10 分钟过期防 worker 中途死亡导致消息永久丢失），已 claim 的不再唤醒；②serve 发送侧按 `message_id` 去重：成功发送后把 ID 与时间写入 `state/sent_ids.json`，1 小时窗口内同 ID 的 pending 文件直接跳过并归档。`scripts/wechat-inbox-watch.sh` 与 `scripts/bridge.py` 已内置这两道闸口。

## 安全红线

- `state/session.json` 含登录凭据：**绝不写入聊天记录、记忆文件、代码仓库或发给任何人**；安装目录必须有 `.gitignore` 忽略 `state/`。
- 日志与 `status` 输出里 token 只能显示掩码（头尾各 4 位 + 长度），context_token 同样不得出现在任何输出里。
- 只回复机主本人的消息；其他人发来的消息直接忽略并记日志。

## 已知限制（部署完成后要明确告诉用户）

- 只支持一对一私聊，不支持群聊。
- 不能主动发起对话：用户必须先发过消息，才能在该会话里回复。
- 连续发约 10 条而用户不回复会被官方拦截；用户回一句即重置计数。
- 定时推送（如每日简报）**无法**搬到微信：推送时刻用户通常还没发过消息、没有有效 context_token。简报类内容继续走原有渠道，不要承诺微信推送。

## 运维速查

- 状态：`python3 bridge.py status`（登录态、serve 进程、context 缓存、队列积压）。
- 日志：`state/serve.log`（收发）、`state/watchdog.log`（看门狗动作）、`state/serve.out`（serve 标准输出）。
- 手动重启 serve：杀掉 `state/serve.pid` 的进程后按部署步骤 3 拉起；看门狗也会在 5 分钟内自动拉起。
- 重新登录：从部署步骤 2 重走 login → wait-login，serve 若在运行需重启以加载新凭据（或等看门狗/手动处理）。
