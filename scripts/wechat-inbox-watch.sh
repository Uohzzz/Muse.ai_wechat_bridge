#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BRIDGE="$HOME/workspace/wechat-bridge"

NEW_IDS=$(python3 - "$BRIDGE" <<'PYEOF'
import json, os, sys, time
bridge = sys.argv[1]
inbox = os.path.join(bridge, "inbox")
state = os.path.join(bridge, "state")
processed_path = os.path.join(state, "processed.json")
claimed_path = os.path.join(state, "claimed.json")
CLAIM_TTL = 600  # claim 10 分钟过期：worker 若中途死掉，消息会被重新唤醒，不永久丢失

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

processed = set(load_json(processed_path, []))
claimed = load_json(claimed_path, {})
if not isinstance(claimed, dict):
    claimed = {}
now = time.time()
# 清理：已处理的、或已过期的 claim
claimed = {k: v for k, v in claimed.items()
           if k not in processed and isinstance(v, (int, float)) and now - v < CLAIM_TTL}
ids = []
if os.path.isdir(inbox):
    for name in sorted(os.listdir(inbox)):
        if name.endswith(".json"):
            mid = name[:-5]
            if mid not in processed and mid not in claimed:
                ids.append(mid)
                claimed[mid] = now  # 唤醒前先 claim，杜绝下一轮轮询重复唤醒同一条消息
if ids:
    tmp = claimed_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(claimed, f, ensure_ascii=False)
    os.replace(tmp, claimed_path)
print(",".join(ids))
PYEOF
)

if [ -n "$NEW_IDS" ]; then
  wake "微信 inbox 有未处理的新消息" "{\"message_ids\": \"$NEW_IDS\"}"
else
  silent "无未处理消息"
fi
