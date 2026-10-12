# Nebula Zero

> 零计费的免费模型聚合网关 —— 自助开通 + 人工审核的账号发放流水线

把上游的免费大模型能力聚合成统一网关，再配一套「提 issue 申请 → 管理员审批 → 系统自动建号发信 → 关闭 issue」的自动化流水线。

---

## 这是什么

| 组件 | 说明 |
|---|---|
| **services/new-api** | 网关主体。基于 [New API](https://github.com/QuantumNous/new-api)（AGPL-3.0），提供 OpenAI / Anthropic 双协议兼容接口、多渠道负载、自动重试与熔断、令牌与用户管理 |
| **services/opencode2api** | 上游适配器。把 OpenCode 生态的免费模型（big-pickle、space-bunny、nemotron、longcat、mimo、step-5、muse 等）转成标准 API。基于 [jasonxu114514/opencode2api](https://github.com/jasonxu114514/opencode2api)（GPL-3.0） |
| **.github/ISSUE_TEMPLATE** | 账号申请表单（用了模板会自动打上「申请」标签） |
| **deploy/** | Docker Compose 编排、Caddy HTTPS 配置与环境变量模板 |
| **nebula-review-bot.py** | 审核机器人：监听 issue → 通知管理员 → 关键字判定 → 自动建号 → 发信 → 关闭 issue |
| **hermes-plugin/** | Hermes 插件，让管理员能在聊天软件里直接发 `#123 approve`，且机器人不插话 |

## 核心特性

- **零计费**：全部模型 `ModelRatio=0`，前端 Input/Output 两项均显示 0 元，用户余额不扣减
- **HTTPS 开箱即用**：Caddy 自动申请并续期证书，没自有域名也能上（用 `<IP>.sslip.io` 这类把 IP 反解成域名的公共 DNS，Let's Encrypt 不签发裸 IP 证书）
- **多实例冗余**：上游适配器 4 实例轮询，实测 p50 1.8s；单实例会频繁 502/429，不可压容
- **协议透明**：OpenAI Chat / OpenAI Responses / Anthropic Messages 三个端点实测可用
- **自助开通**：issue 申请 → 管理员审批 → 账号密码直达邮箱，全程无需人工建号

## 快速开始

```bash
git clone https://github.com/Swcmb/Nebula-Zero.git
cd Nebula-Zero
cp deploy/.env.example deploy/.env      # 填入自己的密钥
docker compose -f deploy/docker-compose.yml up -d
```

反向代理与 HTTPS 见 [`deploy/Caddyfile`](deploy/Caddyfile)（文件头注释里写了要点）。

## 账号申请流程

1. 打开本仓库的 **New** → **账号申请**
2. 填写身份信息、**你在本站的注册邮箱**、以及接收通知用的 QQ 号
3. 提交后自动打上「申请」标签，管理员收到通知
4. 管理员在 issue 下回复 `approve` / `reject`，**或在聊天软件里发 `#123 approve`**（两种任选其一）
5. 通过后账号信息自动发到你的注册邮箱，issue 随即关闭并锁定

> 审核通道刻意不绑定任何特定人群 —— 收到申请的人自己判断是否放行。

## 审核机器人

```bash
cp nebula-review-bot.env /root/.nebula-zero/bot.env   # 填凭据
cp nebula-review-bot.py   /root/.nebula-zero/bot.py
cp nebula-review-bot.service /etc/systemd/system/
systemctl enable --now nebula-review-bot
```

配置项要点：

| 键 | 作用 |
|---|---|
| `GITHUB_TOKEN` | fine-grained PAT，**只授权本仓库**，需要 Issues: Read and write |
| `SITE_URL` | 网关地址，用于邮件里的 curl 示例 |
| `REGISTRATION_DOMAIN` | 放行的注册邮箱域名（**按你的实际情况配置，仓库里不带任何默认值**） |
| `QQ_GROUP_URL` | 写进邮件的交流群链接；留空则邮件里那段会自动省略 |
| `BACKFILL_EMAIL` | 用 SQL 补写 `users.email` —— new-api 的 admin API 建号时根本不接受 email 字段 |

`services/new-api/controller/user.go` 的 `CreateUser` 只把 `Username/Password/DisplayName/Role` 写进 `cleanUser`，`email` 连结构体都进不去；`EditWithTx` 的 updates map 里也只有 `username/display_name/group/remark`。所以「用管理员 API 建号再补邮箱」这条路走不通，机器人是直接走 `docker exec psql` 补写的（SQL 用 stdin + psql 变量绑定，不做字符串拼接）。

## 目录结构

```
Nebula-Zero/
├── services/
│   ├── new-api/              # 网关主体（AGPL-3.0）
│   └── opencode2api/         # 上游适配器（GPL-3.0）
├── deploy/
│   ├── docker-compose.yml    # 生产编排
│   ├── Caddyfile             # HTTPS 入口
│   └── .env.example          # 环境变量模板
├── .github/ISSUE_TEMPLATE/
│   ├── account-application.yml
│   └── config.yml
├── hermes-plugin/            # QQ/聊天软件审批指令拦截插件
├── nebula-review-bot.py      # 审核机器人
├── nebula-review-bot.env     # 机器人配置模板
├── nebula-review-bot.service # systemd unit
└── setup-nebula-zero.sh      # 一键装配脚本
```

## 已知的上游限制

| 现象 | 原因 |
|---|---|
| Gemini 端点 404 | 上游适配器未实现 Gemini 协议（`v1beta/generateContent`），非本项目缺陷 |
| 部分模型偶发 429/502 | 上游免费代理自身限流或宕机，与本网关无关；已用 4 实例轮询缓解 |
| 邮件偶发超时 | 发信通道为 QQ 邮箱免费额度，对企业域投递有节流 |

## 安全须知

- 拉取 `services/new-api/` 会触发 **GitHub secret scanning 误报**：上游源码 `relaykit/relayconvert/kitutil/mask.go` 的注释里有个 Google API Key 格式的示例串，不是真实凭据。本项目自己的部署配置里没有任何云厂商密钥。
- 审核机器人持有的 PAT 权限很大（能建号、能发信）。用 fine-grained token 限定到单个仓库，机器人跑在 `ProtectSystem=strict` + `NoNewPrivileges` 的 systemd 沙箱里。
- 机器人删评论、关单、锁单的顺序是固定的：加说明评论 → 删评论 → 关闭 → 锁定。这样出问题时 GitHub 上还留得下一条审计痕迹。

## 许可证

本仓库聚合两个上游项目，各自保留原始许可证：

- `services/new-api/` — **AGPL-3.0**（© New API contributors）
- `services/opencode2api/` — **GPL-3.0**（© jasonxu114514）

聚合与部署脚本部分以 AGPL-3.0 提供。

## 致谢

- [New API](https://github.com/QuantumNous/new-api) — 网关主体
- [opencode2api](https://github.com/jasonxu114514/opencode2api) — 上游适配器
- [Hermes](https://github.com/) — 聊天软件通知通道