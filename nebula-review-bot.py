***REMOVED***!/usr/bin/env python3
***REMOVED*** -*- coding: utf-8 -*-
"""
Nebula-Zero member自助开户审核机器人
=====================================

流水线
    member在 GitHub 仓库提 issue（用 account-application.yml 模板）
      -> 自动打标签「申请」
      -> 机器人发现新申请，通过 QQ 通知管理员
      -> 管理员在 issue 里回复含 approve / reject 的评论
      -> approve: 建号 + 发含账号密码的邮件 + 删评论 -> 关闭 -> 锁定
      -> reject : 发拒绝邮件 + 删评论 -> 关闭 -> 锁定

设计约束（都是实测得来的，不要随便改）
    * GitHub 只提供 api.github.com 这条路可用，github.com 不通。本程序只用 REST API。
    * GitHub 官方 API 没有"删除 issue"的能力，所以按用户要求走
      「删评论 -> 关闭 -> 锁定」三步。
    * new-api 没有"给任意邮箱发任意邮件"的对外 HTTP 接口
      （common/email.go:78 SendEmail 仅内部调用；对外只有发验证码的
      GET /api/verification），所以邮件必须用 smtplib 直连同一 SMTP 账号。
    * new-api 建号接口 POST /api/user/ 必须带尾斜杠，否则 307 重定向。
    * new-api admin API 需要 Authorization: Bearer <access_token> +
      New-Api-User: 1 两个头，access_token 约 1 小时过期，需要自动重新登录。

配置见同目录的 nebula-review-bot.env（或用 -c 指定）。
"""

import argparse
import json
import logging
import os
import random
import re
import secrets
import shutil
import signal
import smtplib
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

LOG = logging.getLogger("nebula-bot")

***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 配置
***REMOVED*** --------------------------------------------------------------------------
CONF = {}


