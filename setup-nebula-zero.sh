#!/usr/bin/env bash
# =============================================================================
# Nebula-Zero 一键配置脚本（3 Stage 版）
#
#   Stage 1  校验 Hermes QQ 渠道依赖（aiohttp / httpx）
#   Stage 2  写入 QQ Bot 凭据并重启 gateway
#   Stage 3  存放 GitHub fine-grained PAT 并校验权限
#
# 为什么没有"修 GitHub 出网"这一步：
#   实测结论 —— api.github.com 直连可用（200, 0.30s），这是审核机器人唯一的
#   GitHub 依赖；github.com 的 38 个官方 web IP 全部 443 不可达，而审核机器人、
#   QQ 通道、代码上传都不需要它。唯一的用途是 GitHub OAuth 的换 token 请求，
#   而那需要让 client_secret 和用户 access token 经过不受信任的第三方免费代理。
#   这个代价不值得，所以 GitHub OAuth 保持关闭，出网问题留待上 HTTPS / 配代理时
#   一并解决。
#
# 用法（必须交互式，缺 -t 会导致无法输入凭据）：
#   scp setup-nebula-zero.sh root@YOUR_HOST:/root/
#   ssh -t root@YOUR_HOST 'bash /root/setup-nebula-zero.sh'
#
# 全程幂等：重复执行安全。已存在的配置会被覆盖前先备份。
# =============================================================================
set -uo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
stage() { printf '\n%s========================================================%s\n' "$CYN" "$RST"; printf '  %s%s%s\n' "$CYN" "$*" "$RST"; printf '%s========================================================%s\n' "$CYN" "$RST"; }
ok()   { printf '  %s[OK]%s   %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '  %s[FAIL]%s %s\n' "$RED" "$RST" "$*"; exit 1; }
log()  { printf '  %s·%s %s\n' "$CYN" "$RST" "$*"; }

HERMES_USER=admin
HERMES_HOME=/home/admin/.hermes
HERMES_SRC=$HERMES_HOME/hermes-agent
HERMES_PY=$HERMES_HOME/tools/python-3.14.7+20260901-linux-x64/bin/python3
RUN_DIR=/run/user/1000
NEB_DIR=/root/.nebula-zero

# 审核机器人用到的凭据与地址，集中在这里便于排查
GITHUB_REPO=Swcmb/Nebula-Zero

# -----------------------------------------------------------------------------
# Stage 0 — 前置检查
# -----------------------------------------------------------------------------
stage "Stage 0 — 前置检查"

[ "$(id -u)" -eq 0 ] || die "必须用 root 执行"
command -v curl >/dev/null || die "缺少 curl"
command -v python3 >/dev/null || die "缺少 python3"
command -v systemctl >/dev/null || die "缺少 systemctl"
[ -x "$HERMES_PY" ] || die "找不到 Hermes 内置 python：$HERMES_PY"

log "检查审核机器人依赖的三个出口"
check_out() {
  local name=$1 url=$2
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 "$url" 2>/dev/null)
  case "$code" in
    2*|3*|4*) ok "$name 可达（$code）" ;;
    000)        die "$name 不可达（超时）。审核机器人无法工作，请先解决出网。" ;;
    *)          warn "$name 返回 $code（可能正常，需人工确认）" ;;
  esac
}
check_out "api.github.com（GitHub REST API）" https://api.github.com/
check_out "new-api 本地接口" http://127.0.0.1:3000/api/status
check_out "QQ Bot API（sgroup）" https://api.sgroup.qq.com/

log "记录基线（Stage 2 之后要比对，确保没动生产数据）"
USR_BEFORE=$(docker exec postgres psql -U root -d new-api -t -A \
  -c "select count(*) from users where deleted_at is null;" 2>/dev/null || echo "?")
CH_BEFORE=$(docker exec postgres psql -U root -d new-api -t -A \
  -c "select count(*) from channels;" 2>/dev/null || echo "?")
ok "基线：live_users=$USR_BEFORE  channels=$CH_BEFORE"

# -----------------------------------------------------------------------------
# Stage 1 — Hermes QQ 渠道依赖
# -----------------------------------------------------------------------------
stage "Stage 1 — 校验 Hermes QQ 渠道依赖（aiohttp / httpx）"

