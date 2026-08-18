# 阿廖沙 Agent 化计划书与路线图

> 状态：规划中（2026-08-17 立项，暂不动代码）
> 前置：数据库读 + 虚拟屏视觉发 + StepFun/MiniMax 双通道已稳定运行（见 PROJECT.md）

---

## 1. 现状与差距

| 维度 | 现状 | Agent 化后 |
|---|---|---|
| 决策 | 单轮 LLM 调用（chat completion） | tool-calling 循环：模型自主决定查什么、做什么 |
| "工具" | 输出标签 + 正则解析（`[Q]`/`[EMOJI]`/`[IMG]`），模型拿不到执行结果 | 真工具：调用→拿到结果→继续推理（限轮数） |
| 数据 | 每次回复注入固定条数上下文 | 模型按需查解密库（历史/成员/活跃度） |
| 外部能力 | 无 | antony.best 工具网关（占星/文档/计算...）、网页搜索等 |
| 主动行为 | 无（纯被动回复） | 定时任务、主动话题、提醒 |

**关键实测结论（升级可行性已验证）**：
- StepFun `step-3.7-flash` 完整支持 OpenAI 式 function calling
  （实测：正确定义工具后返回 `finish_reason: tool_calls`、参数 JSON 正确）
- MiniMax `minimax-m3` 作为 fallback 同样支持 function calling（待实测确认参数格式）

## 2. 总体架构（目标态）

```
微信(虚拟屏)                    antony.best
    │ 视觉发送/DB读取                │ Cloudflare Worker 网关
    ▼                               ▼
┌─────────────────── wxbot agent core ────────────────────┐
│  快路径（聊天斗嘴）: 单轮 LLM，2-5s，保持现有体验           │
│  慢路径（任务/数据问题）: tool-calling 循环（≤5轮）         │
│    ├─ 内部工具: query_history / query_member / send_msg   │
│    ├─ 网关工具: 动态发现(GET /api/v1/tools 按需注入)       │
│    └─ 记忆工具: 记事本读写(workspaces)                     │
└──────────────────────────────────────────────────────────┘
```

**混合路由是这个设计的灵魂**：群聊 90% 是斗嘴，单轮快路径维持体验；
消息命中"任务特征"（提问求据、查人查事、生成文档、占星等）才进 agent 循环。
路由本身先由规则+轻量分类（关键词），Phase 3 可交给模型自路由。

## 3. 分阶段路线图

### Phase 1 —— 原生 tool-calling 最小闭环（1 个晚上）

不引入任何框架，改造 `wxbot.py` 的 LLM 层：

1. 工具注册表（名称/描述/JSON schema/执行函数），首批 4 个内部工具：
   - `query_chat_history(member?, count?)` — 封装 `read_chat_db`
   - `query_member_info(nickname)` — 封装 sender 映射 + `get_nickname`
   - `query_group_stats(days)` — 活跃度统计（新写，SQL 聚合）
   - `send_message(text)` — 封装 `send_text`（发送成功与否回填给模型）
2. `llm_reply` → `agent_reply(conversation, inbound, tools, max_rounds=5)`：
   LLM 返回 tool_calls → 执行 → results 以 role=tool 回填 → 循环 → 最终文本
3. 每轮工具调用写日志（审计+调试）；循环超限强制收敛为纯文本回复
4. 验收：群里问"52 最近都聊了啥"→ 模型查库 → 引用真实内容回答

### Phase 2 —— antony.best 工具网关接入（**站侧已上线 2026-08-18**，bot 侧客户端已就绪 `wxbot_gateway.py`，待 Phase 1 循环接入）

**站侧改造（在 antony.best 仓库做，本计划书提供契约）**：