def load_conf(path):
    """读 KEY=VALUE 文件。值里的 ***REMOVED*** 不当注释（授权码可能含 ***REMOVED***）。"""
    conf = {}
    if not os.path.exists(path):
        raise SystemExit("配置文件不存在：%s" % path)
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("***REMOVED***"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    return conf


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** GitHub REST API
***REMOVED*** --------------------------------------------------------------------------
class GitHub:
    def __init__(self, token, repo, api="https://api.github.com"):
        self.token = token
        self.repo = repo
        self.api = api.rstrip("/")

    ***REMOVED*** GitHub 的 5xx / 429 都是瞬时的，重试即可。放在传输层统一处理，
    ***REMOVED*** 比在每个调用点包 try/except 可靠 —— 漏一处就是整条流水线断掉。
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def _req(self, method, path, body=None, timeout=25, retries=4):
        url = "%s/repos/%s%s" % (self.api, self.repo, path)
        data = None
        headers = {
            "Authorization": "Bearer %s" % self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nebula-zero-review-bot",
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        last = (0, "未执行")
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return resp.status, None
                    try:
                        return resp.status, json.loads(raw)
                    except ValueError:
                        return resp.status, raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    code, payload = e.code, json.loads(raw)
                except Exception:
                    code, payload = e.code, raw.decode("utf-8", "replace")[:400]
                last = (code, payload)
                if code not in self.RETRY_STATUS or attempt == retries:
                    return code, payload
            except Exception as e:
                ***REMOVED*** 网络层失败（DNS/超时/连接重置）
                last = (0, str(e))
                if attempt == retries:
                    return 0, str(e)

            ***REMOVED*** 指数退避 + 抖动，上限 30s。顺带尊重 Retry-After（若带）
            wait = min(30, 2 ** attempt) + random.random()
            LOG.warning("GitHub %s %s 返回 %s，%.1fs 后重试（%d/%d）",
                        method, path, last[0], wait, attempt + 1, retries)
            time.sleep(wait)
        return last

    ***REMOVED*** -- 读取 ------------------------------------------------------------
    def issues(self, state="all", since=None, per_page=100):
        q = "?state=%s&per_page=%d&sort=created&direction=asc" % (state, per_page)
        if since:
            q += "&since=%s" % urllib.parse.quote(since)
        code, data = self._req("GET", "/issues" + q)
        if code != 200:
            raise RuntimeError("拉取 issues 失败 HTTP %s: %s" % (code, data))
        ***REMOVED*** GitHub 的 issues 列表包含 PR，这里只要 issue
        return [i for i in data if "pull_request" not in i]

    def issue_comments(self, number, per_page=100):
        code, data = self._req(
            "GET", "/issues/%d/comments?per_page=%d" % (number, per_page))
        if code != 200:
            raise RuntimeError("拉取评论失败 HTTP %s: %s" % (code, data))
        return data

    ***REMOVED*** -- 写入 ------------------------------------------------------------
    def add_labels(self, number, labels):
        return self._req("POST", "/issues/%d/labels" % number, {"labels": labels})

    def add_comment(self, number, body):
        return self._req("POST", "/issues/%d/comments" % number, {"body": body})

    def delete_comment(self, comment_id):
        return self._req("DELETE", "/issues/comments/%d" % comment_id)

    def set_state(self, number, state):
        """state: open / closed"""
        return self._req("PATCH", "/issues/%d" % number, {"state": state})

    def lock(self, number, reason):
        """reason: resolved / off-topic。GitHub 只接受这两个枚举值。"""
        return self._req(
            "PUT", "/issues/%d/lock" % number,
            {"lock_reason": reason, "message": "此申请已处理完毕，issue 已关闭并锁定。"})

    def ensure_label(self, name, color="0e8a16", desc=""):
        code, _ = self._req("GET", "/labels/" + urllib.parse.quote(name))
        if code == 200:
            return True
        code, data = self._req("POST", "/labels",
                               {"name": name, "color": color, "description": desc})
        if code == 201:
            return True
        if code == 422:      ***REMOVED*** 已存在（并发情况）
            return True
        LOG.warning("创建标签 %s 失败 HTTP %s: %s", name, code, data)
        return False


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** new-api 客户端（建号）
***REMOVED*** --------------------------------------------------------------------------
class NewAPI:
    def __init__(self, base, admin_user, admin_pass):
        self.base = base.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.token = None
        self.user_id_header = None

    def _req(self, method, path, body=None, timeout=25):
        url = self.base + path
        data = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if self.user_id_header:
            headers["New-Api-User"] = self.user_id_header
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw.decode("utf-8", "replace")[:400]
        except Exception as e:
            return 0, str(e)

    def login(self):
        """POST /api/user/login —— 注意这个接口不带尾斜杠才正常。

        成功响应（controller/user.go:199-214 writeLoginResponse）：
            {"message":"","success":true,
             "data":{"access_token":..., "token_type":...,
                     "access_expires_at":..., "session":{...},
                     "user":{...}}}
        注意 data.id 不存在，用户 id 在 data.user.id。
        """
        code, data = self._req("POST", "/api/user/login", {
            "username": self.admin_user,
            "password": self.admin_pass,
        })
        if code != 200 or not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("管理员登录失败 HTTP %s: %s" % (code, data))
        d = data.get("data") or {}
        self.token = d.get("access_token")
        self.user_id_header = str(
            (d.get("user") or {}).get("id") or d.get("id") or "")
        if not self.token:
            raise RuntimeError("登录响应里没有 access_token")
        LOG.info("new-api 登录成功（用户 id=%s）", self.user_id_header)

    def ensure_token(self):
        if not self.token:
            self.login()

    def create_user(self, username, password, email, quota, role=1):
        """POST /api/user/ —— 尾斜杠必须保留，否则 307。

        controller/user.go:998 CreateUser：
          · 直接 DecodeJson 成 model.User，所以字段名就是数据库列名
          · 校验 Username/Password 非空 + Validate + IsValidateRole
          · 要求 user.Role < 当前操作者的 role（admin 是 root，role=1 安全）
          · 只把 Username/Password/DisplayName/Role 写进 cleanUser，
            Email 单独走 withNormalizedEmailLock → ensureEmailAvailableWithTx，
            重复邮箱会返回 "邮箱地址已被使用"
          · 成功响应 {"success":true,"message":""}，失败走 common.ApiErrorT
        model/user.go:681 InsertWithTx 会用 common.QuotaForNewUser 覆盖传入的
        quota，所以 NEW_USER_QUOTA 只是兜底，实际值以系统设置为准。
        """
        self.ensure_token()
        body = {
            "username": username,
            "password": password,
            "email": email,
            "quota": quota,
            "role": role,
        }
        code, data = self._req("POST", "/api/user/", body)
        if code == 401 or (isinstance(data, dict) and
                           data.get("message") in ("无权进行此操作",
                                                   "AUTH_TOKEN_EXPIRED")):
            LOG.info("access_token 可能过期，重新登录后重试一次")
            self.login()
            code, data = self._req("POST", "/api/user/", body)
        return code, data

    def find_user_by_name(self, username):
        """按用户名查是否存在账号（含软删行）。找不到返回 None。

        model/user.go SearchUsers 用 `tx.Unscoped()` 查，所以软删的账号
        也会被搜到 —— 这正好符合我们要的判断：软删不等于注销掉名字，
        uni_users_username 是全表唯一约束，名字还占着。

        两个实测出来的字段/参数名，别改：
          · 分页参数是 p / page_size（common/page_info.go:44,47），
            不是 page / size —— 写错会被当成默认值 1，结果照样能用但
            容易误以为筛选没生效；
          · 软删时间序列化成 `DeletedAt`（大写 D），gorm.DeletedAt
            没有 json tag，走的是字段名本身。
        """
        self.ensure_token()
        q = urllib.parse.urlencode({"keyword": username, "p": 1,
                                     "page_size": 20})
        code, data = self._req("GET", "/api/user/search?" + q)
        if code != 200 or not isinstance(data, dict):
            LOG.warning("查询用户 %s 失败 HTTP %s: %s", username, code, data)
            return None
        items = data.get("data") or {}
        if not isinstance(items, dict):
            return None
        rows = items.get("items") or []
        for u in rows:
            ***REMOVED*** keyword 是 LIKE 模糊匹配，必须精确比对 username
            if (u.get("username") or "") == username:
                return u
        return None

    def revoke_user(self, user_id):
        """把已存在的账号重置密码 —— 用于「重复申请」场景。

        controller/user.go UpdateUser 在改密码时会走
        requireAdminUserProof(...AdminUserUpdate)，需要 X-Security-Proof，
        机器人拿不到 —— 所以这条路径走不通，改名反而更干脆。
        """
        return None


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 邮箱补写（new-api 的 admin API 不支持）
***REMOVED*** --------------------------------------------------------------------------
class EmailPatch:
    """new-api 建号 API 落不了邮箱，这是被源码堵死的：

        controller/user.go:1032  CreateUser 构造 cleanUser 时只抄
            Username / Password / DisplayName / Role 四个字段，Email 连结构体
            都进不去；
        model/user.go  User.EditWithTx 的 updates map 只有
            username / display_name / group / remark，同样没有 email，
        所以 UpdateUser 也补不上 —— admin 侧根本没有能写 email 的接口。

    账号本身不受影响（用户名就是邮箱），但 users.email 为空意味着该账号
    绑不了邮箱、收不到任何系统通知邮件。用户选择了让机器人补写。

    实现走 `docker exec postgres psql` 而不是 psycopg2：主机 python3 没有
    装驱动，postgres 容器也没把 5432 暴露到宿主机（127.0.0.1:5432 被拒），
    而 bot 服务跑在 ProtectSystem=strict 沙箱里装不了包。实测该沙箱下
    docker exec 可用。

    参数一律走 psql 变量 + :'var' 字面量引用（不是字符串拼接），
    psql 会把值里的单引号转义掉，注入不了。
    """

    ***REMOVED*** 只允许改这一个字段；表名/字段名写死，不接受外部输入
    SQL = ("UPDATE users SET email = :'em' "
           "WHERE username = :'un' AND deleted_at IS NULL "
           "AND (email IS NULL OR email = '');")

    def __init__(self, container="postgres", db_user="root", db_name="new-api"):
        self.container = container
        self.db_user = db_user
        self.db_name = db_name
        self.enabled = bool(shutil.which("docker"))

    def _psql(self, sql, **vars_):
        cmd = ["docker", "exec", "-i", self.container,
               "psql", "-U", self.db_user, "-d", self.db_name,
               "-t", "-A", "-v", "ON_ERROR_STOP=1"]
        for k, v in vars_.items():
            cmd += ["-v", "%s=%s" % (k, v)]
        ***REMOVED*** SQL 走 stdin：psql 的 :'var' 变量替换只在 stdin 路径生效，
        ***REMOVED*** 放在 -c 里会原样输出 ":'var'" 从而语法错误。
        p = subprocess.run(cmd, input=sql, capture_output=True,
                           text=True, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def backfill(self, username, email):
        """把建出来的账号的 users.email 补上。返回 (成功, 详情)。"""
        if not self.enabled:
            return False, "主机没有 docker 命令，跳过邮箱补写"
        if not email or "@" not in email:
            return False, "邮箱格式不合法，拒绝写入"
        email = email.strip().lower()      ***REMOVED*** 对齐 model.NormalizeEmail
        try:
            rc, out, err = self._psql(self.SQL, em=email, un=username.strip())
        except subprocess.TimeoutExpired:
            return False, "psql 执行超时"
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)
        if rc != 0:
            return False, "psql 退出码 %s：%s" % (rc, err[:200])
        ***REMOVED*** psql 默认回显命令本身，输出形如 "UPDATE 1"，不是裸数字
        m = re.search(r"UPDATE\s+(\d+)", out)
        n = int(m.group(1)) if m else 0
        if n == 0:
            ***REMOVED*** 已经有人占了这个邮箱，或者用户名没匹配上 —— 都不能当成成功
            return False, "未更新任何行（邮箱已被占用或用户名不存在）"
        return True, "已补写 %d 行" % n


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** QQ 通知（走 Hermes gateway 的 cron 投递通道）
***REMOVED*** --------------------------------------------------------------------------
class QQNotify:
    """Hermes 的主动推送靠 cron 任务上的 deliver 字段（scheduler_delivery.py:56
    把 qqbot 映射到 QQBOT_HOME_CHANNEL）。所以这里直接往 Hermes 的 jobs.json
    里追加一个一次性任务，由 gateway 消费后推送到 QQ。"""

    def __init__(self, hermes_home, admin_user, openid):
        self.jobs = os.path.join(hermes_home, "cron", "jobs.json")
        self.admin_user = admin_user
        self.openid = openid

    def send(self, text):
        try:
            return self._append_job(text)
        except Exception as e:
            LOG.error("QQ 通知失败：%s", e)
            return False

    def _append_job(self, text):
        import fcntl          ***REMOVED*** Linux 专用；机器人本来就跑在服务器上
        os.makedirs(os.path.dirname(self.jobs), exist_ok=True)

        with open(self.jobs, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read().strip()
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = data.get("jobs", [])

            job = {
                "id": "nebula-" + secrets.token_hex(4),
                "name": "Nebula-Zero 申请通知",
                "created_at": int(time.time()),
                "schedule": "manual",          ***REMOVED*** 只投递一次，不做周期
                "deliver": "qqbot",
                "no_agent": True,              ***REMOVED*** 纯文本直发，不消耗 agent
                "message": text,
                "one_shot": True,
            }
            data.append(job)
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)

        LOG.info("已投递 QQ 通知（job id=%s）", job["id"])
        return True


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 邮件
***REMOVED*** --------------------------------------------------------------------------
class Mailer:
    def __init__(self, host, port, account, token, sender, use_ssl=True):
        self.host = host
        self.port = int(port)
        self.account = account
        self.token = token
        self.sender = sender or account
        self.use_ssl = use_ssl

    def send(self, to_addr, subject, body):
        """new-api 的 common/email.go:44-71 对 465 端口走隐式 TLS（tls.Dial），
        这里保持一致。"""
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("Nebula-Zero", "utf-8")), self.sender))
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)

        if self.use_ssl:
            srv = smtplib.SMTP_SSL(self.host, self.port, timeout=25)
        else:
            srv = smtplib.SMTP(self.host, self.port, timeout=25)
            srv.starttls()
        try:
            srv.login(self.account, self.token)
            srv.sendmail(self.sender, [to_addr], msg.as_string())
        finally:
            try:
                srv.quit()
            except Exception:
                pass


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 解析申请 issue
***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** GitHub 不提供"用了哪个模板"的接口，所以靠 body 里的标记串识别。
TEMPLATE_MARK = "<!-- application-template: account-application -->"
***REMOVED*** YAML 模板渲染出来的表单长这样：
***REMOVED***   ***REMOVED******REMOVED******REMOVED*** 申请邮箱
***REMOVED***   someone@example.edu
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
***REMOVED*** 模板渲染出来是 "***REMOVED******REMOVED******REMOVED*** QQ号\n123456789" 这种形式（标记与值之间可能有换行），
***REMOVED*** 所以 \s* 必须能吃掉换行，否则标准模板反而提不出 QQ 号。
QQ_RE = re.compile(r"(?:QQ|qq|扣扣)\s*(?:号)?\s*[:：]?\s*(\d{5,12})")


