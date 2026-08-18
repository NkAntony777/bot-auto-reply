# 调研报告：agent 框架迁移方案（自研 harness → 成熟框架）

> 调研日期：2026-08-18｜评审修订：同日（外部 agent 交叉评审，5 条修正已吸收）
> 背景：Phase 1 自研 agent 循环已上线，但为长期演进考虑底层框架化
> 结论先行：**分层演进——近期 PydanticAI v2 换脑（仅慢路径）、中期工具层 MCP 化、远期可选 Pi 双脑实验**。
> LangGraph 不推荐现阶段引入。

---

## 1. 先说清楚：现在到底"脆弱"在哪

把现状拆开看，脆弱点分两类，**框架迁移只解决第一类**：

| 类别 | 具体问题 | 换框架能解决吗 |
|---|---|---|
| **脑（agent 循环）** | 手写 fallback 链、手写 tool_calls 解析（StepFun 空响应重试、MiniMax 格式兼容都是打补丁）；工具参数校验裸 json.loads；无瞬时错误重试策略；无结构化输出；上下文每次从零拼（无会话状态持久化）；无评测/回归测试 | **能**——这正是成熟框架的日常维护项 |
| **身体（微信自动化）** | Qt 渲染僵死、zstd 压缩消息、侧边栏裁剪、剪贴板时序、DPI 往返…… | **不能**——这是本项目的核心复杂度，任何框架都不管；且它必须留在 Python/Win32（ctypes、pyautogui、ImageGrab、wechatauto 内存提 key） |

所以迁移的正确形态是**只换脑、身体不动**。这也决定了评估标准：框架必须能低摩擦地调用现有 Python 工具函数。

## 2. 硬约束（筛掉一半候选）

1. **身体在 Python/Win32**：脑若不在 Python，就必须跨运行时调 wxapi HTTP（另一 agent 已建成 127.0.0.1+token 接口，这个桥是现成的）
2. **触发模式是短生命周期 run**：每条入站消息一次独立 agent run（max 5 轮），不是长会话——框架要支持低开销的一次性执行，会话持久化是加分项不是必需
3. **LLM 通道**：StepFun（OpenAI 兼容 + thinking 不可关 + 思考计 token）+ MiniMax fallback——框架必须支持自定义 base_url + 透传非标参数（`thinking`）
4. **部署简单性**：现在 `git pull + python wxbot.py` 就能跑，单 venv——引入 Node/重依赖要有足够回报
5. **预算控制**：send_message 每入站 1 次、tool_budget=8 这些**安全语义必须原样保留**（这是产品约束，不是技术选型）

## 3. 候选评估

### 3.1 PydanticAI v2（Python）—— ★ 推荐近期采用

- **现状**：2026-06-23 出 v2.0 稳定版；typed agent loop，"every model a string swap away"
- **适配度**：
  - `OpenAIProvider(base_url=...)` 原生支持 StepFun/MiniMax 自定义端点
  - 工具就是普通 Python 函数 + 类型注解——现有 5 个内部工具**原样注册**
  - 原生 retries（校验失败/瞬时错误）、usage limits（可表达 tool_budget）、结构化输出
  - v2 的 capability 原语（tools+instructions+hooks+guardrails 打包）正好对应我们的人格×工具集组合
  - 同 venv、纯 Python、零运行时分裂
- **风险**：v2 三个月一次破坏性变更窗口（锁版本即可）；`thinking` 非标参数透传需验证（extra_body）
- **迁移量**：一个晚上。`agent.engine: "builtin" | "pydantic"` 配置开关双实现灰度

### 3.2 Pi（TypeScript，pi.dev / pi-mono）—— 远期可选的第二大脑

