#!/usr/bin/env python3
"""微信 iLink / Clawbot Bot 通道桥接（仅标准库 + qrcode）。

命令：
  login        获取登录二维码，生成二维码图片
  wait-login   轮询扫码状态，成功后把凭据写入 state/session.json
  serve        常驻：长轮询收消息 -> inbox/；发送 outbox/pending/ 的回复
  send         用缓存的 context_token 发一条文本消息
  status       查看登录与运行状态（token 脱敏）

凭据只存 state/session.json（.gitignore 已忽略 state/），日志与输出一律脱敏。
协议依据：openclaw-weixin/docs/protocol_zh_CN.md
"""

import argparse
import base64
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
INBOX_DIR = os.path.join(BASE_DIR, "inbox")
OUTBOX_PENDING = os.path.join(BASE_DIR, "outbox", "pending")
OUTBOX_SENT = os.path.join(BASE_DIR, "outbox", "sent")
OUTBOX_FAILED = os.path.join(BASE_DIR, "outbox", "failed")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")
PENDING_QR_FILE = os.path.join(STATE_DIR, "pending_qrcode.json")
CURSOR_FILE = os.path.join(STATE_DIR, "get_updates_buf.txt")
LAST_CONTEXT_FILE = os.path.join(STATE_DIR, "last_context.json")
SENT_IDS_FILE = os.path.join(STATE_DIR, "sent_ids.json")
DEDUPE_WINDOW_SEC = 3600  # 同一 message_id 在此窗口内只发送一次，防重复回复
PID_FILE = os.path.join(STATE_DIR, "serve.pid")
NEEDS_RELOGIN_FILE = os.path.join(STATE_DIR, "NEEDS_RELOGIN")
NOTIFIED_RELOGIN_FILE = os.path.join(STATE_DIR, "notified_relogin")
SERVE_LOG = os.path.join(STATE_DIR, "serve.log")
QR_VISIBLE_PATH = os.path.expanduser("~/workspace/your_files/wechat-login-qr.png")
QR_LOCAL_PATH = os.path.join(STATE_DIR, "login-qr.png")

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VERSION = "132102"
CHANNEL_VERSION = "2.4.8"
BOT_AGENT = "OpenClaw"


class BridgeError(Exception):
    pass


class TokenDead(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs():
    for d in (STATE_DIR, INBOX_DIR, OUTBOX_PENDING, OUTBOX_SENT, OUTBOX_FAILED):
        os.makedirs(d, exist_ok=True)


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def mask(secret):
    if not secret:
        return "(无)"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}(len={len(secret)})"


def make_uin():
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def app_headers():
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}


def auth_headers(token):
    h = app_headers()
    h["AuthorizationType"] = "ilink_bot_token"
    h["Authorization"] = f"Bearer {token}"
    h["X-WECHAT-UIN"] = make_uin()
    return h


def http_json(method, url, headers=None, body=None, timeout=35):
    """带连接层重试的 HTTP JSON 调用：断开/超时自动重试 3 次，退避 1s/2s。"""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
                return json.loads(raw.decode("utf-8"))
            except Exception:
                last_exc = e
        except (http.client.RemoteDisconnected, ConnectionError, TimeoutError,
                socket.timeout, urllib.error.URLError) as e:
            last_exc = e
        if attempt < 2:
            time.sleep((1, 2)[attempt])
    raise BridgeError(f"HTTP {method} {urllib.parse.urlparse(url).path} 重试 3 次仍失败: {last_exc}")


def load_session():
    s = read_json(SESSION_FILE)
    if not s or not s.get("bot_token"):
        return None
    return s


def api_base(session=None):
    if session and session.get("baseurl"):
        return session["baseurl"].rstrip("/")
    return DEFAULT_BASE


def biz_code(resp):
    """业务返回码：优先 ret，其次 errcode。"""
    if isinstance(resp, dict):
        if resp.get("ret") is not None:
            return resp.get("ret")
        if resp.get("errcode") is not None:
            return resp.get("errcode")
    return None


def mark_token_dead(reason=""):
    atomic_write_text(NEEDS_RELOGIN_FILE, f"{now_iso()} {reason}\n")


# ---------------------------------------------------------------- login