def parse_application(body):
    """从 issue 正文里提取邮箱与 QQ 号。

    返回 (email, qq) —— 解析不到 email 时 email 为 None，此时应拒绝而不是
    猜测，因为建号用户名就是邮箱，猜错等于建出一个错账号。
    """
    if TEMPLATE_MARK not in (body or ""):
        LOG.info("issue 正文没有模板标记串，跳过（多半是普通提问）")
        return None, None

    emails = EMAIL_RE.findall(body or "")
    ***REMOVED*** 排除模板自带的示例地址
    emails = [e for e in emails if "example.com" not in e.lower()]
    email = emails[0] if emails else None

    m = QQ_RE.search(body or "")
    qq = m.group(1) if m else None
    return email, qq


def gen_password(length=16):
    """生成随机密码。避开容易混淆的字符，省得人抄错。"""
    alphabet = string.ascii_letters + string.digits
    tricky = set("l1IO0")
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length))
        if not (set(p) & tricky) and any(c.isdigit() for c in p) \
                and any(c.isupper() for c in p) and any(c.islower() for c in p):
            return p


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 决定：管理员评论里带关键字
***REMOVED*** --------------------------------------------------------------------------
APPROVE_WORDS = ("approve", "批准", "通过", "同意")
REJECT_WORDS = ("reject", "拒绝", "驳回", "不同意")


