# wechat-ilink-bridge — 微信 iLink Bot 桥（Skill）

把 AI 助手接入你自己的微信：走**微信官方 iLink / Clawbot Bot 通道**（`ilinkai.weixin.qq.com`），扫码登录后，助手就能在微信一对一私聊里收消息、回消息。协议依据腾讯官方开源仓库 [openclaw-weixin](https://github.com/tencent/openclaw-weixin) 的协议文档。

这是一个给 AI 助手用的 **Skill 包**：把本仓库放进助手的 skills 目录，助手读 `SKILL.md` 就能按步骤完成部署（取码、扫码登录、常驻服务、自动回复、健康看门狗）。

## 能做什么

- 📩 **收发打通**：你在微信里给 Bot 发消息，助手实时收到；助手的回复自动发回微信，手机不需要 VPN，微信正常联网即可
- 🤖 **自动回复**：每 15 秒检查新消息，多条连发合并成一条回复（200 字内，不刷屏）；图片/文件会礼貌请你打字；复杂问题先答“收到，我记下了”再转人工跟进
- 🛡️ **自愈**：看门狗每 5 分钟检查常驻进程，挂了自动拉起；登录失效只通知你一次重新扫码
- 🔐 **凭据安全**：登录凭据只存本地 `state/session.json`，目录强制 `.gitignore`，日志与状态输出全部脱敏

## 快速开始

前置：Python 3，`pip install qrcode pillow`，微信在手边（二维码约 2 分钟有效，要先备好手机再取码）。

```bash
# 1. 落地（默认装到 ~/workspace/wechat-bridge/）
mkdir -p ~/workspace/wechat-bridge
cp scripts/bridge.py ~/workspace/wechat-bridge/
cp templates/gitignore ~/workspace/wechat-bridge/.gitignore

# 2. 登录：生成二维码 → 微信扫一扫 → 轮询到 confirmed
cd ~/workspace/wechat-bridge
python3 bridge.py login        # 生成二维码图片
python3 bridge.py wait-login   # 轮询扫码状态，凭据写入 state/session.json

# 3. 常驻收发
nohup python3 bridge.py serve > state/serve.out 2>&1 &
python3 bridge.py status
```

之后：**先在微信里给 Bot 发一条消息**（官方限制，Bot 不能主动开口），再 `python3 bridge.py send --text "你好"` 验证回复链路。自动回复 hook 与看门狗 cron 的配置见 `SKILL.md` 第 5、6 步与 `templates/`。

## 仓库结构

```
SKILL.md                    完整部署与运维手册（给 AI 助手执行）
scripts/bridge.py           桥本体：login / wait-login / serve / send / status，仅标准库 + qrcode
scripts/wechat-inbox-watch.sh  自动回复 hook 的轮询脚本
templates/hook-prompt.md    自动回复 worker 指令模板
templates/watchdog-cron.md  看门狗 cron 正文模板
templates/gitignore         安装目录 .gitignore（忽略 state/）
```

## 关键设计

- **文件队列解耦**：收件 `inbox/`、待发 `outbox/pending/`、已发 `outbox/sent/`，回复以 message_id 命名保证幂等，防重复发送
- **连接层重试**：所有 API 调用断开/超时自动重试 3 次（退避 1s/2s），微信服务端偶发断连是常态
- **-14 语义**：先暂停 5 分钟重试（多半是限流），仍失败才判登录失效——token 无 refresh 机制，只能重新扫码
- **sendmessage 字段**：`from_user_id` 必须为空串、`message_state=2`、`context_token` 原样回传，错一个就“发不出去还不报错”

## 限制（官方通道决定，不是实现缺陷）

- 只支持一对一私聊，不支持群聊
- Bot 不能主动发起对话，必须对方先发过消息
- 连续发约 10 条、对方不回复会被拦截，对方回一句即重置
- 因此**定时推送搬不到微信**（推送时刻没有有效的 context_token），简报类内容请走其他渠道

## 安全提示

`state/session.json` 是登录凭据，等同于密码：不要提交到任何仓库、不要发给任何人。本仓库不含任何真实凭据与运行状态。