# 关键事实：gateway 不用系统 python，也不用 hermes-agent/.venv，而是用它在
# installs/<hash>/environments/<hash>/venv 下自建的 venv（hermes_bootstrap 会把它
# 的 site-packages 注入 sys.path）。早前用系统 python 检查会误判"依赖缺失"。
# 所以这里只做"定位 + 校验"，不主动安装任何东西。

find_gateway_venv() {
  sudo -u "$HERMES_USER" "$HERMES_PY" -I -c "
import os, sys
os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None)
sys.path.insert(0, '$HERMES_SRC')
try:
    import hermes_bootstrap
except Exception:
    pass
for p in sys.path:
    if 'site-packages' in p and os.path.isdir(p):
        print(p.split('/lib/')[0]); break
" 2>/dev/null | tail -1
}

GATEWAY_VENV=$(find_gateway_venv)
if [ -n "$GATEWAY_VENV" ]; then
  ok "gateway 实际使用的 venv：$GATEWAY_VENV"
else
  warn "未能自动定位，改用常见路径匹配"
  for cand in "$HERMES_HOME"/installs/*/environments/*/venv "$HERMES_SRC/.venv"; do
    [ -d "$cand/lib/python3.14/site-packages" ] && { GATEWAY_VENV=$cand; break; }
  done
fi

if [ -n "$GATEWAY_VENV" ] && [ -x "$GATEWAY_VENV/bin/python" ]; then
  if sudo -u "$HERMES_USER" "$GATEWAY_VENV/bin/python" -c \
       "import aiohttp, httpx; print('  依赖就绪  aiohttp=%s  httpx=%s' % (aiohttp.__version__, httpx.__version__))" 2>&1; then
    ok "QQ 渠道依赖齐备，无需安装"
  else
    warn "该 venv 缺 aiohttp/httpx。用 uv 精确安装（只加包，不做全量同步）"
    UV=$HERMES_HOME/tools/uv-0.12.3-linux-x64/uv
    if [ -x "$UV" ]; then
      sudo -u "$HERMES_USER" "$UV" pip install --python "$GATEWAY_VENV/bin/python" aiohttp httpx \
        >/tmp/hermes-deps.log 2>&1 || { tail -15 /tmp/hermes-deps.log | sed 's/^/      /'; die "uv pip install 失败"; }
    else
      sudo -u "$HERMES_USER" "$GATEWAY_VENV/bin/python" -m pip install --quiet aiohttp httpx \
        >/tmp/hermes-deps.log 2>&1 || { tail -15 /tmp/hermes-deps.log | sed 's/^/      /'; die "pip install 失败"; }
    fi
    sudo -u "$HERMES_USER" "$GATEWAY_VENV/bin/python" -c "import aiohttp, httpx; print('  复检 OK')" \
      || die "安装后仍不可导入"
  fi
else
  warn "找不到 gateway venv，跳过依赖校验（Stage 2 的平台接入验证会暴露问题）"
fi

# -----------------------------------------------------------------------------
# Stage 2 — 写入 QQ Bot 凭据
# -----------------------------------------------------------------------------
stage "Stage 2 — 写入 QQ Bot 凭据到 Hermes"

if [ -t 0 ]; then
  printf '  QQ App ID                    : '; read -r QQ_APP_ID
  printf '  QQ Client Secret             : '; read -r QQ_CLIENT_SECRET
  printf '  管理员 OpenID（QQBOT_HOME_CHANNEL）: '; read -r QQ_OPENID
  printf '  管理员 OpenID 备注（可留空）      : '; read -r QQ_OPEN_NAME
else
  die "检测到非交互式执行。凭据必须交互输入，请用：
     ssh -t root@YOUR_HOST 'bash /root/setup-nebula-zero.sh'"
fi

[ -n "$QQ_APP_ID" ]        || die "QQ App ID 为空"
[ -n "$QQ_CLIENT_SECRET" ] || die "QQ Client Secret 为空"
[ -n "$QQ_OPENID" ]        || die "管理员 OpenID 为空（机器人无法把通知发给你）"
QQ_OPEN_NAME=${QQ_OPEN_NAME:-管理员}

ENVFILE=$HERMES_HOME/.env
[ -f "$ENVFILE" ] || die "$ENVFILE 不存在"
chmod 600 "$ENVFILE"
cp -a "$ENVFILE" "$ENVFILE.bak.$(date +%Y%m%d-%H%M%S)"

