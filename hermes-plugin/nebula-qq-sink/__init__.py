"""Nebula-Zero QQ 审核指令入口（Hermes 插件）。

为什么要它
----------
管理员在手机上发「***REMOVED***9 approve」就能批准开户申请。他不需要（也不想）看到
Hermes 的任何回复 —— 那些回复既没用又把手机刷满。而 Hermes 默认会把每条
QQ 消息当成聊天请求交给 agent 处理，于是会真的去回一句。

所以这里在 ``pre_gateway_dispatch``（gateway 收到消息后、到达 agent 之前、
甚至在鉴权之前触发）把这类消息**拦下来**：

* 识别成功 → 把指令写进队列文件，返回 ``{"action": "skip"}`` 让 gateway
  丢弃这条消息。Hermes 不产生回复、不调 LLM、不写 state.db。
* 识别失败（闲聊、表情、别的平台的同形文本、没配过管理员）→ 返回 ``None``
  放行，Hermes 的行为跟没装这个插件一模一样。

与 nebula-review-bot 的关系
---------------------------
这个插件**只写队列、不做决策**，真正的建号/发信/关单都在机器人那边。
写入用 append-only 的 JSONL，配合 fcntl 文件锁做跨进程互斥（gateway 与
机器人是两个进程）。

never raises：任何异常都退化成放行，最坏结果是这一条指令被 Hermes 当聊天
处理，管理员再发一次即可 —— 绝不能因为插件的 bug 让 gateway 挂掉。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

***REMOVED*** 关键字表与 nebula-review-bot.py 的 APPROVE_WORDS / REJECT_WORDS 逐字一致。
***REMOVED*** 不在这里"顺手"加 ok / no / deny / 可以 之类的词：no 是 notebook 的子串、
***REMOVED*** 可以 会出现在闲聊里，两边判定标准一旦分叉就会出现"插件吞了但机器人不认"
***REMOVED*** 或者反过来的情况。宁可少认几种写法，也要保证与机器人同进同退。
APPROVE_WORDS = ("approve", "批准", "通过", "同意")
REJECT_WORDS = ("reject", "拒绝", "驳回", "不同意")

***REMOVED*** 数字用贪婪的 \d{1,9}：贪婪匹配才会把 "200000 approve" 的六位数字整个吃掉，
***REMOVED*** 再由 issue_range 判掉。若写成 \d{1,5}，"200000" 会被切成 20000 + "0 approve"，
***REMOVED*** 单号就凭空错了。
_CMD_RE = re.compile(r"^\s****REMOVED***?\s*(\d{1,9})\s*[:：\s,，、]?\s*(.+?)\s*$")

MAX_TEXT = 80


def _load_config() -> dict:
    path = _HERE / "config.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        logger.warning("nebula-qq-sink: 缺少 %s，未启用", path)
        return {}
    except Exception:
        logger.warning("nebula-qq-sink: 读取 %s 失败，未启用", path, exc_info=True)
        return {}


def _parse(text: str, cfg: dict):
    """返回 (issue_number, verdict) 或 None（不是审核指令）。

    与机器人侧 decide() 的优先级一致：**reject 优先**。所以
    「***REMOVED***9 approve 但还是 reject」按拒绝处理，不会误批。
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_TEXT:
        return None
    m = _CMD_RE.match(t)
    if not m:
        return None
    rest = m.group(2).lower()
    ***REMOVED*** 与 decide() 完全同序：先全句找拒绝词，再找批准词。
    ***REMOVED*** 用 in 而不是正则交替匹配，是因为后者只认"最先出现的那个词"，
    ***REMOVED*** 「***REMOVED***9 approve 但是还是 reject」会被 approve 先命中而误批。
    if any(w in rest for w in REJECT_WORDS):
        verdict = "reject"
    elif any(w in rest for w in APPROVE_WORDS):
        verdict = "approve"
    else:
        return None
    lo, hi = cfg.get("issue_range", [1, 100000])
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    if n < int(lo) or n > int(hi):
        return None
    return n, verdict