def decide(text):
    """返回 'approve' / 'reject' / None。

    只看管理员自己发的评论（body 为机器人发的除外）。reject 优先判定，
    避免一句里同时出现两个词时误批。
    """
    t = (text or "").lower()
    if any(w in t for w in REJECT_WORDS):
        return "reject"
    if any(w in t for w in APPROVE_WORDS):
        return "approve"
    return None


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 处理单个 issue
***REMOVED*** --------------------------------------------------------------------------
def finalize(gh, number, keep_note=""):
    """用户要求的收尾顺序：先删评论 -> 再关闭 -> 最后锁定。

    顺序有讲究：锁定后不能再加评论，所以必须先把评论删干净；
    关闭放在锁定前面，保证锁定时 issue 已处于 closed 状态。
    """
    try:
        comments = gh.issue_comments(number)
    except Exception as e:
        LOG.warning("拉取 ***REMOVED***%d 评论失败：%s", number, e)
        comments = []

    ***REMOVED*** 留一条说明，管理员还能看到发生了什么；其余评论全部清掉
    body = ["***REMOVED******REMOVED*** 申请已处理完毕", ""]
    if keep_note:
        body.append(keep_note)
    body += ["", "依据本仓库使用规范，申请流程结束后关闭并锁定本 issue。" ]
    gh.add_comment(number, "\n".join(body))

    removed = 0
    for c in comments:
        cid = c.get("id")
        code, _ = gh.delete_comment(cid)
        if code in (204, 200):
            removed += 1
        else:
            LOG.warning("删除评论 %d 失败 HTTP %s", cid, code)
    LOG.info("***REMOVED***%d 已删除 %d/%d 条评论", number, removed, len(comments))

    code, _ = gh.set_state(number, "closed")
    LOG.info("***REMOVED***%d 关闭 → HTTP %s", number, code)

    code, data = gh.lock(number, "resolved")
    LOG.info("***REMOVED***%d 锁定 → HTTP %s %s", number, code, "" if code in (204, 200) else data)