def cmd_login(_args):
    ensure_dirs()
    url = f"{DEFAULT_BASE}/ilink/bot/get_bot_qrcode?bot_type=3"
    headers = app_headers()
    headers["AuthorizationType"] = "ilink_bot_token"
    headers["X-WECHAT-UIN"] = make_uin()
    # 注意：此接口不带 Authorization、不带 base_info
    resp = http_json("POST", url, headers=headers, body={"local_token_list": []}, timeout=20)
    ret = resp.get("ret")
    if ret not in (0, None):
        raise BridgeError(f"获取二维码失败: ret={ret} errmsg={resp.get('errmsg')}")
    qrcode_value = resp.get("qrcode")
    img_content = resp.get("qrcode_img_content")
    if not qrcode_value or not img_content:
        raise BridgeError(f"响应缺少 qrcode/qrcode_img_content: {list(resp.keys())}")
    atomic_write_json(PENDING_QR_FILE, {"qrcode": qrcode_value, "created_at": now_iso()})
    import qrcode as qrcode_lib
    img = qrcode_lib.make(img_content)
    img.save(QR_LOCAL_PATH)
    os.makedirs(os.path.dirname(QR_VISIBLE_PATH), exist_ok=True)
    img.save(QR_VISIBLE_PATH)
    print("二维码已生成（约 2 分钟内有效）:")
    print(f"  可见文件: {QR_VISIBLE_PATH}")
    print(f"  本地副本: {QR_LOCAL_PATH}")
    print("请用微信扫一扫扫描，然后运行 wait-login 轮询状态。")
    return 0


# ---------------------------------------------------------------- wait-login

def cmd_wait_login(args):
    ensure_dirs()
    pending = read_json(PENDING_QR_FILE, {}) or {}
    qrcode_value = args.qrcode or pending.get("qrcode")
    if not qrcode_value:
        raise BridgeError("没有待轮询的二维码，请先运行 login")
    verify_code = args.verify_code
    base = DEFAULT_BASE
    deadline = time.time() + args.timeout
    last_status = None
    while time.time() < deadline:
        q = urllib.parse.urlencode({"qrcode": qrcode_value})
        if verify_code:
            q += "&" + urllib.parse.urlencode({"verify_code": verify_code})
        url = f"{base}/ilink/bot/get_qrcode_status?{q}"
        try:
            resp = http_json("GET", url, headers=app_headers(), timeout=35)
        except BridgeError as e:
            # 协议惯例：二维码状态轮询请求失败按 wait 处理，继续等
            print(f"[wait-login] 轮询请求失败（按 wait 继续）: {e}")
            time.sleep(1)
            continue
        status = resp.get("status")
        if status != last_status:
            print(f"[wait-login] status={status}")
            last_status = status
        if status in ("wait", "scaned"):
            continue
        if status == "confirmed":
            session = {
                "bot_token": resp.get("bot_token"),
                "baseurl": resp.get("baseurl") or DEFAULT_BASE,
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "ilink_user_id": resp.get("ilink_user_id"),
                "saved_at": now_iso(),
            }
            if not session["bot_token"]:
                raise BridgeError("confirmed 响应缺少 bot_token")
            atomic_write_json(SESSION_FILE, session)
            for flag in (NEEDS_RELOGIN_FILE, NOTIFIED_RELOGIN_FILE):
                if os.path.exists(flag):
                    os.remove(flag)
            if os.path.exists(PENDING_QR_FILE):
                os.remove(PENDING_QR_FILE)
            print(f"[wait-login] 登录成功 bot_id={session['ilink_bot_id']} "
                  f"user_id={mask(session['ilink_user_id'])} token={mask(session['bot_token'])}")
            return 0
        if status == "expired":
            print("[wait-login] 二维码已过期，请重新运行 login 取码", file=sys.stderr)
            return 2
        if status == "need_verifycode":
            print("[wait-login] 需要手机上显示的验证码。用 "
                  "wait-login --verify-code <验证码> 继续轮询。", file=sys.stderr)
            return 3
        if status == "verify_code_blocked":
            print("[wait-login] 验证码错误次数过多，已被拦截，请重新 login 取码。", file=sys.stderr)
            return 4
        if status == "scaned_but_redirect":
            host = resp.get("redirect_host") or ""
            if host:
                base = host if host.startswith("http") else f"https://{host}"
                print(f"[wait-login] 切换轮询地址: {base}")
            continue
        if status == "binded_redirect":
            print("[wait-login] 该 Bot 已绑定到当前实例，视为已绑定。")
            return 0
        # 未知状态：短暂等待后继续
        time.sleep(1)
    print("[wait-login] 轮询超时", file=sys.stderr)
    return 5


# ---------------------------------------------------------------- send