def _append_queue(queue_path: str, record: dict) -> None:
    """把一条指令追加进 JSONL 队列（flock 互斥，O_APPEND 不会写串行）。

    **只允许写队列文件所在目录**。目录写不进去（网关沙箱、权限、只读挂载）
    必须抛错让上层放行 —— 吞掉指令比让 Hermes 多回一句糟糕得多。
    makedirs 同样不行：目录存在但不可写时它返回 None 而不报错，只有真正
    open() 失败才算数。
    """
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(queue_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ***REMOVED*** os.write 可能只写进一部分（信号等原因），必须写完为止，
        ***REMOVED*** 否则队列里会留下半行 JSON。
        while data:
            data = data[os.write(fd, data):]
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _strip_mention(text: str) -> str:
    """去掉开头的 @机器人 / nebula 前缀，返回可被 _CMD_RE 解析的正文。

    群聊里调用者通常会写 "@机器人 ***REMOVED***9 approve" 或 "nebula ***REMOVED***9 approve"。
    群聊闸门要求出现 @ 或 nebula，指令解析又要求单号在开头 —— 不剥掉这个
    前缀，两条规则会互相把对方的合法消息拒掉。
    """
    t = (text or "").strip()
    for _ in range(2):                     ***REMOVED*** 最多剥两层："@bot @bot ***REMOVED***9 approve"
        m = re.match(r"^(?:@[^\s@]+\s*|nebula[\s:：,，]*)+", t, re.I)
        if not m or m.end() == len(t):
            break
        t = t[m.end():].strip()
    return t


def _on_pre_gateway_dispatch(hook_name=None, event=None, gateway=None, session_store=None, **kwargs):
    cfg = _load_config()
    if not cfg or cfg.get("enabled") is False:
        return None
    queue_path = cfg.get("queue_file") or ""
    if not queue_path:
        return None
    if event is None:
        return None

    ***REMOVED*** 只处理配置里列明的平台（目前是 qqbot）。
    platforms = {str(p).lower() for p in (cfg.get("platforms") or ["qqbot"])}
    try:
        plat = (event.source.platform.value if event.source else "") or ""
    except Exception:
        plat = ""
    if plat.lower() not in platforms:
        return None

    ***REMOVED*** 只认授权管理员，别人发的一律放行（不劫持别人的对话）。
    allowed = {str(u).strip().upper() for u in (cfg.get("admin_openids") or []) if str(u).strip()}
    uid = (event.user_id or (event.source.user_id if event.source else "") or "")
    if not allowed or str(uid).strip().upper() not in allowed:
        return None

    ***REMOVED*** 群聊里要求明确 @ 机器人才算，避免多人频道里被别人一句话误触发。
    ***REMOVED*** SessionSource.chat_type 的默认值是 "dm"，但别处会写 "private"，
    ***REMOVED*** 两个都当成私聊处理。
    chat_type = (event.source.chat_type if event.source else "dm") or "dm"
    if cfg.get("require_mention_in_group") and chat_type not in ("dm", "private"):
        text = (event.text or "").strip().lower()
        if "nebula" not in text and "@" not in text:
            return None

    parsed = _parse(_strip_mention(getattr(event, "text", "")), cfg)
    if parsed is None:
        return None                                    ***REMOVED*** 普通聊天，放行
    number, verdict = parsed

    try:
        _append_queue(queue_path, {
            "issue": number,
            "verdict": verdict,
            "text": (event.text or "").strip(),
            "platform": plat,
            "user_id": str(uid),
            "message_id": getattr(event, "message_id", None),
            "ts": time.time(),
        })
    except Exception:
        logger.warning("nebula-qq-sink: 写入指令队列失败（消息将放行给 Hermes）", exc_info=True)
        return None                                    ***REMOVED*** 写不进去就别吞，否则指令丢了

    logger.info("nebula-qq-sink: 拦截 ***REMOVED***%d %s，已入队，Hermes 不再回复", number, verdict)
    return {"action": "skip", "reason": "nebula-zero review command: ***REMOVED***%d %s" % (number, verdict)}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)