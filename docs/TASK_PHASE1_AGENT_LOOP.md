# 任务书：Phase 1 —— Tool-Calling Agent 循环

> 开工日期：待定｜预计工作量：1 晚
> 上游依赖：全部就绪（网关客户端 ✅ 输入僵死修复 ✅ 虚拟屏方案 ✅）
> 总路线：docs/AGENT_ROADMAP.md ｜ 架构现状：docs/PROJECT.md

---

## 0. 开工前检查清单（Pre-flight）

| # | 检查项 | 方法 | 状态 |
|---|--------|------|------|
| 1 | 虚拟屏已激活（监视器数=2） | 右键管理员运行 `enable_virtual_display.bat` | ☐ 手动 |
| 2 | （可选优化）虚拟屏缩放=125% | 设置→显示→选虚拟屏→缩放 125% | ☐ 手动 |
| 3 | 微信已登录、窗口存在 | 启动微信登录 | ☐ |
| 4 | 网关健康 | `python wxbot_gateway.py` 自测全绿（health/catalog/tarot） | ☐ 已验证过 |
| 5 | StepFun function calling | 已实测支持（`finish_reason: tool_calls` 正常） | ✅ |
| 6 | MiniMax function calling | **未实测**——开工时先用 curl 验证 m3 的 tool_calls 格式 | ☐ 待验 |

## 1. 目标

把"单轮 LLM 调用"升级为**带工具的 agent 循环**，让阿廖沙能：
- 查群聊历史/成员信息回答事实问题（"52 最近聊了啥"）
- 调用 antony.best 网关的 13 个玄学工具（群里发八字→现场排盘解读）
- （受限）自主发消息

**非目标**（本轮不做）：定时主动消息、记忆图谱、模型路由、多 agent——那是 Phase 3。

## 2. 改动范围

| 文件 | 动作 | 内容 |
|------|------|------|
| `wxbot_agent.py` | **新建** | 工具注册表 + agent 循环 + 混合路由（核心交付物） |
| `wxbot.py` | 小改 | `poll_once` 里 `llm_reply(...)` 调用点替换为混合路由分发 |
| `wxbot_config.json` + example | 小改 | 新增 `agent` 配置段 |
| 其余文件 | **不动** | 发送链/DB/虚拟屏/网关客户端全部复用 |

## 3. 任务分解

### T1 工具注册表（wxbot_agent.py）

```python
TOOLS = {}  # name -> {description, parameters, exec(args)->str, budget_cost}

def register(name, description, parameters, exec_fn): ...

# 内部工具（首批 4 个，全部只读除 T4）：
# 1. query_chat_history(member, count=5)
#    实现注：wxmini2._nick_cache 反查 wxid 有局限，直接用 read_chat_db 的
#    sender 字段过滤最近 50 条；找不到成员时返回提示文本让模型自己说
# 2. query_member_info(nickname)  → sender 映射 + get_nickname
# 3. query_group_stats(days=7)    → 新写：SQL 聚合每人发言数/最后活跃
# 4. send_message(text)           → wx.send_text；见 T5 安全限制
```

外部工具：`Gateway(cfg).llm_tools(消息文本)` 动态注入（已就绪），执行走
`gw.call(name, params)`，工具名统一 `antony_` 前缀（防撞名已处理）。

**工具结果给模型的格式**：截断到 2000 字/次（bazi 排盘 Markdown 可能很长，
超长截断+尾部标注"已截断"）。

### T2 Agent 循环（wxbot_agent.py）

```python
def agent_reply(cfg, conversation, inbound, ctx_lines, is_group,
                max_rounds=5, tool_budget=8) -> str | None
```

- OpenAI 格式 messages 累积：system（现有人设逻辑复用 `_build_system`）+ 上下文 + inbound
- 每轮带 `tools` 发给 `_llm_call` 的改造版（需新增透传 tools 参数，
  **保持 fallback 链**：StepFun 失败→MiniMax，注意 m3 的 tool_calls 字段格式差异）
- `finish_reason == "tool_calls"` → 执行 → `{"role":"tool","tool_call_id":...,"content":结果}`
  追加进 messages → 下一轮
- 每轮记日志：`[agent] round N tool=X(args摘要) → 结果Y字`
- 收敛条件（任一）：模型返回纯文本 / 达 max_rounds / tool_budget 耗尽
  （超限时追加 system "工具额度用完，请直接给最终回复"）
