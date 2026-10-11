#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BRIDGE="$HOME/workspace/wechat-bridge"

NEW_IDS=$(python3 - "$BRIDGE" <<'PYEOF'
import json, os, sys
bridge = sys.argv[1]
inbox = os.path.join(bridge, "inbox")
processed_path = os.path.join(bridge, "state", "processed.json")
processed = set()
if os.path.exists(processed_path):
    try:
        with open(processed_path, encoding="utf-8") as f:
            processed = set(json.load(f))
    except Exception:
        processed = set()
ids = []
if os.path.isdir(inbox):
    for name in sorted(os.listdir(inbox)):
        if name.endswith(".json"):
            mid = name[:-5]
            if mid not in processed:
                ids.append(mid)
print(",".join(ids))
PYEOF
)

if [ -n "$NEW_IDS" ]; then
  wake "微信 inbox 有未处理的新消息" "{\"message_ids\": \"$NEW_IDS\"}"
else
  silent "无未处理消息"
fi