def handle(gh, api, mailer, qq, cfg, issue, pre_existing=False, patcher=None):
    """pre_existing=True 表示这条申请在机器人启动前就已存在。

    这种单子照样要进等待循环 —— 管理员可能已经回复了 approve，只是机器人在
    启动那一刻才上线。只有"通知"这一步要跳过，避免重启风暴反复骚扰。
    """
    number = issue["number"]
    body = issue.get("body") or ""
    author = (issue.get("user") or {}).get("login", "?")
    title = issue.get("title", "")

    LOG.info("处理 issue ***REMOVED***%d（%s）：%s%s",
             number, author, title, "（启动前已存在）" if pre_existing else "")

    email, user_qq = parse_application(body)
    if not email:
        LOG.warning("***REMOVED***%d 正文里解析不到邮箱，标记为无法自动处理", number)
        finalize(gh, number,
                 "未能从申请正文中识别出**申请邮箱**，因此无法自动开户。\n\n"
                 "请用仓库提供的申请模板重新提交，并确保填写了有效邮箱。")
        return

    ***REMOVED*** 自动挂上「申请」标签，方便管理员筛
    gh.add_labels(number, [cfg["label"]])

    ***REMOVED*** 通知管理员
    lines = [
        "【Nebula-Zero】收到新的开户申请",
        "",
        "Issue   : ***REMOVED***%d %s" % (number, title),
        "申请人  : @%s" % author,
        "申请邮箱: %s" % email,
        "QQ号    : %s" % (user_qq or "（未填）"),
        "链接    : %s/issues/%d" % (cfg["repo_url"], number),
        "",
        "处理方式：在该 issue 下回复含 approve 或 reject 的评论即可。",
    ]
    if pre_existing:
        lines.insert(1, "（本单在机器人启动前已提交）")
    qq.send("\n".join(lines))

    ***REMOVED*** 等管理员决定
    LOG.info("***REMOVED***%d 已通知管理员，进入等待状态（每 %s 秒轮询一次）",
             number, cfg["poll_seconds"])

    ***REMOVED*** 只把机器人自己发的那条通知评论标记为已读，其余评论一律视为待处理。
    ***REMOVED*** 不能把进入循环时已存在的所有评论都标记已读 —— GitHub 通知有延迟，
    ***REMOVED*** 管理员完全可能在机器人发现 issue 之前就已经回复了 approve，
    ***REMOVED*** 那样会被当成"旧评论"永久跳过（实测踩过：评论早到 17 秒就丢了）。
    seen = set()
    try:
        seen = {c["id"] for c in gh.issue_comments(number)
                if (c.get("user") or {}).get("login", "").endswith("[bot]")}
    except Exception:
        ***REMOVED*** 拉不到就退化成"什么都不算已读"，最坏结果只是重读一次自己的通知评论，
        ***REMOVED*** 不会漏掉管理员的决定。
        LOG.warning("***REMOVED***%d 初始化已读评论集合失败，退化为空集合", number,
                    exc_info=True)
    while True:
        time.sleep(cfg["poll_seconds"])
        try:
            issue_now = next((i for i in gh.issues("all", per_page=100)
                              if i["number"] == number), None)
        except Exception as e:
            LOG.warning("重新拉取 ***REMOVED***%d 失败：%s", number, e)
            issue_now = None

        if issue_now is None:
            LOG.info("***REMOVED***%d 已不存在（可能被手动删除），放弃跟踪", number)
            return
        if issue_now.get("state") == "closed":
            LOG.info("***REMOVED***%d 已被管理员手动关闭，放弃跟踪", number)
            return

        try:
            comments = gh.issue_comments(number)
        except Exception as e:
            LOG.warning("拉取 ***REMOVED***%d 评论失败：%s", number, e)
            continue

        verdict = None
        for c in comments:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            ***REMOVED*** 机器人自己的通知评论不算决策
            if (c.get("user") or {}).get("login", "").endswith("[bot]"):
                continue
            verdict = decide(c.get("body"))
            LOG.info("***REMOVED***%d 收到新评论（@%s）→ 判定 %s",
                     number, (c.get("user") or {}).get("login", "?"), verdict or "无关键字")
            if verdict:
                break

        if not verdict:
            continue

        ***REMOVED*** ---- 决策落地 ----
        if verdict == "reject":
            try:
                mailer.send(email, "【Nebula-Zero】您的开户申请未通过",
                            reject_email_body(cfg, title))
                LOG.info("***REMOVED***%d 拒绝邮件已发往 %s", number, email)
            except Exception as e:
                LOG.error("***REMOVED***%d 拒绝邮件发送失败：%s", number, e)
                qq.send("【Nebula-Zero】***REMOVED***%d 拒绝邮件发送失败：%s" % (number, e))
            finalize(gh, number, "**结果：未通过**\n\n已向 %s 发送结果通知邮件。" % email)
            return

        ***REMOVED*** approve —— 先建号，再发信；建号失败绝不能发信，否则用户拿到的是无效账号

        ***REMOVED*** 名字已经存在怎么办？两种情况要分开：
        ***REMOVED***   · 账号还活着（deleted_at 为空）→ 直接把新密码发给本人，这是
        ***REMOVED***     "重置密码"，member重复提交 issue 时最该发生的行为；
        ***REMOVED***   · 账号已软删 → uni_users_username 是全表唯一约束，名字仍被占着，
        ***REMOVED***     重新申请必然失败。此时给名字加一次性后缀，并在邮件里说明。
        pw = gen_password()
        LOG.info("***REMOVED***%d 批准，建号中：username=%s", number, email)
        username = email            ***REMOVED*** 默认用户名就是邮箱本身
        existing = api.find_user_by_name(email)
        if existing is not None:
            if not existing.get("DeletedAt"):
                LOG.info("***REMOVED***%d 该邮箱已有活跃账号（id=%s），改为重置密码",
                         number, existing.get("id"))
                ***REMOVED*** UpdateUser 改密码需要 X-Security-Proof，机器人拿不到，
                ***REMOVED*** 所以只能提示管理员手工处理，而不是假装成功。
                qq.send("【Nebula-Zero】***REMOVED***%d 邮箱 %s 已有活跃账号（id=%s），"
                        "本次申请不重复建号。\n如需重置密码，请手工在后台操作。"
                        % (number, email, existing.get("id")))
                finalize(gh, number,
                         "**结果：该邮箱已开户**\n\n"
                         "`%s` 已存在有效账号，本次申请未重复创建。\n\n"
                         "如需重置密码，请联系管理员。" % email)
                return

            ***REMOVED*** 软删账号：名字还被占着，换一个新用户名重开。
            ***REMOVED*** model/user.go:81 Username 是 validate:"max=20"，而registered邮箱
            ***REMOVED*** "20231234567890@example.edu" 本身就 26 个字符，超长的申请人在
            ***REMOVED*** 正常路径上就会失败 —— 候选链按长度从长到短试，都塞不下就放弃。
            base, _, dom = email.partition("@")
            for cand in ("%s+n%s@%s" % (base, number, dom),
                         "%s+%s@%s" % (base, str(number)[:4], dom),
                         "neb%d@%s" % (number, dom)):
                if len(cand) <= 20:
                    username = cand
                    break
            if username == email:
                ***REMOVED*** 没有任何候选能压进 20 字符 —— 只能放弃，让管理员手工建号
                LOG.error("***REMOVED***%d 原用户名 %s 已超 20 字符上限且无法改名",
                          number, email)
                qq.send("【Nebula-Zero】***REMOVED***%d 邮箱 %s 长度超过系统 20 字符上限，"
                        "且该名字已被注销账号占用，无法自动建号，请手工处理。"
                        % (number, email))
                finalize(gh, number,
                         "**结果：批准，但自动建号失败**\n\n"
                         "邮箱 `%s` 已超过系统用户名 20 字符上限，"
                         "且该名字被一个已注销的账号占用，请管理员手工创建。" % email)
                return

            LOG.warning("***REMOVED***%d %s 已被软删账号占用，改用 %s 重新开户",
                        number, email, username)
            qq.send("【Nebula-Zero】***REMOVED***%d 注意：%s 曾被一个已注销的账号占用，"
                    "已改用 %s 作为新用户名（绑定邮箱仍是 %s）。"
                    % (number, email, username, email))

        code, data = api.create_user(
            username=username, password=pw, email=email,
            quota=int(cfg["quota"]), role=int(cfg["role"]))

        ***REMOVED*** 建号失败就到此为止 —— 绝不能继续发信，否则用户拿到的是无效账号
        if code not in (200, 201) or (isinstance(data, dict) and
                                      data.get("success") is False):
            LOG.error("***REMOVED***%d 建号失败 HTTP %s: %s", number, code, data)
            qq.send("【Nebula-Zero】***REMOVED***%d 建号失败 HTTP %s，请人工处理：%s"
                    % (number, code, data))
            finalize(gh, number,
                     "**结果：批准，但自动建号失败**\n\n"
                     "错误：`HTTP %s` `%s`\n\n管理员已收到告警，请手工创建账号。"
                     % (code, data))
            return

        LOG.info("***REMOVED***%d 建号成功，用户名=%s", number, username)

        ***REMOVED*** new-api 建号 API 落不了邮箱（CreateUser/EditWithTx 都不写这个字段），
        ***REMOVED*** 这里补一道 SQL。补写是锦上添花，账号已经建好、凭据已确定，
        ***REMOVED*** 所以任何异常都必须吞掉，绝不能阻断后面的发信与收尾。
        if patcher is not None:
            try:
                ok, detail = patcher.backfill(username, email)
                if ok:
                    LOG.info("***REMOVED***%d 邮箱已补写入库：%s", number, detail)
                else:
                    LOG.warning("***REMOVED***%d 邮箱补写失败：%s", number, detail)
                    qq.send("【Nebula-Zero】***REMOVED***%d 账号已创建，但 users.email 补写失败：%s\n"
                            "用户名 %s 仍可正常登录，只是收不到系统通知邮件。"
                            % (number, detail, username))
            except Exception:
                LOG.exception("***REMOVED***%d 邮箱补写异常（已忽略，不影响开户）", number)

        try:
            mailer.send(email, "【Nebula-Zero】您的账号已开通",
                        approve_email_body(cfg, username, pw, email))
            LOG.info("***REMOVED***%d 开户邮件已发往 %s", number, email)
            note = ("**结果：已通过**\n\n"
                    "已创建账号 `%s` 并向 %s 发送开户邮件（含初始密码）。"
                    % (username, email))
        except Exception as e:
            LOG.error("***REMOVED***%d 开户邮件发送失败：%s", number, e)
            ***REMOVED*** 账号已建但邮件没发出去——必须告诉管理员密码，否则用户永远拿不到
            qq.send("【Nebula-Zero】***REMOVED***%d 账号已创建但邮件发送失败：%s\n"
                    "用户名 %s，初始密码 %s —— 请手工把凭据发给申请人。"
                    % (number, e, username, pw))
            note = ("**结果：已通过，但邮件发送失败**\n\n"
                    "账号已创建（用户名 `%s`），但通知邮件未能送达。\n\n"
                    "管理员已收到含初始密码的告警，请手工转发凭据。"
                    % username)

        finalize(gh, number, note)
        return