# 幂等：先删旧的 QQ_* 行再追加，避免重复键
sed -i '/^QQ_APP_ID=/d;/^QQ_CLIENT_SECRET=/d;/^QQBOT_HOME_CHANNEL=/d;/^QQBOT_HOME_CHANNEL_NAME=/d;/^QQ_ALLOWED_USERS=/d;/^QQ_ALLOW_ALL_USERS=/d' "$ENVFILE"
cat >> "$ENVFILE" <<EOF

# --- Nebula-Zero QQ Bot（脚本写入 $(date '+%F %T')）---
QQ_APP_ID=$QQ_APP_ID
QQ_CLIENT_SECRET=$QQ_CLIENT_SECRET
QQBOT_HOME_CHANNEL=$QQ_OPENID
QQBOT_HOME_CHANNEL_NAME=$QQ_OPEN_NAME
QQ_ALLOWED_USERS=$QQ_OPENID
QQ_ALLOW_ALL_USERS=false
EOF
chmod 600 "$ENVFILE"
ok "已写入 $ENVFILE（备份：$(ls -t "$HERMES_HOME"/.env.bak.* | head -1)）"

log "重启 hermes-gateway"
sudo -u "$HERMES_USER" env XDG_RUNTIME_DIR=$RUN_DIR systemctl --user restart hermes-gateway
sleep 8
if sudo -u "$HERMES_USER" env XDG_RUNTIME_DIR=$RUN_DIR systemctl --user is-active --quiet hermes-gateway; then
  ok "gateway 运行中"
else
  sudo -u "$HERMES_USER" env XDG_RUNTIME_DIR=$RUN_DIR journalctl --user -u hermes-gateway -n 40 --no-pager | tail -20
  die "gateway 启动失败，日志已打印在上方"
fi

sleep 5
PLATFORMS=$(python3 -c "
import json
try:
    d = json.load(open('$HERMES_HOME/gateway_state.json'))
    print(','.join((d.get('platforms') or {}).keys()) or '(空)')
except Exception as e:
    print('读取失败:%s' % e)
" 2>/dev/null)
case "$PLATFORMS" in
  *qq*) ok "已接入平台：$PLATFORMS" ;;
  *)
    warn "gateway_state.json 的 platforms 未包含 qqbot。当前值：${PLATFORMS:-读取失败}"
    warn "常见三因：① AppID/Secret 填错 ② q.qq.com 后台未启用 C2C 消息 intent ③ 机器人仍在沙盒模式"
    log  "诊断日志：journalctl --user -u hermes-gateway -f"
    ;;
esac

# -----------------------------------------------------------------------------
# Stage 3 — GitHub PAT
# -----------------------------------------------------------------------------
stage "Stage 3 — 存放 GitHub Fine-grained PAT 并校验权限"

printf '  GitHub Fine-grained PAT: '; read -r GH_PAT
[ -n "$GH_PAT" ] || die "PAT 为空"

mkdir -p "$NEB_DIR"; chmod 700 "$NEB_DIR"
cat > "$NEB_DIR/github.env" <<EOF
# Nebula-Zero 审核机器人 · GitHub 凭据（写入于 $(date '+%F %T')）
# 该 PAT 的权限应严格限定为：
#   Repository access: 仅 Swcmb/Nebula-Zero
#   Permissions: Issues -> Read and write ; Metadata -> Read-only
GITHUB_TOKEN=$GH_PAT
GITHUB_REPO=$GITHUB_REPO
GITHUB_API=https://api.github.com
EOF
chmod 600 "$NEB_DIR/github.env"
ok "已写入 $NEB_DIR/github.env（权限 600）"

GH_HDR=(-H "Authorization: Bearer $GH_PAT" -H "Accept: application/vnd.github+json")

log "校验 PAT 能否读取目标仓库"
CODE=$(curl -s -o /tmp/pat-repo.json -w "%{http_code}" --max-time 20 "${GH_HDR[@]}" \
  "https://api.github.com/repos/$GITHUB_REPO" 2>/dev/null)