- **现状**：Mario Zechner 的极简 agent harness，OpenClaw 的底座；extensions 注册工具、skills 渐进加载、15+ provider、models.json 支持自定义 OpenAI 兼容端点
- **关键能力**：四种运行模式——TUI / Print-JSON（脚本）/**RPC（JSON over stdio，专供非 Node 集成）**/ SDK。RPC 模式意味着 Python daemon 可以把 pi 当子进程脑用
- **适配路径**：pi（脑） ↔ wxapi HTTP（身体）——桥已存在。extension 写 5 个工具调 wxapi 端点即可
- **成本**：双运行时（Node 子进程 + 版本管理）、每次 run 的进程/会话开销、TS 扩展调试链路、团队(我)上下文切换
- **什么时候值得**：想接入 pi/OpenClaw 生态的 skills、多 agent 编排、或想让 ZCode/pi 直接驱动 bot 身体时。**纯为了"更成熟"不值**——pi 的成熟度在 coding agent 场景，我们的场景（IM 消息驱动+视觉自动化）它一样没见过
- **建议**：做一个 1-2 晚的 spike（pi RPC + wxapi 打通一次排盘），验证后再决定要不要双轨

### 3.3 OpenAI Agents SDK（Python）—— 备选

轻、官方、handoffs 适合 Phase 3 的多人格路由。但更偏 OpenAI 生态假设，自定义 provider 体验不如 PydanticAI 一等公民。若 PydanticAI v2 迁移受阻可回退到此。

### 3.4 LangGraph（Python）—— 不推荐（现阶段）

复杂有状态工作流的标准答案，但我们的状态机还太简单（路由→循环→发送）。它的收益要到 Phase 3（主动消息调度、跨会话记忆图谱、多步规划）才显现，现在引入是**为未来的复杂度预付今天的认知税**。Phase 3 真到再评估。

### 3.5 MCP 工具标准化—— 正交且必做（中期）

不管脑用什么，把工具层 MCP 化是回报最稳的一步：
- 内部工具（查群史/成员/统计/预约发送/生图）+ antony 网关工具包成 **MCP server**（Python SDK 成熟）
- 之后任何 MCP 客户端（ZCode、pi、Claude Desktop、未来任何框架）都能驱动同一个 bot 身体
- **框架选择从此变成可逆决策**——这是对抗"框架押错宝"的根本手段

## 4. 推荐路线（三层演进）

```
现在:  poll ─→ wxbot_agent(自研) ─→ 工具(Python 函数)
                                  ↘ antony 网关

近期:  poll ─→ agent.engine 开关 ─┬─ wxbot_agent(自研, 保底)
                                  └─ wxbot_agent_py(PydanticAI v2)   ← 灰度并行
中期:  任意脑 ─→ MCP server(工具层) ─→ 身体(wxmini2/wxapi HTTP)
远期:  poll ─┬─ PydanticAI 脑（默认）
             └─ pi RPC 脑（spike 通过后，可选第二人格/生态实验）
```

### 阶段 A：PydanticAI v2 换脑（**2-3 晚**，仅慢路径）—— ✅ 已完工 2026-08-18

> 实际 1 晚完工（评审估 2-3 晚）：收口层重构后工具实现零改动复用是关键提速项。
> 交付：`wxbot_agent_py.py`（当前默认引擎）+ `run_fixtures.py`（route 12/12，
> full 双引擎 8/8 零 crash，塔罗 seed 跨引擎同牌验证确定性）+ 配置 `agent.engine`
> 一行回滚。快路径未动。

**范围铁律：快路径不进框架。** `reply_dispatch` 的快路径（单轮 `llm_reply`，
承担 ~90% 群聊斗嘴）保持裸调用原样不动——换脑的只有 `agent_reply` 慢路径。
路由判定逻辑（快/慢分流）也留在框架外共用，避免迁移顺手把快路径"框架化"
引入延迟回归。

慢路径里的非标语义在框架中的落点（评审确认的坑位清单）：

| 现有语义 | PydanticAI 表达 | 风险 |
|---|---|---|
| 工具是 ctx 闭包（outbound/sent_count/img_stem 每 run 状态） | `RunContext[Deps]` 注入，Deps 每 run 新建 | 低，机械改写 |
| send_message 预约发送 + outbound 覆盖 final_text + [IMG:] 兜底 | **留在框架外的收口层**（两个引擎共用同一份 `_finalize_reply`） | 中——收口层要做模块级重构共享 |
| "最后一轮撤工具逼收口""预算耗尽回填提示" | UsageLimits 超限捕获后自己补一次无工具收口调用 | 中 |
| StepFun 空响应（思考烧完 token） | output validator 抛 ModelRetry 接 retries（框架不自动做） | 中 |
| fallback 链 | `FallbackModel`（好消息：不用手写） | 低 |
| thinking 透传 | OpenAI provider `extra_body`（需实测） | 低 |

