# OpenClaw — 从 0 到 1 写一个常驻私人 AI 助手

比特鹰成长计划 · 综合实战。参考原版 [nanoclaw.dev](https://nanoclaw.dev/)(TypeScript)的**架构思想**,
用 Python 复刻:Agent 大脑来自项目 3(mini-claude),后端工程套路来自项目 4(ai-chat-backend)。

技术设计文档:桌面 `OpenClaw技术文档.md`。

## 架构(Phase 1)

```
浏览器(WebChat 渠道) → FastAPI 网关(认证/会话路由/SSE 流式桥接) → Agent 大脑
                                                                   (ReAct 循环 + 3 个工具)
```

- **渠道**:Phase 1 只有网页;CLI / 微信 / QQ 是后续 Phase 的门
- **网关**:token 认证(X-Gateway-Token)、会话管理(内存)、SSE 流式转发
- **Agent**:mini-claude 的核心循环迁移而来——模型要调工具时,后端替它执行(bash / read_file / write_file),
  结果回填继续思考;每步动作以事件形式流式推给前端
- **会话**:每条对话线独立存储;Phase 1 内存版,重启即失,Phase 2 落 PostgreSQL

## 启动

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env   # 填 ANTHROPIC_API_KEY 和 GATEWAY_TOKEN(默认已给开发值)

.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

浏览器打开 http://localhost:8000/ ,左下角填 `GATEWAY_TOKEN`(.env 里),开始聊天。
API 文档:http://localhost:8000/docs

## 测试

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

测试用假 LLM(按脚本出牌)替换真模型,不联网也能验证 ReAct 全链路。

## 目录结构

```
app/
├── main.py            # FastAPI 入口:挂网关路由 + 静态聊天页
├── config.py          # 配置(.env)
├── gateway/           # 网关:auth.py token 认证,router.py 会话接口 + SSE 桥接
├── core/sessions.py   # 会话存储(Phase 1 内存版)
└── agent/             # 大脑(从 mini-claude 迁移)
    ├── tools.py       # 工具三件套:TOOLS schema + run_* 实现 + 分发表
    ├── llm.py         # Anthropic SDK 封装(DeepSeek 兼容端点)
    └── loop.py        # ReAct 主循环:调模型→执行工具→回填,事件回调输出
static/index.html      # 聊天页:会话列表 + 打字机流式 + 工具调用轨迹展示
tests/                 # pytest:网关/流式链路(假 LLM)/工具与安全闸门
```

## Phase 1 已知简化(ponytail 标注)

| 简化 | 说明 | 升级路径 |
|---|---|---|
| 会话存内存 | 重启即失 | Phase 2 换 PostgreSQL(接口不变) |
| 固定 token 认证 | 单用户够用 | 多用户时上 JWT(ai-chat-backend 有现成写法) |
| 权限闸门只留硬拒绝表 | web 场景无交互确认,危险命令默认拒绝 | Phase 3 做工具权限配置 + 审批流 |
| 客户端断连后 Agent 线程继续跑完 | 结果照常进会话,只是没人收事件 | Phase 3 任务队列统一治理 |
| 同步 Agent 跑在线程池 | to_thread + Queue 桥接,代码少 | 若并发上来再改 AsyncAnthropic |

## 下一步(Phase 2)

1. PostgreSQL + SQLAlchemy + Alembic:会话/消息落库
2. CLI 渠道(第二扇门,验证"换门不换线")
3. Redis:任务状态、限流
4. edit_file / glob / grep 工具从 mini-claude 搬回