def approve_email_body(cfg, username, password, email):
    extra = ""
    if username != email:
        extra = ("\n注意：%s 曾被一个已注销的账号占用，"
                 "因此你的登录用户名是下面这个，绑定邮箱仍是 %s。\n"
                 % (email, email))
    return (
        "%s 同学你好：\n\n"
        "你的 Nebula-Zero 账号已开通。\n\n"
        "登录地址 : %s\n"
        "用户名   : %s\n"
        "初始密码 : %s\n%s\n"
        "使用须知：\n"
        "  1. 首次登录后请立即在「个人设置」里修改密码。\n"
        "  2. 所有模型当前均为 0 元按量计费，你的账号额度不会因调用而扣减。\n"
        "  3. 创建自己的 API Key 后即可调用，接口地址同为上述站点地址 + /v1。\n"
        "  4. 上游为公开免费服务，偶发限流或临时不可用属正常现象，可稍后重试。\n\n"
        "如有任何问题，直接在本仓库开 issue 询问即可。\n\n"
        "—— Nebula-Zero 自动开户系统\n"
        % (username, cfg["site_url"], username, password, extra)
    )


def reject_email_body(cfg, title):
    return (
        "你好：\n\n"
        "很抱歉，你在 Nebula-Zero 提交的开户申请（%s）本次未能通过。\n\n"
        "常见原因：\n"
        "  · 申请邮箱不属于本站允许的注册域名\n"
        "  · 申请信息填写不完整\n"
        "  · 申请提交频率过于频繁\n\n"
        "如果你认为这是误判，欢迎在原 issue 下补充说明，或直接重新提交一份申请。\n\n"
        "—— Nebula-Zero 自动开户系统\n" % title
    )