case "$CODE" in
  200) ok "PAT 有效，可访问 $GITHUB_REPO" ;;
  401) die "PAT 无效或已过期（401）。检查是否复制完整、是否已被撤销" ;;
  403) die "PAT 无权访问该仓库（403）" ;;
  404) die "仓库不存在，或该 PAT 的 Repository access 未选中 $GITHUB_REPO" ;;
  *)   warn "仓库查询返回 $CODE，稍后可手工复核" ;;
esac

log "校验 Issues 读权限"
ICODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "${GH_HDR[@]}" \
  "https://api.github.com/repos/$GITHUB_REPO/issues?state=all&per_page=1" 2>/dev/null)
case "$ICODE" in
  200) ok "Issues 读取正常" ;;
  *)   warn "Issues 读取返回 $ICODE —— 若为 403/404，机器人将无法监听申请" ;;
esac

log "校验标签 申请 是否存在（不存在则用 API 建）"
LABEL_CODE=$(curl -s -o /tmp/label-check.json -w "%{http_code}" --max-time 20 "${GH_HDR[@]}" \
  "https://api.github.com/repos/$GITHUB_REPO/labels/$(python3 -c "import urllib.parse;print(urllib.parse.quote('申请'))")" 2>/dev/null)
if [ "$LABEL_CODE" != "200" ]; then
  log "  标签不存在，尝试创建"
  MKCODE=$(curl -s -o /tmp/label-make.json -w "%{http_code}" --max-time 20 -X POST "${GH_HDR[@]}" \
    -d '{"name":"申请","color":"0e8a16","description":"账号申请"}' \
    "https://api.github.com/repos/$GITHUB_REPO/labels" 2>/dev/null)
  case "$MKCODE" in
    201) ok "标签 申请 已创建" ;;
    403) warn "无权创建标签（403）。请在仓库页面手动新建名为 申请 的标签" ;;
    *)   warn "创建标签返回 $MKCODE，请手工确认" ;;
  esac
else
  ok "标签 申请 已存在"
fi

# -----------------------------------------------------------------------------
# 汇总
# -----------------------------------------------------------------------------
stage "配置完成 — 汇总"

USR_AFTER=$(docker exec postgres psql -U root -d new-api -t -A \
  -c "select count(*) from users where deleted_at is null;" 2>/dev/null || echo "?")
CH_AFTER=$(docker exec postgres psql -U root -d new-api -t -A \
  -c "select count(*) from channels;" 2>/dev/null || echo "?")
if [ "$USR_BEFORE" = "$USR_AFTER" ] && [ "$CH_BEFORE" = "$CH_AFTER" ]; then
  ok "生产数据未变动（live_users=$USR_AFTER  channels=$CH_AFTER）"
else
  warn "数据发生变化！live_users $USR_BEFORE→$USR_AFTER，channels $CH_BEFORE→$CH_AFTER"
fi

cat <<SUMMARY

  ✅ Stage 1  QQ 依赖校验    $GATEWAY_VENV
                              aiohttp / httpx 已就绪（脚本不会擅自安装）
  ✅ Stage 2  QQ Bot 凭据    $HERMES_HOME/.env（600，已备份 .env.bak.*）
                              当前平台：${PLATFORMS:-读取失败}
  ✅ Stage 3  GitHub PAT    $NEB_DIR/github.env（600）

  ── 关于 GitHub 出网 ────────────────────────────────────────────────
  审核机器人只用 api.github.com（直连可用），不需要 github.com。
  GitHub OAuth 保持关闭状态——它需要 client_secret 和用户 token
  经过不可信的免费代理，这个代价不值得。

  ── 你现在需要做的 ────────────────────────────────────────────────
  1. 在 QQ 里给机器人发一条消息，确认能收到并回复。
     无响应就看：journalctl --user -u hermes-gateway -f
  2. 部署审核机器人（本次已一并交付）：
       install -m 755 nebula-review-bot.py /opt/nebula-zero/nebula-review-bot.py
       install -m 600 nebula-review-bot.env /root/.nebula-zero/bot.env
       systemctl enable --now nebula-review-bot
  3. 仓库里要有的 issue 模板与打标签工作流，本地推上去。

  ── 回滚方法 ──────────────────────────────────────────────────────
  QQ 凭据：从 $HERMES_HOME/.env 删除 QQ_* 行后执行
          systemctl --user -M admin@ restart hermes-gateway
          （或直接 cp .env.bak.* 覆盖回来）
  PAT：    rm -f $NEB_DIR/github.env

SUMMARY