- **异常安全**：工具执行异常 → 以错误文本回填（不中断循环）；全链失败 → 返回 None
  （poll 侧已有的退避机制接管）

### T3 混合路由（wxbot_agent.py）

```python
def reply_dispatch(cfg, conversation, inbound, ctx_lines, is_group) -> str | None
```

- **快路径**（现状保留）：无工具命中信号 → 走原 `llm_reply`，2-5s 体验不变
- **慢路径**：满足任一 → `agent_reply`：
  a) 网关工具相关性命中（复用 `gw.llm_tools` 的 score>0 判断）
  b) 关键词：查/谁/最近/多少/统计/排盘/占卜/算/看看.*八字 …（初版关键词表，跑一周再调）
  c) 群内 @阿廖沙 + 问句（被点名提问大概率要查证）
- 首轮可全量走快路径灰度开关：`agent.enabled` + `agent.route_threshold`

### T4 send_message 安全限制（必须实现）

- 每条入站消息最多触发 **1 次** send_message（计数器在 agent_reply 局部）
- 内容过滤：发送前经 `max_reply_chars` 截断；正文含"系统/配置/API/token"直接拒绝
- 发送结果回填给模型（成功/失败），让它决定是否改口

### T5 poll_once 集成 + 配置

```jsonc
// wxbot_config.json 新增：
"agent": {
  "enabled": true,
  "max_rounds": 5,
  "tool_budget": 8,
  "result_max_chars": 2000,
  "allow_send_message": true,     // 决策点①：默认开，可随时关
  "route_keywords": ["查", "谁", "最近", "排盘", "占卜", "算一卦", "塔罗", "黄历", "八字", "紫微", "六爻"]
}
```

`poll_once` 改动点：`reply = llm_reply(...)` → `reply = wxbot_agent.reply_dispatch(...)`
（注意 `--once` 与守护两模式都要测；SKIP 语义保持：agent 路径返回字面 `[SKIP]` 同样生效）。

## 4. 验收标准（全过才算完）

| # | 场景 | 预期 |
|---|------|------|
| A1 | 群友发"52 最近都聊了什么" | 走慢路径→query_chat_history→回复引用真实聊天内容（对照 DB） |
| A2 | 群友发"帮我排个八字：2024-06-15 14:30 女" | 走慢路径→antony_bazi→猫设解读排盘（gateway 审计日志有记录） |
| A3 | 群友发"塔罗测下明天运势"（带消息 id seed） | antony_tarot + seed=消息指纹，同 seed 复现同结果 |
| A4 | 日常斗嘴（"喵喵喵"） | 走快路径，延迟与现状持平（≤6s），无工具调用日志 |
| A5 | 模型试图连发 3 条 send_message | 第 2 条起被预算拦截，最终只发 1 条 |
| A6 | 拔网线模拟网关挂 | 工具调用优雅失败→模型回复"工具暂时喵不动"→不 crash 不丢消息 |
| A7 | Ctrl+C 停止 bot | 微信归还主屏且**可正常点击**（输入重置生效） |

## 5. 已知坑（开工必读）

1. **StepFun 思考型**：tool_calls 轮也有 reasoning，`max_tokens: 2400` 别降；
   空 content 时先看 finish_reason 再判定（别误判失败）
2. **MiniMax m3 的 tool_calls 格式未验证**（Pre-flight #6）——若格式不同，
   `_llm_call` 的 fallback 判定要兼容两家
3. **工具结果超长**：bazi/紫微 Markdown 可达 4-6KB，必须截断（T1 已定 2000 字）
4. **网关限流 60 req/min**：tool_budget=8 上限天然安全，但别在循环里重试网关调用
5. **agent 循环期间微信窗口状态**：查库工具不碰窗口；只有 send_message 触碰
   ——慢路径回复的"拟人延迟"要加在最终 send 之前（沿用 poll 现有 delay 逻辑，不要在 T2 里自己 sleep）
6. **别动 wxbot.py 的发送/停靠逻辑**：那是稳定层，本任务只换"大脑"

## 6. 交付物清单

- [ ] `wxbot_agent.py`（注册表/循环/路由/预算）
- [ ] `wxbot.py` 调用点替换 + `--once` 验证
- [ ] 配置段 + example 同步
- [ ] 验收 A1-A7 全过（日志留档 `wxbot_run.log`）
- [ ] docs/PROJECT.md 架构图补一笔（快/慢双路径）
- [ ] git commit + push