***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** 主循环
***REMOVED*** --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Nebula-Zero 开户审核机器人")
    ap.add_argument("-c", "--config", default="/root/.nebula-zero/bot.env",
                    help="配置文件路径")
    ap.add_argument("--once", action="store_true",
                    help="只扫描一轮就退出（用于排查问题）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要做的动作，不调 GitHub 写接口、不建号、不发信")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    conf = load_conf(args.config)
    CONF.update(conf)

    gh = GitHub(conf["GITHUB_TOKEN"], conf["GITHUB_REPO"],
                 conf.get("GITHUB_API", "https://api.github.com"))
    api = NewAPI(conf["NEWAPI_BASE"], conf["NEWAPI_ADMIN_USER"],
                 conf["NEWAPI_ADMIN_PASS"])
    mailer = Mailer(conf["SMTP_HOST"], conf["SMTP_PORT"], conf["SMTP_ACCOUNT"],
                    conf["SMTP_TOKEN"], conf.get("SMTP_FROM") or conf["SMTP_ACCOUNT"],
                    use_ssl=conf.get("SMTP_SSL", "true").lower() == "true")
    qq = QQNotify(conf["HERMES_HOME"], conf["NEWAPI_ADMIN_USER"],
                  conf.get("QQ_HOME_CHANNEL", ""))

    ***REMOVED*** new-api 建号 API 落不了邮箱，靠这个补写。可用 BACKFILL_EMAIL=false 关掉。
    patcher = None
    if conf.get("BACKFILL_EMAIL", "true").lower() == "true":
        patcher = EmailPatch(
            conf.get("PG_CONTAINER", "postgres"),
            conf.get("PG_USER", "root"),
            conf.get("PG_DATABASE", "new-api"))
        LOG.info("邮箱补写：%s（容器=%s）",
                 "已启用" if patcher.enabled else "已禁用（无 docker 命令）",
                 patcher.container)
    else:
        LOG.info("邮箱补写：已通过 BACKFILL_EMAIL=false 关闭")

    handle_conf = {
        "label": conf.get("LABEL", "申请"),
        "repo_url": "https://github.com/%s" % conf["GITHUB_REPO"],
        "site_url": conf.get("SITE_URL", "http://YOUR_HOST:3000"),
        "quota": conf.get("NEW_USER_QUOTA", "500000000"),
        "role": conf.get("NEW_USER_ROLE", "1"),
        "poll_seconds": int(conf.get("POLL_SECONDS", "30")),
    }

    if args.dry_run:
        LOG.warning("dry-run 模式：不会建号、不会发信、不会修改 issue")
        try:
            items = gh.issues("open", per_page=100)
        except Exception as e:
            LOG.error("拉取 issue 失败：%s", e)
            return 1
        LOG.info("当前有 %d 个 open issue", len(items))
        for it in items:
            email, uqq = parse_application(it.get("body") or "")
            LOG.info("***REMOVED***%-4s %-40s 识别邮箱=%s QQ=%s",
                     it["number"], it.get("title", "")[:40], email or "无", uqq or "无")
        return 0

    gh.ensure_label(handle_conf["label"], "0e8a16", "member自助开户申请")
    LOG.info("机器人启动 · 仓库 %s · 标签「%s」 · 轮询 %ss",
             conf["GITHUB_REPO"], handle_conf["label"], handle_conf["poll_seconds"])

    stopping = {"v": False}

    def _bye(signum, _frame):
        LOG.info("收到信号 %s，准备退出", signum)
        stopping["v"] = True
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    ***REMOVED*** 记住处理过的 issue，避免重启后重复处理
    seen_file = os.path.join(os.path.dirname(args.config), ".bot_seen.json")
    seen = set()
    if os.path.exists(seen_file):
        try:
            with open(seen_file, encoding="utf-8") as f:
                seen = set(json.load(f))
        except Exception:
            seen = set()

    def persist():
        try:
            with open(seen_file, "w", encoding="utf-8") as f:
                json.dump(sorted(seen), f)
            os.chmod(seen_file, 0o600)
        except Exception as e:
            LOG.warning("写入 %s 失败：%s", seen_file, e)

    first = True
    while not stopping["v"]:
        try:
            for issue in gh.issues("open", per_page=100):
                n = issue["number"]
                if n in seen:
                    continue
                body = issue.get("body") or ""
                if TEMPLATE_MARK not in body:
                    seen.add(n)          ***REMOVED*** 不是申请，不归我们管
                    continue
                try:
                    handle(gh, api, mailer, qq, handle_conf, issue,
                           pre_existing=first, patcher=patcher)
                except Exception:
                    LOG.exception("处理 ***REMOVED***%d 时异常", n)
                seen.add(n)
                persist()
            first = False
            if not os.path.exists(seen_file):
                persist()
        except Exception as e:
            LOG.error("本轮扫描失败：%s", e)

        if args.once:
            break
        for _ in range(handle_conf["poll_seconds"]):
            if stopping["v"]:
                break
            time.sleep(1)

    LOG.info("机器人已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())