```
抽核：工具计算逻辑 → @antony/calc-core（纯函数包，零 DOM 依赖）
网关：Cloudflare Worker（站已在 CF）挂同一包

GET  /api/v1/tools
  → [{name, description, parameters(JSON schema), category, cost_hint}]
POST /api/v1/tools/:name     body: 参数 JSON → 结果 JSON
鉴权：Authorization: Bearer <静态token>；加简单限速
目录条目筛选：DOM/canvas 依赖的工具标记 gateway: false 不暴露
```

**bot 侧**：
1. `AntonyGateway` 客户端：启动拉目录 + 缓存（TTL 1h）
2. 动态工具注入：每轮按消息相关性（关键词/嵌入相似度，先用关键词）
   挑 top-N（≤6）个网关工具转成 LLM tools 定义——工具再多也不撑爆上下文
3. 网关调用超时 15s、失败优雅降级（回消息"工具暂时喵不动"）
4. 验收：群友发八字 → bot 调用站上八字/占星工具 → 人设化解读

### Phase 3 —— 主动性与记忆升级（一周内渐进）

1. **定时/主动**：内部 scheduler（APScheduler 或简单循环）：
   早安话题、群冷场 6h 主动冒泡（频率可配，防骚扰）
2. **记忆图谱**：workspaces 事实提取升级为带时间线的结构化记忆，
   注入 system 时做相关性检索而不是全量
3. **模型路由**：简单消息→快模型（step-3.5-flash），复杂任务→3.7，
   由首 token 分类或规则决定
4. **网页搜索工具**：接 search API（用户有 anysearch/zhihu-cli 基建可复用）

### Phase 4 —— 框架化重构（已调研，方案定稿 2026-08-18）

完整调研见 **docs/RESEARCH_AGENT_FRAMEWORK.md**，结论：三层演进——
- **A（近期）**：PydanticAI v2 换 agent 内核（`agent.engine` 开关灰度，工具原样注册）
- **B（中期）**：工具层 MCP 化（wxbot_mcp.py，框架选择从此可逆）
- **C（可选 spike）**：pi RPC 模式作第二大脑（wxapi HTTP 桥已就绪，1-2 晚验证）
- LangGraph 现阶段不引入；wxmini2/DB 层永远不动——执行层是资产，框架是耗材

## 4. antony.best 现状探测结论（2026-08-17 实测）

- 前端 SPA（Vite 构建，zh-CN，"智能工具工作台"），**无后端计算 API**
  （主包与 calculate/占星组件中 0 处 fetch 后端调用；/openapi.json 等路径
  均返回 SPA 兜底 HTML）
- 工具形态：纯前端 JS 计算（资产预载可见 exceljs、pdf-lib、doc-hub-skills、
  Vedic/Western Astrology、AiInterpretation、CityCoordinatePicker 等）
- 托管：Cloudflare（响应头 Server: cloudflare）→ Workers 网关顺理成章
- 结论：走"抽核 + Worker 网关"，无头浏览器方案仅作个别工具兜底

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| agent 循环拉高延迟（每轮工具+推理 3-15s） | 混合路由，聊天走快路径；循环限 5 轮 |
| 模型滥用工具刷请求 | 每条消息工具调用预算（如 ≤8 次），网关限速 |
| 群里执行副作用工具（发消息）出乱子 | send_message 每轮限 1 次；敏感工具加确认门 |
| 微信封号风险（本群刚聊过有人搞 bot 被封） | 维持拟人节奏/频率上限，不碰协议注入，主动消息低频 |
| 网关 token 泄漏 | 静态 token + IP 白名单可选；token 进 gitignore 配置 |
| Workers 内存/时长限制 | 大文件类工具单独评估；超时 15s 优雅降级 |

## 6. 决策点（需要拍板）

1. Phase 1 内部工具是否含 `send_message`（含=模型可自主发消息，刺激但需限流）
2. antony.best 网关改造在哪个仓库做（建议站仓库，calc-core 做成独立包）
3. Phase 3 主动冒泡的频率与时段（建议 ≤1 次/6h + 白天时段）
