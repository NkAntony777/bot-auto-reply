# 调研报告：agent 框架迁移方案（自研 harness → 成熟框架）

> 调研日期：2026-08-18｜背景：Phase 1 自研 agent 循环已上线，但为长期演进考虑底层框架化
> 结论先行：**分层演进——近期 PydanticAI v2 换脑、中期工具层 MCP 化、远期可选 Pi 双脑实验**。
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

### 阶段 A：PydanticAI v2 换脑（1 晚，风险低）
- 新建 `wxbot_agent_py.py`：`reply_dispatch` 同签名，内部用 PydanticAI Agent + OpenAIProvider(StepFun) + fallback 实例(MiniMax)
- 现有工具函数直接注册；send_message 预约/预算语义在外层保留（框架 usage limit 只做兜底）
- 配置 `agent.engine`，按会话灰度切换，wxbot_run.log 对比双引擎输出
- 验收：A1-A7 场景重跑全过 + thinking 参数透传验证 + 快路径延迟无回归
- 回滚：开关拨回 `builtin`，一秒回滚

### 阶段 B：工具层 MCP 化（1-2 晚）
- `wxbot_mcp.py`：FastMCP 包 5 个内部工具 + 网关转发 + send_image/send_text
- wxapi HTTP 与 MCP server 并存（前者给脚本/curl，后者给 agent 客户端）
- 顺带收益：ZCode 里就能直接调 bot 工具调试（我现在调试要跑 python -c，以后 /mcp 直调）

### 阶段 C：Pi spike（可选，1-2 晚）
- pi RPC 模式 + extension 调 wxapi HTTP，跑通一次"排八字并发群里"
- 评估：延迟（子进程+RPC 开销）、StepFun thinking 兼容、extension 维护成本
- 通过标准：端到端延迟 ≤ PydanticAI 脑的 1.5 倍且功能不缺；不达标就留在调研报告里吃灰

## 5. 明确不做的事

- **不迁 LangGraph**：等 Phase 3 的状态复杂度真出现再说
- **不把身体迁出 Python**：wxmini2 的 500 行 Win32 血泪史是资产不是负债
- **不为了框架改产品语义**：预算/预约发送/人设注入的优先级高于任何框架的"标准做法"，框架适配我们，不是反过来

## 6. 引用

- [Pi 官网](https://pi.dev/)｜[pi-mono 仓库](https://github.com/earendil-works/pi)｜[Armin Ronacher: Pi: The Minimal Agent Within OpenClaw](https://lucumr.pocoo.org/2026/1/31/pi/)
- [PydanticAI v2 发布](https://pydantic.dev/articles/pydantic-ai-v2)｜[Model Providers](https://pydantic.dev/docs/ai/models/overview/)｜[升级指南](https://pydantic.dev/docs/ai/project/changelog/)
- [2026 框架对比 (AgentMail)](https://www.agentmail.to/blog/best-ai-agent-frameworks-2026)｜[6 框架实测 (Towards AI)](https://pub.towardsai.net/i-compared-6-python-ai-agent-frameworks-so-you-dont-have-to-langgraph-vs-crewai-vs-pydanticai-vs-d8a5e6e43262)