def do_send_message(session, to_user_id, text, context_token):
    """构造并发送一条文本消息。关键字段照协议：from_user_id=""、message_type=2、
    message_state=2、client_id 每次随机唯一、context_token 原样回传、顶层 base_info。"""
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token or "",
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": base_info(),
    }
    url = f"{api_base(session)}/ilink/bot/sendmessage"
    return http_json("POST", url, headers=auth_headers(session["bot_token"]),
                     body=payload, timeout=20)


def send_with_relogin_handling(session, to_user_id, text, context_token, log=print):
    """发消息并处理 -14：先暂停 5 分钟重试一次（可能是限流而非过期），
    仍失败才判 token 死亡，写 NEEDS_RELOGIN 标记并抛 TokenDead。"""
    resp = do_send_message(session, to_user_id, text, context_token)
    if biz_code(resp) == -14:
        log("[send] 收到 -14，暂停 5 分钟后重试一次")
        time.sleep(300)
        resp = do_send_message(session, to_user_id, text, context_token)
        if biz_code(resp) == -14:
            mark_token_dead("sendmessage ret=-14 twice")
            raise TokenDead("sendmessage 连续两次 -14，判定 token 已失效")
    return resp


def cmd_send(args):
    session = load_session()
    if not session:
        raise BridgeError("尚未登录，请先 login + wait-login")
    to_user = args.to
    context_token = args.context
    if not to_user or not context_token:
        last = read_json(LAST_CONTEXT_FILE, {}) or {}
        to_user = to_user or last.get("to_user_id")
        context_token = context_token or last.get("context_token")
    if not to_user:
        to_user = session.get("ilink_user_id")
    if not context_token:
        raise BridgeError("没有可用的 context_token：请先从微信给 Bot 发一条消息，"
                          "serve 收到后会缓存 context_token，或用 --context 显式指定")
    try:
        resp = send_with_relogin_handling(session, to_user, args.text, context_token)
    except TokenDead:
        print("发送失败：token 已失效，已写入 NEEDS_RELOGIN 标记，请重新扫码登录。", file=sys.stderr)
        return 5
    code = biz_code(resp)
    if code not in (0, None):
        print(f"发送失败: ret/errcode={code} errmsg={resp.get('errmsg')}", file=sys.stderr)
        return 6
    print(f"已发送 -> {mask(to_user)}: {args.text[:40]}")
    return 0


# ---------------------------------------------------------------- serve

class ServeLog:
    def __init__(self):
        ensure_dirs()
        self._fh = open(SERVE_LOG, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, msg):
        line = f"[{now_iso()}] {msg}"
        with self._lock:
            print(line, flush=True)
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:
                pass


def process_incoming(session, msg, log):
    """把一条入站消息写入 inbox/。返回 True 表示已处理（写入或明确忽略）。"""
    if msg.get("group_id"):
        log(f"[in] 忽略群消息 group_id={msg.get('group_id')}")
        return True
    from_user = msg.get("from_user_id") or ""
    owner = session.get("ilink_user_id")
    if owner and from_user != owner:
        log(f"[in] 忽略非机主消息 from={mask(from_user)}")
        return True
    if msg.get("message_type") == 2:
        return True  # Bot 自己发的，不回写 inbox
    items = msg.get("item_list") or []
    text_parts = []
    media_types = []
    for it in items:
        t = it.get("type")
        if t == 1:
            txt = (it.get("text_item") or {}).get("text")
            if txt:
                text_parts.append(txt)
        elif t == 3:
            voice = it.get("voice_item") or {}
            if voice.get("text"):
                text_parts.append(voice["text"])
                media_types.append("voice_transcribed")
            else:
                media_types.append("voice")
        elif t == 2:
            media_types.append("image")
        elif t == 5:
            media_types.append("video")
        elif t == 4:
            media_types.append("file")
        else:
            media_types.append(f"type{t}")
    text = "\n".join(text_parts) if text_parts else None
    if text:
        kind = "text"
    elif media_types:
        kind = media_types[0].split("_")[0]
    else:
        kind = "unknown"
    message_id = msg.get("message_id")
    mid = str(message_id) if message_id is not None else f"{msg.get('create_time_ms')}_{msg.get('seq')}"
    record = {
        "message_id": mid,
        "seq": msg.get("seq"),
        "text": text,
        "type": kind,
        "media_types": media_types,
        "from_user_id": from_user,
        "to_user_id": msg.get("to_user_id"),
        "context_token": msg.get("context_token"),
        "create_time_ms": msg.get("create_time_ms"),
        "received_at": now_iso(),
    }
    path = os.path.join(INBOX_DIR, f"{mid}.json")
    if not os.path.exists(path):
        atomic_write_json(path, record)
    if msg.get("context_token"):
        atomic_write_json(LAST_CONTEXT_FILE, {
            "to_user_id": from_user,
            "context_token": msg["context_token"],
            "updated_at": now_iso(),
        })
    log(f"[in] message_id={mid} type={kind} text={str(text)[:60]!r}")
    return True


