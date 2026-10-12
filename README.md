***REMOVED*** Nebula Zero

> 零计费的免费模型聚合网关 —— the organizationmember自助开通平台

一个把上游免费大模型能力聚合成统一网关的开源项目。member好友提 issue 申请，管理员在 QQ 上回复关键字即可批准，系统自动建号并发信。

---

***REMOVED******REMOVED*** 这是什么

| 组件 | 说明 |
|---|---|
| **services/new-api** | 网关主体。基于 [New API](https://github.com/QuantumNous/new-api)（AGPL-3.0），提供 OpenAI / Anthropic 双协议兼容接口、多渠道负载、自动重试与熔断、令牌与用户管理 |
| **services/opencode2api** | 上游适配器。把 OpenCode 生态的免费模型（big-pickle、space-bunny、nemotron、longcat、mimo、step-5、muse 等）转成标准 API。基于 [jasonxu114514/opencode2api](https://github.com/jasonxu114514/opencode2api)（GPL-3.0） |
| **.github/ISSUE_TEMPLATE** | member账号申请表单 |
| **deploy/** | Docker Compose 编排与环境变量模板 |
| **nebula-review-bot.py** | 审核机器人：监听 issue → QQ 通知 → 关键字判定 → 自动建号 → 发信 → 关闭 issue |

***REMOVED******REMOVED*** 核心特性

- **零计费**：全部模型 `ModelRatio=0`，前端 Input/Output 两项均显示 0 元，用户余额不扣减
- **多实例冗余**：上游适配器 4 实例轮询，实测 p50 1.8s；单实例会频繁 502/429，不可压容
- **协议透明**：OpenAI Chat / OpenAI Responses / Anthropic Messages 三个端点实测可用
- **自助开通**：issue 申请 → QQ 审批 → 账号密码直达邮箱，全程无需人工建号

***REMOVED******REMOVED*** 快速开始

```bash
git clone https://github.com/Swcmb/Nebula-Zero.git
cd Nebula-Zero
cp deploy/.env.example deploy/.env      ***REMOVED*** 填入自己的密钥
docker compose -f deploy/docker-compose.yml up -d
```

详细部署见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

***REMOVED******REMOVED*** member申请账号

1. 打开本仓库的 **New** → **账号申请**
2. 填写姓名、registered邮箱（`@example.edu`）、QQ 号
3. 提交后管理员会收到 QQ 通知
4. 管理员在 issue 下回复 `approve` 或 `reject`
5. 通过后账号信息自动发到你的registered邮箱，issue 随即关闭

> 需要先用registered邮箱在本部署的 New API 注册基础账号，再提交本申请。

***REMOVED******REMOVED*** 目录结构

```
Nebula-Zero/
├── services/
│   ├── new-api/              ***REMOVED*** 网关主体（AGPL-3.0）
│   └── opencode2api/         ***REMOVED*** 上游适配器（GPL-3.0）
├── deploy/
│   ├── docker-compose.yml    ***REMOVED*** 生产编排
│   └── .env.example          ***REMOVED*** 环境变量模板
├── .github/ISSUE_TEMPLATE/
│   ├── account-application.yml
│   └── config.yml
├── nebula-review-bot.py      ***REMOVED*** 审核机器人
├── nebula-review-bot.env     ***REMOVED*** 机器人配置模板
├── nebula-review-bot.service ***REMOVED*** systemd unit
└── setup-nebula-zero.sh      ***REMOVED*** 一键装配脚本
```

***REMOVED******REMOVED*** 已知的上游限制

| 现象 | 原因 |
|---|---|
| Gemini 端点 404 | 上游适配器未实现 Gemini 协议（`v1beta/generateContent`），非本项目缺陷 |
| 部分模型偶发 429/502 | 上游免费代理自身限流或宕机，与本网关无关；已用 4 实例轮询缓解 |
| 邮件偶发超时 | 发信通道为 QQ 邮箱免费额度，对企业域投递有节流 |

***REMOVED******REMOVED*** 许可证

本仓库聚合两个上游项目，各自保留原始许可证：

- `services/new-api/` — **AGPL-3.0**（© New API contributors）
- `services/opencode2api/` — **GPL-3.0**（© jasonxu114514）

聚合与部署脚本部分以 AGPL-3.0 提供。

***REMOVED******REMOVED*** 致谢

- [New API](https://github.com/QuantumNous/new-api) — 网关主体
- [opencode2api](https://github.com/jasonxu114514/opencode2api) — 上游适配器
- [Hermes](https://github.com/) — QQ 通知通道