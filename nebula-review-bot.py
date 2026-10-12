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
import http.cookiejar
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
from email.mime.multipart import MIMEMultipart
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
    """new-api 的 dashboard 客户端。

    会话处理是这里唯一 tricky 的地方，机制全部来自源码：
        service/auth_session.go:63  createLoginSession 每次登录都新建一条
        user_sessions 记录，且 if activeCount >= common.UserSessionActiveLimit
        就返回 AUTH_SESSION_LIMIT（默认 50，common/constants.go:46，
        可用环境变量 USER_SESSION_ACTIVE_LIMIT 覆盖）。
        会话寿命 LoginSessionTTL 实测恒为 30 天（expires_at - created_at
        = 2592000），而 service/auth_cleanup.go 的清理任务只删「已过期」
        和「已撤销」的，不删「活跃但闲置」的 —— 所以反复调 /api/user/login
        会把自己的登录路慢慢堵死（实测撞过 HTTP 409 AUTH_SESSION_LIMIT）。

    对策是走 refresh 而不是重登：
        POST /api/user/auth/refresh（router/api-router.go:77）
        契约见 controller/auth_session.go:17 RefreshAuth：
            · refresh token 从 Cookie `new_api_refresh` 里取
              （service/auth_session.go:16 RefreshCookieName）
            · 可选 `X-Auth-Session` 头带 sid，不一致返回
              ErrLoginSessionMismatch
            · 成功返回 {"success":true,"data":{"access_token",...}}
            · service.RefreshLoginSession 只轮换 refresh secret
              （RotateUserSessionRefresh），不新建 user_sessions 行
            · 没有 cookie 时返回 401 AUTH_UNAUTHORIZED
        实测：带 cookie + X-Auth-Session 调一次，user_sessions 里活跃行数
        不变，access_token 换新且能正常调 admin 接口。

        登出是 POST /api/user/auth/logout（router/api-router.go:78）。
        注意不是 /api/user/logout 或 /api/user/self/logout，那两个是 404。
    """

    def __init__(self, base, admin_user, admin_pass, session_file=None):
        self.base = base.rstrip("/")
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.token = None
        self.user_id_header = None
        self.user_id = None
        self.sid = None
        ***REMOVED*** cookie jar 让 refresh 用的 new_api_refresh 能跨请求留存
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        ***REMOVED*** 跨进程重启复用同一条会话，否则重启一次就 +1 条活跃会话
        self.session_file = session_file
        self._load_session()

    ***REMOVED*** ---- 会话持久化 -------------------------------------------------
    def _load_session(self):
        if not self.session_file or not os.path.exists(self.session_file):
            return
        try:
            with open(self.session_file, encoding="utf-8") as f:
                st = json.load(f)
        except Exception as e:
            LOG.warning("读取会话文件 %s 失败：%s", self.session_file, e)
            return
        if st.get("base") != self.base:
            return
        self.sid = st.get("sid")
        self.user_id = st.get("user_id")
        self.user_id_header = str(self.user_id or "")
        for c in st.get("cookies") or []:
            try:
                self.jar.set_cookie(http.cookiejar.Cookie(
                    version=0, name=c["name"], value=c["value"], port=None,
                    port_specified=False, domain=c.get("domain", ""),
                    domain_specified=True, domain_initial_dot=False,
                    path=c.get("path", "/"), path_specified=True,
                    secure=False, expires=c.get("expires"),
                    discard=False, comment=None, comment_url=None,
                    rest={}, rfc2109=False))
            except Exception as e:
                LOG.warning("恢复 cookie %s 失败：%s", c.get("name"), e)
        LOG.info("复用已保存的会话 sid=%s", self.sid)

    def _save_session(self):
        if not self.session_file:
            return
        data = {
            "base": self.base,
            "sid": self.sid,
            "user_id": self.user_id,
            "cookies": [{"name": c.name, "value": c.value,
                         "domain": c.domain, "path": c.path,
                         "expires": c.expires} for c in self.jar],
        }
        tmp = self.session_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.session_file)
        except Exception as e:
            LOG.warning("写入会话文件 %s 失败：%s", self.session_file, e)

    def _absorb(self, data):
        """把登录/刷新响应里的 token 与 sid 收进来。"""
        d = data.get("data") or {}
        self.token = d.get("access_token")
        self.user_id = (d.get("user") or {}).get("id") or d.get("id")
        self.user_id_header = str(self.user_id or "")
        self.sid = (d.get("session") or {}).get("sid") or self.sid
        self._save_session()

    def _req(self, method, path, body=None, timeout=25, extra=None):
        url = self.base + path
        data = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if self.user_id_header:
            headers["New-Api-User"] = self.user_id_header
        if extra:
            headers.update(extra)
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
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

    def refresh(self):
        """用已有会话换新的 access_token —— 不新建 user_sessions 行。

        返回 True 表示 token 已刷新；False 表示没有可用会话或刷新失败，
        调用方应退回 login()。
        """
        if not self.sid:
            return False
        code, data = self._req("POST", "/api/user/auth/refresh", None,
                               extra={"X-Auth-Session": self.sid})
        if code != 200 or not isinstance(data, dict) or not data.get("success"):
            LOG.info("refresh 失败 HTTP %s：%s（退回重新登录）", code, data)
            ***REMOVED*** 会话可能已被撤销/过期，清掉避免下次又走这条路
            self.sid = None
            self.token = None
            self.jar.clear()
            self._save_session()
            return False
        self._absorb(data)
        LOG.info("已通过 refresh 换新 access_token（沿用会话 sid=%s）", self.sid)
        return True

    def login(self):
        """POST /api/user/login —— 注意这个接口不带尾斜杠才正常。

        成功响应（controller/user.go:199-214 writeLoginResponse）：
            {"message":"","success":true,
             "data":{"access_token":..., "token_type":...,
                     "access_expires_at":..., "session":{...},
                     "user":{...}}}
        注意 data.id 不存在，用户 id 在 data.user.id。

        409 AUTH_SESSION_LIMIT 的处置：new-api 给每个账号最多
        common.UserSessionActiveLimit（默认 50，common/constants.go:46，可用环境变量
        USER_SESSION_ACTIVE_LIMIT 覆盖）条「活跃会话」，而每次登录都新建一条
        user_sessions 记录（service/auth_session.go:63 createLoginSession），
        旧记录不因被新登录取代而撤销。机器人只要活过一天、每 30 秒轮询里
        碰到一次 token 过期就重新登录，累积几十条后会把自己的登录路堵死
        —— 实测撞到过 409，报错原文 `AUTH_SESSION_LIMIT`。

        所以：
          · 首选 refresh()（不新建会话行，也不受上限约束）；
          · refresh 用不了才 login()；
          · login 仍撞 409 时先登出旧会话再重试一次，仍不行才算真失败。
        登出走 POST /api/user/auth/logout（router/api-router.go:78，注意不是
        /api/user/logout 或 /api/user/self/logout，那两个是 404）。
        """
        code, data = self._req("POST", "/api/user/login", {
            "username": self.admin_user,
            "password": self.admin_pass,
        })
        if code == 409 and isinstance(data, dict) and \
                data.get("code") == "AUTH_SESSION_LIMIT":
            LOG.warning("登录撞到会话上限（%s），先登出旧会话再重试一次", data.get("message"))
            self._logout()
            code, data = self._req("POST", "/api/user/login", {
                "username": self.admin_user,
                "password": self.admin_pass,
            })
        if code != 200 or not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("管理员登录失败 HTTP %s: %s" % (code, data))
        if not (data.get("data") or {}).get("access_token"):
            raise RuntimeError("登录响应里没有 access_token")
        self._absorb(data)
        LOG.info("new-api 登录成功（用户 id=%s，sid=%s）",
                 self.user_id_header, self.sid)

    def ensure_token(self):
        """拿一个可用的 access_token：refresh 优先，失败才重新登录。

        这样机器人长期只占 1 条活跃会话，而不是每次 token 过期都 +1 条
        30 天会话把自己撞进 AUTH_SESSION_LIMIT。
        """
        if self.token:
            return
        if self.refresh():
            return
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
            LOG.info("access_token 可能过期，refresh 或重新登录后重试一次")
            self.token = None
            self.ensure_token()
            code, data = self._req("POST", "/api/user/", body)
        return code, data

    def _logout(self):
        """用当前 access_token 登出，把这条 user_sessions 记录撤销掉。

        失败无所谓 —— 那说明这条会话本来就是过期的，占用照旧，只能靠 DB 清理。
        """
        if not self.token:
            return
        code, data = self._req("POST", "/api/user/auth/logout", None)
        LOG.info("登出旧会话 → HTTP %s", code)
        self.token = None
        self.user_id_header = str(self.user_id or "")
        self.sid = None
        self.jar.clear()
        self._save_session()

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
***REMOVED*** QQ 通知（走 Hermes 的 hermes send 直发通道）
***REMOVED*** --------------------------------------------------------------------------
class QQNotify:
    """调 `hermes send -t qqbot`，由 Hermes 复用 gateway 已配置的平台凭据投递，
    不经过 agent、不需要 LLM。

    曾经写成往 ~/.hermes/cron/jobs.json 里追加 `deliver=qqbot` 的一次性任务，
    靠 gateway 的 ticker 消费。实测那条路**从未生效**：Hermes 不认 "manual"
    这个调度类型，规范化后 schedule 变成 {}（无 kind / run_at），ticker 无从
    触发，executions.db 里也查不到任何执行记录；同时 no_agent=True 不带
    script 本身就是非法组合（cron/jobs.py:1788 NO_AGENT_WITHOUT_SCRIPT_ERROR），
    jobs.json 里因此攒下了 12 条永不执行的僵尸任务。
    `hermes send` 是官方为此提供的脚本管道通道，直接投递，无中间状态。"""

    def __init__(self, hermes_bin, admin_user, openid):
        self.hermes = hermes_bin
        self.admin_user = admin_user
        self.openid = openid
        try:
            self.uid = os.stat("/home/%s" % admin_user).st_uid
        except OSError:
            self.uid = 1000

    def send(self, text):
        try:
            return self._deliver(text)
        except Exception as e:
            LOG.error("QQ 通知失败：%s", e)
            return False

    def _deliver(self, text):
        import subprocess
        import tempfile
        env = dict(os.environ)
        env["HOME"] = "/home/%s" % self.admin_user
        env["XDG_RUNTIME_DIR"] = "/run/user/%d" % self.uid
        ***REMOVED*** 消息体走临时文件而不是 argv：issue 标题可能很长，argv 有长度上限；
        ***REMOVED*** 写文件也让 systemd 日志里的换行显示得清楚些。
        fd, path = tempfile.mkstemp(prefix="nebula-qq-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            ***REMOVED*** 消息里含邮箱和初始密码，权限按属主收紧；但必须归到 Hermes 的
            ***REMOVED*** 属主名下，否则 sudo -u 切过去之后读不到
            ***REMOVED*** （Errno 13 Permission denied）。
            os.chown(path, self.uid, self.uid)
            os.chmod(path, 0o600)
            ***REMOVED*** -t qqbot 走 home channel；openid 只用于日志核对，不参与路由。
            proc = subprocess.run(
                ["/usr/bin/sudo", "-n", "-u", self.admin_user,
                 self.hermes, "send", "-t", "qqbot", "-f", path],
                capture_output=True, text=True, timeout=60, env=env)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if proc.returncode != 0:
            LOG.error("hermes send 退出码 %d：%s",
                      proc.returncode, (proc.stderr or "").strip()[:300])
            return False
        ***REMOVED*** hermes send 会回报实际投递目标，把它记下来：出问题时这是唯一能证明
        ***REMOVED*** 消息到底发到哪里的凭据。
        result = (proc.stdout or "").strip().replace("\n", " ")[:200]
        LOG.info("已投递 QQ 通知（%d 字）→ %s", len(text), result)
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

    def send(self, to_addr, subject, body, html_body=None):
        """new-api 的 common/email.go:44-71 对 465 端口走隐式 TLS（tls.Dial），
        这里保持一致。

        html_body 非空时用 multipart/alternative 同时挂纯文本与 HTML 两份，
        收件端按能力挑：QQ 邮箱、Outlook、Gmail 都认，但各家默认样式差异
        很大，所以 HTML 必须全部 inline 写死，不依赖 <style> 块（QQ 邮箱
        会把它整个丢掉）。"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("Nebula-Zero", "utf-8")), self.sender))
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

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
***REMOVED*** 用户名只允许字母数字下划线连字符（new-api 的 Username 不接受别的）
SAFE_RE = re.compile(r"[A-Za-z0-9_-]+")


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
***REMOVED*** 用户名策略
***REMOVED*** --------------------------------------------------------------------------
***REMOVED*** new-api 的 Username 上限是 20 个字符（model/user.go:81 的 validate:"max=20"），
***REMOVED*** 而 "@example.edu" 光后缀就占 12 个 —— 完整邮箱当用户名时，只有本地部分
***REMOVED*** 不超过 8 个字符的地址才能通过校验，Qufu师大的ID邮箱（26 字符）会在
***REMOVED*** 审核环节静默失败。所以用户名统一取邮箱 @ 前面的部分，也就是ID或姓名拼音。
***REMOVED***
***REMOVED*** 补写 users.email 之后邮箱本身也能登录：model/user.go:1068 查的是
***REMOVED*** "username = ? OR email = ?"，两个凭据都能用，邮件里会一并说明。
USERNAME_MAX = 20


def username_from_email(email, exists):
    """按批准策略推导登录用户名。

    exists(candidate) -> bool：候选名是否已被占用（含软删账号，因为
    uni_users_username 是全表唯一索引，软删不释放名字）。

    返回 (username, randomized) —— randomized 为 True 表示因为名字被占
    或者本地部分超长而走了随机后缀，邮件里要额外说明。
    """
    local, _, _dom = email.partition("@")

    def usable(name):
        return 1 <= len(name) <= USERNAME_MAX and bool(SAFE_RE.fullmatch(name))

    ***REMOVED*** 首选：本地部分原样。ID天然唯一，绝大多数人都能一次过。
    if not exists(local) and usable(local):
        return local, False

    reason = "已被其他账号占用" if exists(local) else "长度超过 %d 字符" % USERNAME_MAX

    ***REMOVED*** 降级：截前 6 个安全字符 + 随机后缀，总长 <= 11，撞名就重试。
    prefix = "".join(c for c in local[:6] if SAFE_RE.match(c))[:6]
    if prefix:
        for _ in range(5):
            cand = "%s-%s" % (prefix, "".join(
                secrets.choice(string.ascii_lowercase + string.digits)
                for _ in range(4)))
            if not exists(cand):
                return cand, True
    ***REMOVED*** 前缀都造不出来（全是符号/中文）→ 只剩纯随机名。
    for _ in range(5):
        cand = "nz" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits)
            for _ in range(6))
        if not exists(cand):
            return cand, True
    return None, False


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


QQ_CMD_RE = re.compile(r"^\s****REMOVED***?\s*(\d{1,5})\s*[:：\s,，、]?\s*(.+?)\s*$")


def parse_qq_command(text):
    """把管理员在 QQ 里发的一句话解析成 (issue_number, verdict)。

    认得的写法（都要求消息里有单号，避免误触发）：
        ***REMOVED***9 approve   /  ***REMOVED***9 批准   /  9 通过   /  ***REMOVED***9: reject   /  ***REMOVED***9 拒绝
    后半段允许带客套话，例如「***REMOVED***9 approve 但是我觉得还是 reject」——按 decide()
    的规则 reject 优先，不会因为句首的 approve 就误批。
    返回 None 表示这不是一条指令（聊天、表情、别的闲聊都走这里）。
    """
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    m = QQ_CMD_RE.match(t)
    if not m:
        return None
    verdict = decide(m.group(2))
    return (int(m.group(1)), verdict) if verdict else None


class QQInbox:
    """读 QQ 入站消息，作为审核指令的第二个入口。

    管理员在手机上直接发「***REMOVED***9 approve」就能批准，不必去 GitHub 网页点评论，
    而且 **Hermes 自己不会有任何回复** —— 这是 m07470 的明确要求。

    两种数据后端，优先队列文件：

    1. **队列文件**（首选，装了 hermes-plugin/nebula-qq-sink 时）
       那个插件挂在 Hermes 的 ``pre_gateway_dispatch`` 钩子上，在消息到达
       agent 之前就把「***REMOVED***N approve/reject」拦下来，写进一个 JSONL 队列并让
       gateway 丢弃该消息 —— 所以 Hermes 既不回复也不写 state.db。机器人
       只需跟着读这个文件的尾巴。

    2. **state.db 只读**（没装插件时的兜底）
       gateway 收到 QQ 消息后会把消息写进 sessions/messages 表，机器人以
       只读方式读这张表。缺点是消息会继续走 agent，Hermes 会回一句废话，
       所以只作为兼容保留。

    两种后端都不做 WebSocket、不经过 LLM、不碰 Hermes 的写入逻辑。
    """

    def __init__(self, db_path, admin_openid, session_id, queue_path=None):
        self.db = db_path
        self.admin_openid = (admin_openid or "").upper()
        self.session_id = session_id
        self.queue = queue_path or ""
        self.offset = 0
        self.cursor = None
        self.enabled = False
        self.mode = ""
        if self.queue:
            try:
                ***REMOVED*** 队列文件可能还不存在（插件还没拦到过消息），这不影响启用。
                self.offset = os.path.getsize(self.queue) if os.path.exists(self.queue) else 0
            except Exception as e:
                LOG.warning("队列文件不可读，暂时退回 state.db 模式：%s", e)
                self.queue = ""
        if not self.queue:
            if not os.path.exists(db_path):
                LOG.warning("QQ 指令通道未启用：找不到 %s", db_path)
                return
            try:
                self._open()
            except Exception as e:
                LOG.warning("QQ 指令通道初始化失败（不影响 GitHub 评论审核）：%s", e)
                return
            self.mode = "db"
        else:
            self.mode = "queue"
        self.enabled = True
        LOG.info("QQ 指令通道已启用（%s）",
                 "队列文件 %s" % self.queue if self.mode == "queue"
                 else "state.db 会话 %s" % (self.session_id or "自动定位"))

    def _connect(self):
        import sqlite3
        ***REMOVED*** 只读打开：绝不能因为机器人的 bug 影响运行中的 Hermes。
        uri = "file:%s?mode=ro&immutable=0" % self.db.replace("?", "%3f")
        return sqlite3.connect(uri, uri=True, timeout=5)

    def _open(self):
        con = self._connect()
        try:
            if not self.session_id:
                row = con.execute(
                    "select id from sessions where source='qqbot' and "
                    "(? = '' or user_id = ?) order by rowid desc limit 1",
                    (self.admin_openid, self.admin_openid)).fetchone()
                if not row:
                    raise RuntimeError("state.db 里没有 qqbot 会话")
                self.session_id = row[0]
            ***REMOVED*** 只从"当前已有消息"之后开始读，避开历史闲聊与机器人自己的转发回执。
            row = con.execute(
                "select coalesce(max(id), 0) from messages where session_id=?",
                (self.session_id,)).fetchone()
            self.cursor = row[0] if row else 0
        finally:
            con.close()

    def poll(self):
        """返回 [(message_id, text), ...] —— 上次读取之后新增的 QQ 消息。"""
        if not self.enabled:
            return []
        return self._poll_queue() if self.mode == "queue" else self._poll_db()

    def _poll_queue(self):
        """读插件写下的 JSONL 队列尾巴。

        插件已经替我们做完了解析与鉴权，这里只把 ``text`` 原样交给
        parse_qq_command 再判一次 —— 双保险，避免插件配置漂移时误判。
        读不到就当没有，下一轮再看。
        """
        out = []
        try:
            size = os.path.getsize(self.queue)
        except OSError:
            return []
        if size <= self.offset:
            ***REMOVED*** 文件被截断/替换（不该发生），从新文件头开始，别漏指令。
            if size < self.offset:
                LOG.warning("QQ 指令队列变小（%d < %d），从头重读", size, self.offset)
                self.offset = 0
            else:
                return []
        try:
            with open(self.queue, "r", encoding="utf-8") as fh:
                fh.seek(self.offset)
                blob = fh.read()
        except OSError as e:
            LOG.warning("读取 QQ 指令队列失败：%s", e)
            return []
        ***REMOVED*** 只消费到最后一个换行。插件正在往文件里 append 时，我们完全可能读到
        ***REMOVED*** 半行 JSON；把这一截留在文件里等下一轮，否则既丢指令又刷无意义的
        ***REMOVED*** "不是 JSON" 警告。
        nl = blob.rfind("\n")
        if nl < 0:
            return []
        blob, rest = blob[:nl + 1], blob[nl + 1:]
        self.offset += len(blob.encode("utf-8"))
        if rest.strip():
            LOG.debug("QQ 指令队列尾部有半行未写完，本轮不消费（%d 字节）",
                      len(rest.encode("utf-8")))
        for idx, line in enumerate(blob.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                LOG.warning("QQ 指令队列第 %d 行不是 JSON，已跳过：%r", idx + 1, line[:80])
                continue
            text = rec.get("text") or ""
            if not text:
                continue
            out.append((rec.get("message_id") or rec.get("ts") or self.offset, text))
        return out

    def _poll_db(self):
        out = []
        try:
            con = self._connect()
        except Exception as e:
            LOG.warning("打开 state.db 失败：%s", e)
            return []
        try:
            rows = con.execute(
                "select id, content from messages "
                "where session_id=? and role='user' and id > ? "
                "and coalesce(platform_message_id,'') <> '' "
                "order by id limit 20",
                (self.session_id, self.cursor)).fetchall()
            for mid, content in rows:
                self.cursor = max(self.cursor, mid)
                if content:
                    out.append((mid, content))
        except Exception as e:
            LOG.warning("读取 QQ 消息失败：%s", e)
        finally:
            con.close()
        return out

    def rewind(self, message_id):
        """把游标退回某条消息之前，让它能被重新解析（仅 state.db 模式）。

        只改内存游标，不写库（state.db 是 Hermes 在用的，必须保持只读打开）。
        队列模式是 append-only 的，逻辑上不存在回退，也没有任何调用点。
        """
        if self.mode == "db" and self.enabled and self.cursor and message_id < self.cursor:
            self.cursor = message_id - 1


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


def handle(gh, api, mailer, qq, cfg, issue, pre_existing=False, patcher=None,
           qq_inbox=None):
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
        "两种方式任选其一：",
        "· 在本 QQ 里直接发「***REMOVED***%d approve」或「***REMOVED***%d reject」" % (number, number),
        "· 或者去该 issue 下回复含 approve / reject 的评论。",
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

        ***REMOVED*** ---- QQ 指令：管理员在手机上直接发「***REMOVED***N approve」即可，不必去网页 ----
        ***REMOVED*** 先看 QQ，因为它是管理者最快的入口；单号对不上就退回游标等下一轮，
        ***REMOVED*** 不会干扰下面 GitHub 评论的正常判定。
        verdict = None
        if qq_inbox is not None and qq_inbox.enabled:
            for mid, text in qq_inbox.poll():
                cmd = parse_qq_command(text)
                if cmd is None:
                    continue                      ***REMOVED*** 闲聊，不是指令
                cmd_number, cmd_verdict = cmd
                if cmd_number != number:
                    LOG.info("忽略 QQ 指令 ***REMOVED***%d %s（当前等待的是 ***REMOVED***%d）",
                             cmd_number, cmd_verdict, number)
                    continue
                LOG.info("***REMOVED***%d 收到 QQ 指令 → 判定 %s", number, cmd_verdict)
                verdict = cmd_verdict
                break

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

        if verdict is None:
            try:
                comments = gh.issue_comments(number)
            except Exception as e:
                LOG.warning("拉取 ***REMOVED***%d 评论失败：%s", number, e)
                continue

            for c in comments:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                ***REMOVED*** 机器人自己的通知评论不算决策
                if (c.get("user") or {}).get("login", "").endswith("[bot]"):
                    continue
                verdict = decide(c.get("body"))
                LOG.info("***REMOVED***%d 收到新评论（@%s）→ 判定 %s",
                         number, (c.get("user") or {}).get("login", "?"),
                         verdict or "无关键字")
                if verdict:
                    break

        if not verdict:
            continue

        ***REMOVED*** ---- 决策落地 ----
        if verdict == "reject":
            plain, rich = reject_email_body(cfg, title)
            try:
                mailer.send(email, "【Nebula-Zero】您的开户申请未通过",
                            plain, rich)
                LOG.info("***REMOVED***%d 拒绝邮件已发往 %s", number, email)
                ***REMOVED*** 管理员在 QQ 里下的指令，处置结果必须回到 QQ —— 否则他只能
                ***REMOVED*** 去 GitHub 看 issue 才知道跑没跑完。
                qq.send("【Nebula-Zero】***REMOVED***%d 已拒绝（未创建账号）\n"
                        "申请人邮箱：%s\n"
                        "拒绝通知邮件已发出，issue 已关闭并锁定。"
                        % (number, email))
            except Exception as e:
                LOG.error("***REMOVED***%d 拒绝邮件发送失败：%s", number, e)
                qq.send("【Nebula-Zero】***REMOVED***%d 已拒绝（未创建账号），但拒绝邮件发送失败：%s\n"
                        "申请人邮箱：%s，请手工告知结果。\nissue 已关闭并锁定。"
                        % (number, e, email))
            finalize(gh, number, "**结果：未通过**\n\n已向 %s 发送结果通知邮件。" % email)
            return

        ***REMOVED*** approve —— 先建号，再发信；建号失败绝不能发信，否则用户拿到的是无效账号

        ***REMOVED*** 用户名已经被人占了怎么办？分三种：
        ***REMOVED***   · 账号还活着（deleted_at 为空）→ 不重复建号，提示管理员手工重置密码
        ***REMOVED***     （UpdateUser 需要 X-Security-Proof，机器人拿不到）；
        ***REMOVED***   · 名字被软删账号占着，或本地部分超 20 字符 → 走随机后缀改名；
        ***REMOVED***   · 随机 5 次仍撞名 → 放弃，让管理员手工建号。
        ***REMOVED*** 查找要同时看邮箱和用户名：username_from_email 拿邮箱本地部分去查，
        ***REMOVED*** 重复申请的人用哪个凭据登录都能命中。
        pw = gen_password()
        LOG.info("***REMOVED***%d 批准，建号中：email=%s", number, email)
        exists = lambda cand: api.find_user_by_name(cand) is not None
        username, randomized = username_from_email(email, exists)
        if username is None:
            LOG.error("***REMOVED***%d 无法为 %s 推导可用用户名（随机 5 次均撞名）", number, email)
            qq.send("【Nebula-Zero】***REMOVED***%d 批准但**未创建账号**\n"
                    "申请人邮箱：%s\n"
                    "原因：邮箱本地部分超 20 字符上限，随机用户名又连续撞名，需手工建号。\n"
                    "issue 已关闭并锁定。" % (number, email))
            finalize(gh, number,
                     "**结果：批准，但自动建号失败**\n\n"
                     "邮箱 `%s` 的本地部分超过系统 20 字符上限，"
                     "随机用户名又连续撞名，请管理员手工创建。" % email)
            return

        ***REMOVED*** 邮箱本身已经被一个活跃账号占着 → 不重复建号
        existing = api.find_user_by_name(email)
        if existing is not None and not existing.get("DeletedAt"):
            LOG.info("***REMOVED***%d 该邮箱已有活跃账号（id=%s），不重复建号",
                     number, existing.get("id"))
            qq.send("【Nebula-Zero】***REMOVED***%d 批准但**未创建新账号**\n"
                    "申请人邮箱：%s\n"
                    "该邮箱已有活跃账号（id=%s），本次申请不重复建号。\n"
                    "如需重置密码，请手工在后台操作。\n"
                    "issue 已关闭并锁定。" % (number, email, existing.get("id")))
            finalize(gh, number,
                     "**结果：该邮箱已开户**\n\n"
                     "`%s` 已存在有效账号，本次申请未重复创建。\n\n"
                     "如需重置密码，请联系管理员。" % email)
            return

        if randomized:
            LOG.warning("***REMOVED***%d 用户名改为随机：%s（邮箱 %s）", number, username, email)
            ***REMOVED*** 改名只是提示，真正的处置回执在后面统一发。
            qq.send("【Nebula-Zero】***REMOVED***%d 提示：登录用户名改为 %s\n"
                    "（邮箱 %s 的本地部分过长，或该名字已被注销账号占用）。绑定邮箱不变。"
                    % (number, username, email))

        code, data = api.create_user(
            username=username, password=pw, email=email,
            quota=int(cfg["quota"]), role=int(cfg["role"]))

        ***REMOVED*** 建号失败就到此为止 —— 绝不能继续发信，否则用户拿到的是无效账号
        if code not in (200, 201) or (isinstance(data, dict) and
                                      data.get("success") is False):
            LOG.error("***REMOVED***%d 建号失败 HTTP %s: %s", number, code, data)
            qq.send("【Nebula-Zero】***REMOVED***%d 批准但**未创建账号**\n"
                    "申请人邮箱：%s\n"
                    "原因：new-api 建号接口返回 HTTP %s `%s`，请人工处理。\n"
                    "（未发开户邮件 —— 避免用户拿到无效账号。）\n"
                    "issue 已关闭并锁定。" % (number, email, code, data))
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
                    ***REMOVED*** 补写失败不影响开户（账号已建、邮件照发），这里只提醒管理员
                    ***REMOVED*** 邮箱列空着会导致日后给他发系统通知邮件投不出去。
                    qq.send("【Nebula-Zero】***REMOVED***%d 小提醒：users.email 补写失败（%s）\n"
                            "账号 %s 已正常创建并开户，只是邮箱列没落上，"
                            "日后系统通知邮件可能投不出去。不影响本次流程。"
                            % (number, detail, username))
            except Exception:
                LOG.exception("***REMOVED***%d 邮箱补写异常（已忽略，不影响开户）", number)

        try:
            plain, rich = approve_email_body(cfg, username, pw, email, randomized)
            mailer.send(email, "【Nebula-Zero】您的账号已开通", plain, rich)
            LOG.info("***REMOVED***%d 开户邮件已发往 %s", number, email)
            note = ("**结果：已通过**\n\n"
                    "已创建账号 `%s` 并向 %s 发送开户邮件（含初始密码）。"
                    % (username, email))
            ***REMOVED*** 账号已建且邮件已发 —— 这是最该回报的一条，密码同时发到 QQ 兜底，
            ***REMOVED*** 免得邮件进了垃圾箱就彻底找不回来了。
            qq.send("【Nebula-Zero】***REMOVED***%d 已通过，账号创建成功\n"
                    "申请人邮箱：%s\n"
                    "登录用户名：%s\n"
                    "初始密码：%s\n"
                    "开户邮件已发出，issue 已关闭并锁定。"
                    "\n提醒本人首次登录后立刻改密码。"
                    % (number, email, username, pw))
        except Exception as e:
            LOG.error("***REMOVED***%d 开户邮件发送失败：%s", number, e)
            ***REMOVED*** 账号已建但邮件没发出去——必须告诉管理员密码，否则用户永远拿不到
            qq.send("【Nebula-Zero】***REMOVED***%d 账号**已创建**但开户邮件发送失败：%s\n"
                    "申请人邮箱：%s\n"
                    "登录用户名：%s\n"
                    "初始密码：%s\n"
                    "请手工把凭据发给申请人。issue 已关闭并锁定。"
                    % (number, e, email, username, pw))
            note = ("**结果：已通过，但邮件发送失败**\n\n"
                    "账号已创建（用户名 `%s`），但通知邮件未能送达。\n\n"
                    "管理员已收到含初始密码的告警，请手工转发凭据。"
                    % username)

        finalize(gh, number, note)
        return


def approve_email_body(cfg, username, password, email, randomized=False):
    site = cfg["site_url"]
    note = ""
    if randomized:
        note = ("<table width='100%' cellpadding='0' cellspacing='0' "
                "style='background:***REMOVED***fff7ed;border-left:4px solid ***REMOVED***f59e0b'>"
                "<tr><td style='padding:12px 14px;font-size:13px;line-height:1.8;"
                "color:***REMOVED***7c2d12'>你的邮箱 @ 前面的部分超过 {mx} 个字符，"
                "或者这个名字已经被别的账号占用，所以登录用户名是 "
                "<b style='font-family:Consolas,Menlo,monospace'>{un}</b>，"
                "绑定邮箱仍然是 "
                "<b style='font-family:Consolas,Menlo,monospace'>{em}</b>。"
                "用它登录也可以。</td></tr></table>"
                ).format(mx=USERNAME_MAX, un=_esc(username), em=_esc(email))

    plain = (
        "%s 同学你好：\n\n"
        "你的 Nebula-Zero 账号已开通。\n\n"
        "登录地址 : %s\n"
        "用户名   : %s\n"
        "初始密码 : %s\n\n"
        "使用须知：\n"
        "  1. 首次登录后请立即在「个人设置」里修改密码。\n"
        "  2. 所有模型当前均为 0 元按量计费，你的账号额度不会因调用而扣减。\n"
        "  3. 创建自己的 API Key 后即可调用，接口地址见下方三种协议。\n"
        "  4. 上游为公开免费服务，偶发限流或临时不可用属正常现象，可稍后重试。\n\n"
        "如有任何问题，直接在本仓库开 issue 询问即可。\n\n"
        "—— Nebula-Zero 自动开户系统\n"
        % (username, site, username, password)
    )

    def kv(label, value, mono=True):
        fam = "font-family:Consolas,Menlo,monospace;font-size:15px;" if mono else ""
        return ("<tr><td style='padding:9px 0;color:***REMOVED***64748b;font-size:13px;"
                "width:88px;vertical-align:top'>{lb}</td>"
                "<td style='padding:9px 0;color:***REMOVED***0f172a;{fam}"
                "word-break:break-all'>{val}</td></tr>").format(
                    lb=label, fam=fam, val=value)

    html = _MAIL_SHELL.format(
        title="您的账号已开通",
        greeting="<p style='margin:0 0 16px 0;font-size:15px;line-height:1.8;"
                 "color:***REMOVED***334155'>%s 同学你好，</p>"
                 "<p style='margin:0 0 20px 0;font-size:15px;line-height:1.8;"
                 "color:***REMOVED***334155'>你的 <b>Nebula-Zero</b> 账号已经开通了，"
                 "下面是登录凭据和接入方式。</p>" % _esc(username),
        creds=("<table width='100%' cellpadding='0' cellspacing='0' "
              "style='background:***REMOVED***f8fafc;border:1px solid ***REMOVED***e2e8f0;"
              "border-radius:8px'>{a}{b}{c}</table>").format(
                  a=kv("登录地址", _esc(site)),
                  b=kv("登录用户名", _esc(username)),
                  c=kv("初始密码", _esc(password))),
        note=note,
        warn="<table width='100%' cellpadding='0' cellspacing='0' "
             "style='background:***REMOVED***fef2f2;border-left:4px solid ***REMOVED***ef4444'>"
             "<tr><td style='padding:12px 14px;font-size:13px;line-height:1.7;"
             "color:***REMOVED***7f1d1d'><b>请注意</b>：首次登录后立刻在「个人设置」里"
             "改掉这个初始密码。</td></tr></table>",
        usage=_protocol_block(site) +
              "<div style='font-size:13px;line-height:1.9;color:***REMOVED***334155;"
              "margin-top:18px'>"
              "<b>怎么用</b><br>"
              "<b>1. 建 Key</b>：登录后在「API 密钥」里新建一个 Key。<br>"
              "<b>2. 不扣额度</b>：全部模型当前 0 元按量计费，"
              "调用不扣你的额度。<br>"
              "<b>3. 上游偶发不可用</b>：上游是公开免费服务，"
              "429 限流或 502 属正常现象，稍后重试即可。<br>"
              "<b>4. 提问</b>：直接在本仓库开一个 issue 就行。</div>",
    )
    return plain, html


def reject_email_body(cfg, title):
    plain = (
        "你好：\n\n"
        "很抱歉，你在 Nebula-Zero 提交的开户申请（%s）本次未能通过。\n\n"
        "常见原因：\n"
        "  · 申请邮箱不属于本站允许的注册域名\n"
        "  · 申请信息填写不完整\n"
        "  · 申请提交频率过于频繁\n\n"
        "如果你认为这是误判，欢迎在原 issue 下补充说明，或直接重新提交一份申请。\n\n"
        "—— Nebula-Zero 自动开户系统\n" % title
    )
    html = _MAIL_SHELL.format(
        title="您的申请未通过",
        greeting="<p style='margin:0 0 16px 0;font-size:15px;line-height:1.8;"
                 "color:***REMOVED***334155'>你好，</p>"
                 "<p style='margin:0 0 20px 0;font-size:15px;line-height:1.8;"
                 "color:***REMOVED***334155'>很抱歉，你在 Nebula-Zero 提交的申请"
                 "（%s）本次未能通过。</p>" % _esc(title),
        creds="<table width='100%' cellpadding='0' cellspacing='0' "
              "style='background:***REMOVED***fff7ed;border-left:4px solid ***REMOVED***f59e0b'>"
              "<tr><td style='padding:12px 14px;font-size:13px;line-height:1.9;"
              "color:***REMOVED***7c2d12'>常见原因：<br>"
              "· 申请邮箱不属于本站允许的注册域名（仅限 example.edu）<br>"
              "· 申请信息填写不完整<br>"
              "· 申请提交频率过于频繁</td></tr></table>",
        note="",
        warn="",
        usage="<p style='margin:0;font-size:13px;line-height:1.8;"
              "color:***REMOVED***334155'>如果你认为这是误判，欢迎在原 issue 下"
              "补充说明，或直接重新提交一份申请。</p>",
    )
    return plain, html


def _protocol_block(site):
    """三种协议的接入示例。Gemini 原生协议上游不支持，单独说明，
    免得有人照着别处的教程填 /v1beta 结果拿到 404。"""
    base = _esc(site)
    rows = [
        ("OpenAI 兼容（推荐）", "绝大多数客户端填这个",
         'curl %s/v1/chat/completions \\\n'
         '  -H "Authorization: Bearer 你的Key" \\\n'
         '  -H "Content-Type: application/json" \\\n'
         '  -d \'{"model":"big-pickle","messages":'
         '[{"role":"user","content":"你好"}]}\'' % base),
        ("Anthropic 兼容", "Claude 官方 SDK 可直接使用",
         'curl %s/v1/messages \\\n'
         '  -H "x-api-key: 你的Key" \\\n'
         '  -H "anthropic-version: 2023-06-01" \\\n'
         '  -H "Content-Type: application/json" \\\n'
         '  -d \'{"model":"big-pickle","max_tokens":1024,'
         '"messages":[{"role":"user","content":"你好"}]}\'' % base),
        ("Responses 协议", "OpenAI 新版 Responses 接口",
         'curl %s/v1/responses \\\n'
         '  -H "Authorization: Bearer 你的Key" \\\n'
         '  -H "Content-Type: application/json" \\\n'
         '  -d \'{"model":"big-pickle","input":"你好"}\'' % base),
    ]
    out = []
    for name, desc, code in rows:
        out.append(
            "<tr><td style='padding:14px 0 0 0'>"
            "<div style='font-size:14px;color:***REMOVED***0f172a'><b>{nm}</b>"
            "<span style='color:***REMOVED***94a3b8;font-size:12px'> · {ds}</span></div>"
            "<pre style='margin:8px 0 0 0;padding:12px;background:***REMOVED***0f172a;"
            "color:***REMOVED***e2e8f0;border-radius:6px;font-size:12px;line-height:1.6;"
            "overflow-x:auto;word-break:break-all'>{cd}</pre></td></tr>"
            .format(nm=name, ds=desc, cd=_esc(code)))
    out.append(
        "<tr><td style='padding:14px 0 0 0;font-size:12px;color:***REMOVED***64748b'>"
        "Gemini 原生协议（/v1beta/…:generateContent）上游暂不支持，"
        "需要该格式请走 OpenAI 兼容接口。</td></tr>")
    return ("<table width='100%' cellpadding='0' cellspacing='0'>"
            + "".join(out) + "</table>")


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


***REMOVED*** 邮件外壳：全部 inline 样式。QQ 邮箱会丢弃 <style> 块和部分 CSS，
***REMOVED*** 所以配色、间距、内边距都写在内联属性上，靠 table 布局兜住各家客户端。
***REMOVED***
***REMOVED*** 用 str.format 的具名占位符而不是 %-格式化：这份模板里全是 CSS 的
***REMOVED*** 百分号（width:100%），走 %-格式化每个都得写成 %%，漏一个就是
***REMOVED*** TypeError 或整段样式错位 —— 调试时已经在这上面翻车好几次。
***REMOVED*** 具名占位符对 % 完全免疫，少传一个会直接 KeyError，而不是静默错位。
_MAIL_SHELL = (
    "<!DOCTYPE html><html><body style='margin:0;padding:0;"
    "background:***REMOVED***f1f5f9'>"
    "<table width='100%' cellpadding='0' cellspacing='0' "
    "style='background:***REMOVED***f1f5f9;padding:28px 12px'>"
    "<tr><td align='center'>"
    "<table width=\"600\" cellpadding=\"0\" cellspacing=\"0\" "
    "style=\"width:600px;max-width:100%;background:***REMOVED***ffffff;"
    "border-radius:12px;overflow:hidden;font-family:'Segoe UI',"
    "'PingFang SC','Microsoft YaHei',Arial,sans-serif\">"

    "<tr><td style='background:***REMOVED***4f46e5;padding:26px 24px'>"
    "<div style='color:***REMOVED***ffffff;font-size:19px;font-weight:600'>Nebula-Zero</div>"
    "<div style='color:***REMOVED***c7d2fe;font-size:13px;margin-top:5px'>{title}</div>"
    "</td></tr>"

    "<tr><td style='padding:24px 24px 4px 24px'>{greeting}</td></tr>"
    "<tr><td style='padding:12px 24px 0 24px'>{creds}</td></tr>"
    "<tr><td style='padding:0 24px'>{note}</td></tr>"
    "<tr><td style='padding:18px 24px 0 24px'>{warn}</td></tr>"
    "<tr><td style='padding:22px 24px 22px 24px'>{usage}</td></tr>"

    "<tr><td style='padding:26px 24px 22px 24px'>"
    "<div style='border-top:1px solid ***REMOVED***e2e8f0;padding-top:16px;"
    "font-size:12px;color:***REMOVED***94a3b8;line-height:1.8'>"
    "本邮件由 Nebula-Zero 自动开户系统发出，请勿直接回复。<br>"
    "本站只接受the organizationregistered邮箱（@example.edu）申请，"
    "没有registered邮箱请先看 "
    "<a href='https://net.example.edu' style='color:***REMOVED***4f46e5'>"
    "the IT service portal的申请指引</a>。"
    "</div></td></tr>"

    "</table></td></tr></table></body></html>"
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
                 conf["NEWAPI_ADMIN_PASS"],
                 session_file=os.path.join(os.path.dirname(os.path.abspath(args.config)),
                                           ".bot_session.json"))
    mailer = Mailer(conf["SMTP_HOST"], conf["SMTP_PORT"], conf["SMTP_ACCOUNT"],
                    conf["SMTP_TOKEN"], conf.get("SMTP_FROM") or conf["SMTP_ACCOUNT"],
                    use_ssl=conf.get("SMTP_SSL", "true").lower() == "true")
    qq = QQNotify(conf.get("HERMES_BIN",
                          "/home/admin/.hermes/hermes-agent/.hermes/bin/hermes"),
                  conf.get("HERMES_ADMIN_USER", conf["NEWAPI_ADMIN_USER"]),
                  conf.get("QQ_HOME_CHANNEL", ""))

    ***REMOVED*** QQ 指令通道：让管理员在手机上发「***REMOVED***N approve」就能批准，不必去 GitHub 网页。
    ***REMOVED*** QQ 指令通道：优先读 hermes-plugin/nebula-qq-sink 写的队列文件（Hermes 保持
    ***REMOVED*** 沉默）；没配队列时退回只读 Hermes 的 state.db。
    qq_inbox = None
    if conf.get("QQ_COMMAND", "true").lower() == "true":
        admin_user = conf.get("HERMES_ADMIN_USER", conf["NEWAPI_ADMIN_USER"])
        hermes_home = conf.get("HERMES_HOME", "/home/%s/.hermes" % admin_user)
        queue_path = conf.get("QQ_COMMAND_QUEUE", "")
        if queue_path:
            parent = os.path.dirname(queue_path)
            if parent and not os.access(parent, os.W_OK):
                LOG.warning("QQ 指令队列目录 %s 对本进程不可写；机器人只读队列不受影响，"
                            "但如果 gateway 也不是该目录的属主，QQ 审批会静默失效", parent)
            if not os.path.exists(queue_path):
                LOG.info("QQ 指令队列 %s 还不存在（插件尚未拦到过消息），按队列模式等待", queue_path)
        qq_inbox = QQInbox(
            conf.get("HERMES_STATE_DB", os.path.join(hermes_home, "state.db")),
            conf.get("QQ_HOME_CHANNEL", ""),
            conf.get("QQ_SESSION_ID", ""),
            queue_path=queue_path)
    else:
        LOG.info("QQ 指令通道：已通过 QQ_COMMAND=false 关闭")

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
    ***REMOVED*** 崩溃的 issue 不进 seen，但要退避重试 —— 否则一次网络抖动就永久漏掉一个申请。
    ***REMOVED*** 10 分钟退避是为了别在每轮 30s 里把 QQ 通知刷爆。
    retry_after = {}
    RETRY_SECONDS = 600
    while not stopping["v"]:
        try:
            for issue in gh.issues("open", per_page=100):
                n = issue["number"]
                if n in seen:
                    continue
                if retry_after.get(n, 0) > time.time():
                    continue
                body = issue.get("body") or ""
                if TEMPLATE_MARK not in body:
                    seen.add(n)          ***REMOVED*** 不是申请，不归我们管
                    continue
                try:
                    handle(gh, api, mailer, qq, handle_conf, issue,
                           pre_existing=first, patcher=patcher,
                           qq_inbox=qq_inbox)
                except Exception:
                    LOG.exception("处理 ***REMOVED***%d 时异常，%.0f 分钟后重试",
                                  n, RETRY_SECONDS / 60.0)
                    retry_after[n] = time.time() + RETRY_SECONDS
                    persist()
                    continue
                retry_after.pop(n, None)
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