def flush_outbox(session, log, attempts):
    """把 outbox/pending/ 的回复发出去，成功移 sent/，多次失败移 failed/。"""
    try:
        names = sorted(os.listdir(OUTBOX_PENDING))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(OUTBOX_PENDING, name)
        data = read_json(path)
        if not data or not data.get("text"):
            log(f"[out] 跳过无效文件 {name}")
            os.replace(path, os.path.join(OUTBOX_FAILED, name))
            continue
        # 去重闸口：同一 message_id 在窗口内已成功发送过，就不再发第二遍
        # （防上游重复唤醒 worker 生成两条回复、写同名 pending 文件）
        mid = str(data.get("message_id") or name[:-5])
        sent_ids = read_json(SENT_IDS_FILE) or {}
        last_sent = sent_ids.get(mid)
        if isinstance(last_sent, (int, float)) and time.time() - last_sent < DEDUPE_WINDOW_SEC:
            log(f"[out] 跳过重复发送 {name}（message_id {mid} 已发送过）")
            os.replace(path, os.path.join(OUTBOX_SENT, name))
            continue
        to_user = data.get("to_user_id") or session.get("ilink_user_id")
        try:
            resp = send_with_relogin_handling(session, to_user, data["text"],
                                              data.get("context_token"), log=log)
        except TokenDead:
            raise
        except BridgeError as e:
            attempts[name] = attempts.get(name, 0) + 1
            log(f"[out] 发送失败({attempts[name]}/5) {name}: {e}")
            if attempts[name] >= 5:
                os.replace(path, os.path.join(OUTBOX_FAILED, name))
                log(f"[out] {name} 失败次数过多，移入 failed/")
            continue
        code = biz_code(resp)
        if code in (0, None):
            os.replace(path, os.path.join(OUTBOX_SENT, name))
            attempts.pop(name, None)
            sent_ids[mid] = time.time()
            pruned = {k: v for k, v in sent_ids.items()
                      if isinstance(v, (int, float)) and time.time() - v < 86400}
            atomic_write_text(SENT_IDS_FILE, json.dumps(pruned, ensure_ascii=False))
            log(f"[out] 已发送 {name} -> {mask(to_user)}")
        else:
            attempts[name] = attempts.get(name, 0) + 1
            log(f"[out] 发送返回 code={code} errmsg={resp.get('errmsg')} ({attempts[name]}/5) {name}")
            if attempts[name] >= 5:
                os.replace(path, os.path.join(OUTBOX_FAILED, name))


