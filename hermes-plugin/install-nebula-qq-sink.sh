***REMOVED***!/bin/sh
***REMOVED*** 安装 nebula-qq-sink 插件（让 Hermes 不回复 QQ 审核指令）。
***REMOVED*** 用法：把本仓库的 hermes-plugin/nebula-qq-sink 目录传上来后执行
***REMOVED***       sh install-nebula-qq-sink.sh <ADMIN_OPENID>
set -e

OPENID="${1:-}"
SRC="$(cd "$(dirname "$0")" && pwd)/nebula-qq-sink"
HERMES_HOME="${HERMES_HOME:-/home/admin/.hermes}"
DEST="$HERMES_HOME/plugins/nebula-qq-sink"
QUEUE_DIR="$HERMES_HOME/nebula-zero"
QUEUE="$QUEUE_DIR/qq-commands.jsonl"

if [ -z "$OPENID" ]; then
  echo "用法：sh install-nebula-qq-sink.sh <ADMIN_OPENID>" >&2
  exit 1
fi
if [ ! -f "$SRC/__init__.py" ]; then
  echo "找不到 $SRC/__init__.py" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")" "$QUEUE_DIR"
rm -rf "$DEST"
cp -a "$SRC" "$DEST"

***REMOVED*** 把 OpenID 和队列路径写进 config.json（用 python 改，避免手改 JSON 出错）
python3 - "$DEST/config.json" "$OPENID" "$QUEUE" <<'PY'
import json, sys
path, openid, queue = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["admin_openids"] = [openid]
cfg["queue_file"] = queue
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
print("admin_openids ->", cfg["admin_openids"])
print("queue_file     ->", cfg["queue_file"])
PY

***REMOVED*** 队列文件由 gateway（以 admin 身份）写入，所以属主必须是 admin。
touch "$QUEUE"
chown admin:admin "$QUEUE"
chmod 600 "$QUEUE"

echo "插件已安装到 $DEST"
echo "队列文件 $QUEUE"
echo "接下来重启 gateway 使插件生效："
echo "  systemctl --user restart hermes-gateway.service"
echo "验证：hermes plugins list | grep nebula-qq-sink"