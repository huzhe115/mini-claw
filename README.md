# Mini-Claw — 从 0 到 1 复刻 OpenClaw 的常驻私人 AI 助手

比特鹰成长计划 · 综合实战。参考原版 [nanoclaw.dev](https://nanoclaw.dev/)(TypeScript)的**架构思想**,
用 Python 复刻:Agent 大脑来自项目 3(mini-claude),后端工程套路来自项目 4(ai-chat-backend)。

技术设计文档:桌面 `Mini-Claw技术文档.md`。

## 架构(Phase 3)

```
WebChat(网页) ┐
CLI(命令行)   ├─→ FastAPI 网关(认证/会话路由/SSE 流式桥接) ─→ Agent 大脑
Cron(报时员)  ┘        │                    (ReAct 循环 + 7 个工具 + 记忆)
                       ▼
                 PostgreSQL(会话/消息/排班表,重启不丢)
```

- **渠道**:网页和命令行两扇门,共用同一个网关 HTTP 接口——浏览器聊的会话,
  命令行能接着聊(换门不换线),服务重启会话还在
- **报时员(cron)**:到点自动以 cron 角色向目标会话注入消息并跑一轮 Agent,
  "主动来找你"的来源;用户正在聊时跳过不插话
- **记忆**:每次对话前召回相关记忆拼进 system prompt;回合结束后台提取跨会话
  事实,存 ~/.mini-claw/memory(对齐原版 OpenClaw 的文件式记忆)
- **网关**:token 认证(X-Gateway-Token)、会话管理、SSE 流式转发
- **Agent**:mini-claude 的核心循环迁移而来——模型要调工具时,后端替它执行
  (bash / read_file / write_file / edit_file / glob / grep / todo_write),
  结果回填继续思考;每步动作以事件形式流式推给前端
- **落库**:sessions(会话 + llm_messages JSONB)、messages(展示消息 + meta)、
  cron_jobs(排班表);每次 Agent 运行的模型、token 用量、耗时记进 meta

## 启动

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env   # 填 ANTHROPIC_API_KEY / GATEWAY_TOKEN / DATABASE_URL

# 数据库(本机 PostgreSQL):建库 + 迁移
.venv/Scripts/python -m alembic upgrade head

.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

- 网页:http://localhost:8000/ ,左下角填 `GATEWAY_TOKEN`
- 命令行(第二扇门):`.venv/Scripts/python -m app.cli`
  - 直接回车继续上次会话;`--new` 新建;`--list` 手动选;`--session <id>` 指定
- 定时任务(报时员),创建后到点自动在会话里出现:
  ```bash
  curl -X POST http://localhost:8000/api/cron \
    -H "X-Gateway-Token: <你的 token>" -H "Content-Type: application/json" \
    -d '{"session_id": "<会话 id>", "prompt": "每天早上 8 点给用户发天气预报",
         "schedule": "0 8 * * *"}'
  # 立即触发一次验证效果:POST /api/cron/<job_id>/run
  ```
- API 文档:http://localhost:8000/docs

## 测试

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

测试用假 LLM(按脚本出牌,内容块是真实 SDK 类型)替换真模型,不联网也能验证
ReAct 全链路;数据库用独立的 `mini_claw_test` 库,每个测试前重建表。

## 目录结构

```
app/
├── main.py            # FastAPI 入口:挂网关路由 + 静态聊天页
├── config.py          # 配置(.env)
├── cli.py             # CLI 渠道:和网页共用网关,换门不换线
├── db.py              # SQLAlchemy 异步引擎/会话工厂
├── models.py          # ORM:sessions、messages
├── gateway/           # 网关:auth.py token 认证,router.py 会话接口 + SSE 桥接
├── core/sessions.py   # 会话存储(PostgreSQL 版,接口与 Phase 1 内存版相同)
└── agent/             # 大脑(从 mini-claude 迁移)
    ├── tools.py       # 工具三件套:TOOLS schema + run_* 实现 + 分发表
    ├── llm.py         # Anthropic SDK 封装(DeepSeek 兼容端点)
    └── loop.py        # ReAct 主循环:调模型→执行工具→回填,事件回调输出
alembic/               # 数据库迁移(0001: sessions + messages)
static/index.html      # 聊天页:会话列表 + 打字机流式 + 工具调用轨迹展示
tests/                 # pytest 21 例:网关/流式链路/持久化/CLI/工具与安全闸门
```

## 数据库设计

| 表 | 存什么 | 关键字段 |
|---|---|---|
| sessions | 会话本体 | id、title、llm_messages(JSONB,喂给模型的原始对话)、created_at |
| messages | 展示消息 | session_id、role、payload(JSONB,user 文本/assistant 步骤列表)、meta(模型/token/耗时) |

两份数据各存各的互不翻译:llm_messages 给模型看,payload 给人看。

## 已知简化(ponytail 标注)

| 简化 | 说明 | 升级路径 |
|---|---|---|
| 固定 token 认证 | 单用户够用 | 多用户时上 JWT(ai-chat-backend 有现成写法) |
| 权限闸门只留硬拒绝表 | web 场景无交互确认,危险命令默认拒绝 | Phase 3 做工具权限配置 + 审批流 |
| 客户端断连后 Agent 线程继续跑完 | 结果照常进会话,只是没人收事件 | Phase 3 任务队列统一治理 |
| 同步 Agent 跑在线程池 | to_thread + Queue 桥接,代码少 | 并发上来再改 AsyncAnthropic |
| 会话锁是进程内注册表 | 单进程网关够用 | 多进程部署换分布式锁 |
| 测试直接建表不跑迁移 | drop_all/create_all 更快 | 表结构变化时与迁移文件对齐即可 |

## 下一步(Phase 4)

1. Docker Compose 一条命令起全环境(PostgreSQL + Redis)
2. Redis:任务状态、限流(等服务容器化后一起做)
3. GitHub Actions CI:ruff lint + pytest
4. README Prompt 工程章节(计划硬性要求:Prompt 结构说明 + 至少 3 个修改前后对比)

**RabbitMQ 结论**:个人助手没有真正的后台任务积压(Agent 跑在请求线程,cron 跑在
报时员),引入消息队列属于为用而用——计划已在项目 4(ai-chat-backend)练过 RabbitMQ,
本项目不强上。