def cmd_serve(_args):
    ensure_dirs()
    session = load_session()
    if not session:
        raise BridgeError("尚未登录，serve 无法启动")
    log = ServeLog()
    atomic_write_text(PID_FILE, str(os.getpid()))
    stop_event = threading.Event()
    send_lock = threading.Lock()
    attempts = {}

    # notifyStart：尽力而为，失败只记日志
    try:
        http_json("POST", f"{api_base(session)}/ilink/bot/msg/notifystart",
                  headers=auth_headers(session["bot_token"]), body={"base_info": base_info()},
                  timeout=15)
        log("[serve] notifyStart 已发送")
    except Exception as e:
        log(f"[serve] notifyStart 失败（忽略）: {e}")

    def sender_loop():
        while not stop_event.is_set():
            try:
                with send_lock:
                    flush_outbox(session, log, attempts)
            except TokenDead:
                log("[serve] token 失效（发送侧），serve 退出，等待重新扫码")
                stop_event.set()
                return
            except Exception as e:
                log(f"[serve] 发送线程异常: {e}")
            stop_event.wait(2)

    sender = threading.Thread(target=sender_loop, daemon=True)
    sender.start()

    cursor = ""
    if os.path.exists(CURSOR_FILE):
        with open(CURSOR_FILE, encoding="utf-8") as f:
            cursor = f.read().strip()
    log(f"[serve] 启动，游标={'有' if cursor else '空'}，bot={mask(session.get('ilink_bot_id'))}")

    backoff = 5
    try:
        while not stop_event.is_set():
            payload = {"get_updates_buf": cursor, "base_info": base_info()}
            try:
                resp = http_json("POST", f"{api_base(session)}/ilink/bot/getupdates",
                                 headers=auth_headers(session["bot_token"]),
                                 body=payload, timeout=45)
            except BridgeError as e:
                log(f"[poll] getupdates 请求失败: {e}；{backoff}s 后重试")
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            code = biz_code(resp)
            if code == -14:
                log("[poll] getupdates 返回 -14，暂停 5 分钟后重试一次")
                stop_event.wait(300)
                if stop_event.is_set():
                    break
                try:
                    resp = http_json("POST", f"{api_base(session)}/ilink/bot/getupdates",
                                     headers=auth_headers(session["bot_token"]),
                                     body=payload, timeout=45)
                except BridgeError as e:
                    log(f"[poll] -14 后重试请求失败: {e}")
                    continue
                if biz_code(resp) == -14:
                    mark_token_dead("getupdates ret=-14 twice")
                    log("[poll] 连续两次 -14，判定 token 失效，serve 退出")
                    break
                code = biz_code(resp)
            if code not in (0, None):
                log(f"[poll] getupdates 返回 code={code} errmsg={resp.get('errmsg')}；{backoff}s 后重试")
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            new_cursor = resp.get("get_updates_buf")
            if new_cursor:
                cursor = new_cursor
                atomic_write_text(CURSOR_FILE, cursor)
            for msg in resp.get("msgs") or []:
                try:
                    process_incoming(session, msg, log)
                except Exception as e:
                    log(f"[in] 处理消息异常: {e}")
    finally:
        stop_event.set()
        try:
            http_json("POST", f"{api_base(session)}/ilink/bot/msg/notifystop",
                      headers=auth_headers(session["bot_token"]), body={"base_info": base_info()},
                      timeout=10)
        except Exception:
            pass
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        log("[serve] 已退出")
    return 0


# ---------------------------------------------------------------- status

def cmd_status(_args):
    ensure_dirs()
    session = load_session()
    print("== 微信桥状态 ==")
    if session:
        print(f"登录: 已登录")
        print(f"  bot_id : {session.get('ilink_bot_id')}")
        print(f"  user_id: {mask(session.get('ilink_user_id'))}")
        print(f"  token  : {mask(session.get('bot_token'))}")
        print(f"  baseurl: {session.get('baseurl')}")
        print(f"  保存于 : {session.get('saved_at')}")
    else:
        print("登录: 未登录")
    running = False
    pid = None
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            running = False
    print(f"serve: {'运行中 (pid ' + str(pid) + ')' if running else '未运行'}")
    if os.path.exists(NEEDS_RELOGIN_FILE):
        print("⚠️ NEEDS_RELOGIN: token 已失效，需要重新扫码登录")
    last = read_json(LAST_CONTEXT_FILE, {}) or {}
    print(f"context_token 缓存: {'有 (' + str(last.get('updated_at')) + ')' if last.get('context_token') else '无'}")
    n_inbox = len([n for n in os.listdir(INBOX_DIR) if n.endswith('.json')]) if os.path.isdir(INBOX_DIR) else 0
    n_pending = len([n for n in os.listdir(OUTBOX_PENDING) if n.endswith('.json')]) if os.path.isdir(OUTBOX_PENDING) else 0
    print(f"inbox 文件数: {n_inbox}；outbox 待发: {n_pending}")
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="微信 iLink Bot 桥接")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="获取登录二维码")

    p_wait = sub.add_parser("wait-login", help="轮询扫码状态")
    p_wait.add_argument("--qrcode", default=None)
    p_wait.add_argument("--verify-code", default=None)
    p_wait.add_argument("--timeout", type=int, default=600)

    p_send = sub.add_parser("send", help="发送一条文本消息")
    p_send.add_argument("--text", required=True)
    p_send.add_argument("--to", default=None)
    p_send.add_argument("--context", default=None)

    sub.add_parser("serve", help="常驻收发循环")
    sub.add_parser("status", help="查看状态")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "login":
            return cmd_login(args)
        if args.cmd == "wait-login":
            return cmd_wait_login(args)
        if args.cmd == "send":
            return cmd_send(args)
        if args.cmd == "serve":
            return cmd_serve(args)
        if args.cmd == "status":
            return cmd_status(args)
    except BridgeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
