# 看门狗 cron 正文模板

新建 interval cron（每 5 分钟，标题“微信桥健康看门狗”）时使用下面正文（路径按实际安装目录替换）：

---

微信桥（~/workspace/wechat-bridge）健康看门狗，每 5 分钟执行一次：

1. 先检查 ~/workspace/wechat-bridge/state/NEEDS_RELOGIN 是否存在：
   - 存在且 state/notified_relogin 不存在：向 state/watchdog.log 追加一行（当前时间 + NEEDS_RELOGIN detected），创建空文件 state/notified_relogin，并给用户发一条消息：「微信桥的登录已失效，需要重新扫码登录——你在主对话里说一声，我重新走一遍 login 流程。」不要尝试拉起 serve。
   - 存在且 notified_relogin 已存在：本轮静默，不发任何消息。
2. NEEDS_RELOGIN 不存在时，检查 serve 是否存活：读 state/serve.pid 里的 pid，用 kill -0 检测；pidfile 不存在、pid 为空或进程不存在都算已挂。
   - 存活：本轮静默，不发任何消息，也不要写日志。
   - 已挂：在 ~/workspace/wechat-bridge 目录执行 `nohup python3 bridge.py serve >> state/serve.out 2>&1 &` 拉起，等 3 秒后按同样方法复检；向 state/watchdog.log 追加一行（当前时间 + restarted serve + 新 pid 或拉起失败）；给用户发一条简短消息：拉起成功就说「微信桥 serve 刚才掉了，我已自动拉起，微信收发恢复正常」，失败就说「微信桥 serve 掉了且自动拉起失败，需要人工处理」。

全程不得读取或输出 state/session.json 中的 bot_token，不得把任何凭据写进消息或日志。