- 配置 `agent.engine: "builtin" | "pydantic"`，按会话灰度切换
- 回滚：开关拨回 `builtin`，一秒回滚

**阶段 A.5（并行做）：fixture 语料库。** 双引擎灰度期间把 wxbot_run.log 的
输入（conversation/inbound/ctx_lines/is_group/username）+ 双路输出固化成
fixture 集（目标 ≥20 条，覆盖：八字/塔罗/查群史/画图/网关挂/发送预算/空响应/
快路径样例），落 `_fixtures/` + `run_fixtures.py` 重放脚本。以后每次框架升级
重放一遍——顺手解决"无评测"痛点。

### 阶段 B：工具层 MCP 化（1-2 晚，**先定 ctx 状态方案再动手**）—— ✅ 已完工 2026-08-18
- **ctx 状态方案（定案）**：server 侧会话态——`begin_run(conversation)` → run_id →
  ctx（TTL 30min），`end_run` 返回收口摘要（outbound/img_stem/计数）。只读查询
  无状态直传 conversation；预算检查在工具实现层（换入口绕不过）
- **预算语义已在 MCP 层强制**：send_message 每 run 1 次（实测第 2 次被拦）、
  tool_budget 计数、操作员直发 send_text/send_image 60s 限速
- 交付 `wxbot_mcp.py`：12 工具（run 生命周期 2 + 只读查询 4 + 预算类 3 + 操作员直发 2 + 目录 1），
  streamable-http 绑 127.0.0.1:8766；客户端集成测试全绿（含真实网关塔罗、
  实弹 send_text DB 确认）
- ZCode 接入：用户级 mcp 设置加 `{"mcpServers": {"wxbot": {"type": "http",
  "url": "http://127.0.0.1:8766/mcp"}}}`
- wxapi HTTP 与 MCP server 并存（前者给脚本/curl，后者给 agent 客户端）

### 阶段 C：Pi spike（可选，严格 timebox 1-2 晚）
- pi RPC 模式 + extension 调 wxapi HTTP，跑通一次"排八字并发群里"
- **评测方法（评审修正）**：我们的触发模式是每条入站消息一次短 run——
  spike 必须按"**常驻子进程 + 多次 run 复用**"测延迟（pi RPC 会话保持），
  不能按 per-run spawn 子进程测，否则消息频率会把进程开销放大，1.5 倍标准
  轻松不达标，结论失真
- 评估：常驻复用下的 per-run 延迟、StepFun thinking 兼容、extension 维护成本
- 通过标准：端到端延迟 ≤ PydanticAI 脑的 1.5 倍且功能不缺；不达标就留在调研报告里吃灰

## 5. 明确不做的事

- **不迁 LangGraph**：等 Phase 3 的状态复杂度真出现再说
- **不把身体迁出 Python**：wxmini2 的 500 行 Win32 血泪史是资产不是负债
- **不为了框架改产品语义**：预算/预约发送/人设注入的优先级高于任何框架的"标准做法"，框架适配我们，不是反过来

## 6. 引用

- [Pi 官网](https://pi.dev/)｜[pi-mono 仓库](https://github.com/earendil-works/pi)｜[Armin Ronacher: Pi: The Minimal Agent Within OpenClaw](https://lucumr.pocoo.org/2026/1/31/pi/)
- [PydanticAI v2 发布](https://pydantic.dev/articles/pydantic-ai-v2)｜[Model Providers](https://pydantic.dev/docs/ai/models/overview/)｜[升级指南](https://pydantic.dev/docs/ai/project/changelog/)
- [2026 框架对比 (AgentMail)](https://www.agentmail.to/blog/best-ai-agent-frameworks-2026)｜[6 框架实测 (Towards AI)](https://pub.towardsai.net/i-compared-6-python-ai-agent-frameworks-so-you-dont-have-to-langgraph-vs-crewai-vs-pydanticai-vs-d8a5e6e43262)
