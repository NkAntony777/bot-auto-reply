# wxbot

微信 PC 版自动读消息 + 自动回复机器人。

**当前架构（2026-08 重构后）：读消息走微信本地解密数据库（SQLCipher，权威精确），
发消息走视觉自动化（ImageGrab + RapidOCR + 坐标点击），LLM 双通道（StepFun 主 + MiniMax 备）。**
主力人设：阿廖沙（中华田园长毛奶牛猫，傲娇）。

> ⚠️ 架构细节、微信 4.x 踩坑实录与运行手册见 **[docs/PROJECT.md](docs/PROJECT.md)**（权威文档）。
> 本文以下部分为历史版本说明，如与 PROJECT.md 冲突，以 PROJECT.md 为准。

> 面向微信 PC 4.x，Windows 平台，Python 3.11+。

---

## 功能特性

- **全自动收发**：轮询会话列表 → 发现新消息 → 点开会话读气泡 → LLM 生成 → 按真人节奏分句发送
- **智能判边**：截图按背景色 + 气泡位置判断「自己发的 / 对方发的」，只在对方有新消息时回复（带预览反匹配兜底，深/浅主题自适应）
- **群聊门控**：默认只在 `[有人@我]` 时回复；白名单群（`unlimited_groups`）可无条件自动回复，带独立冷却
- **私聊门控**：独立冷却时间 + 免打扰时段（支持跨夜）
- **人格系统**：不同群 / 私聊可指派不同人格（蒸馏自语料的 `.md` 文件），说话风格按人格走
- **特殊能力**（模型按需触发）：
  - `@昵称` 真 @ 群成员
  - `[Q]` 引用对方消息再回复
  - `[IMG:关键词]` 从图片库发图
  - `[EMOJI:表情名]` 发微信自带表情
  - `[STICKER:编号或关键词]` 发「爱心」收藏里的自定义表情包贴纸
  - 对方发图 → 自动识图（vision 模型描述）；对方发文件 → 自动读取内容（docx/pdf/xlsx/md/txt…）
- **攒批等待（仿真人节奏）**：对方连发消息时先不回——等静默 `settle_s` 秒（被叫到名字时等更短的 `settle_s_called`，反应更快）或累计 `settle_max_s` 封顶后通读整段再回一次，不再来一条回一条
- **叫名字必回**：消息里出现 `reply.call_names`（如 阿廖沙/沙沙）或账号昵称 = 一律回复——绕过快筛、群聊不开 @ 徽章也算被点名、主模型想 [SKIP] 会被强制重新生成
- **快筛 gate**：极速小模型（默认 `step-3.5-flash`）先判断消息值不值得回，不值得就不惊动主模型（fail-open：快筛挂了照常走主模型；对线目标/叫名字不经过快筛）
- **主动说话**：白名单对话静默够久时，主模型决定要不要自然地开个口（关心/追问后续/分享）；带单聊间隔、每日上限、时段、免打扰、"上次没人理就不再推"多重保守限制，默认关闭
- **行为旋钮**：`@` / 表情 / 贴纸 / 图片 / 引用 各有一个 0~1 频率，发送时硬性掷骰子节流，避免机械感
- **对话记忆（双层）**：mem0 语义记忆（每 N 轮 LLM 提取事实+自动去重更新，本地 qdrant 向量库，回复前按当前消息语义检索 top-k 注入；embedding 走 MiniMax embo-01）+ markdown 记忆兜底/镜像（`workspaces/<对话>/`，人类可读可手改）
- **上下文按需读取 + 自动压缩**：只追溯要回复的那个窗口所需的历史条数；超预算按词元压缩（截断旧消息 → 丢最旧）
- **多通道 LLM fallback + 全局退避**：主模型挂了逐个试备用通道；全挂进入退避、**不丢消息**，网络恢复自动重试
- **UIA 自愈**：微信 UIA 树挂死时自动重启微信进程恢复
- **单实例守卫**：启动即清理所有旧 `wxbot.py` 进程（含其他解释器起的老实例），多开竞争从机制上杜绝
- **本地监测看台**：`http://127.0.0.1:8788` 只读看台——运行状态/心跳/LLM 退避/日志尾实时刷新，bot 挂了也能看到"已停止"；随守护进程自动拉起
- **Web 控制台**：改配置、看状态、看日志、管理人格/贴纸/行为旋钮，暗夜/白天双主题

---

## 架构

```
wxbot.py        主守护进程：DB 驱动轮询 / 回复策略 / 人格注入 / 能力分发 / 退避重试 / 单实例守卫 / 日志 Tee
wxbot_dashboard.py 本地只读看台（127.0.0.1:8788）：状态卡片 + 日志尾，读文件驱动，bot 挂了也可观测
wxmini2.py      视觉自动化库：ImageGrab+RapidOCR / 侧边栏定位点击 / 发送验证链 / 渲染三级自愈 / 微信重启
wxmini.py       旧版 UIA 库（保留兼容）
wxbot_files.py  文件消息读取：定位微信文件存储 + 按类型解析（docx/pdf/xlsx/xls/md/txt）
wxbot_memory.py 记忆系统（双层）：mem0 语义记忆（默认，本地 qdrant+SQLite，MiniMax embo-01 embedding 适配器）+ markdown 记忆兜底/镜像（workspace 骨架 + system 注入 + LLM 事实提取）
wxbot_context.py 输入缓存 + 词元估算 + 上下文压缩
wxbot_stickers.py 贴纸目录重建（截图 + vision 建档 catalog.json）
personas/       人格文件（每个 .md = 一个人格，文件名即人格名；含示例 wen.md）
prompts/        base.md —— 所有对话共用的底层行为准则（可编辑）
wxbot-gui/      本地 Web 控制台（Express + TS + esbuild）
workspaces/     对话级记忆（运行期生成，不入库）
wxbot_images/   发图用的图片库（文件名含关键词）；stickers/ 存收藏贴纸截图与目录
```

---

## 快速开始

### 1. 准备

- Windows + 微信 PC 4.x，已登录、窗口可见（最小化到托盘会导致截屏/UIA 异常）
- Python 3.11+
- 装依赖：`wxauto4`（UIA 绑定）、`Pillow`（截屏判边/识图）、`curl_cffi`（绕 Cloudflare，强烈建议）

```bash
pip install wxauto4 pillow curl_cffi
```

### 2. 配置

复制样例并改关键项：

```bash
cp wxbot_config.example.json wxbot_config.json
```

必须改的：

- `llm.base_url` / `llm.model` / `llm.api_key_env`：LLM 接口 + 模型 + key 所在环境变量
- `reply.group.mention_names` / `own_nicknames`：你的微信昵称（决定「谁 @ 我」）
- `reply.unlimited_groups`：需要无条件自动回复的群（慎用）

### 3. 运行

```bash
python -X utf8 wxbot.py          # 常驻（启动自动清理旧实例，重复启动安全）
python -X utf8 wxbot.py --once   # 只跑一轮，调试用
```

停止（安全退出：保存状态、微信从虚拟屏还回主屏、停看板）：

```bash
stop_wxbot.bat                   # 双击即可；优雅退出优先，超时询问是否强杀
python -X utf8 stop_wxbot.py --status   # 只查看进程/微信位置，不动手
```

本地看台（只读监测，随 wxbot 自动拉起；也可独立跑 `python wxbot_dashboard.py`）：

```
http://127.0.0.1:8788            # 状态卡片 + 日志尾实时刷新
```

Web 控制台（可选，改配置/看日志/管人格贴纸）：

```bash
cd wxbot-gui
npm install
npm run build    # esbuild 打包 public/app.ts → app.js
npm start        # http://127.0.0.1:7931
```

---

## 配置速览

完整字段见 `wxbot_config.example.json`，核心概念如下：

| 段 | 作用 |
|---|---|
| `llm` | 主模型（`base_url/model/api_key_env`）+ `fallbacks` 备用通道链 + `context_window` |
| `vision` | 识图模型 + 独立 fallback 链 |
| `reply.private` | 私聊回复：开关 / 延时区间 / 冷却 / 免打扰时段 / 黑白名单 |
| `reply.group` | 群聊回复：开关 / 是否要求 @ / 延时 / 黑白名单 |
| `reply.unlimited_groups` | 免 @ 白名单群 + `unlimited_group_interval_s` 群内冷却 |
| `reply.context_messages` | 每个会话追溯的历史条数（`{"default": 8, "某群": 30}`） |
| `reply.personas` | 人格：`per_group` / `per_contact` / `default` 指派，`definitions` 映射人格名→文件，`behaviors` 行为旋钮 |
| `reply.target_matcher` | 按群指定「对线目标」（`contains_all` 关键词全命中才算），目标发言强制反击不许 SKIP |
| `reply.settle_s` / `reply.settle_max_s` / `reply.settle_s_called` | 攒批等待：对方静默多少秒才回 / 连发时最多等多少秒 / 被叫到名字时等多少秒（`settle_s: 0` 关闭） |
| `reply.call_names` | 叫到这些名字一律回复（绕过快筛、群聊视同被 @），配合 `own_nicknames` / `mention_names` 一起识别 |
| `screener` | 快筛 gate：`model`（默认 step-3.5-flash，字段缺省继承主 llm 通道）、`timeout_s`、`max_ctx_lines`、`fallbacks` |
| `proactive` | 主动说话：`enabled`（默认关）、`conversations` 白名单、`min_silence_min`、`min_interval_min`、`max_per_day`、`active_hours` |
| `context.compression` | 上下文压缩：按百分比或词元预算，两阶段（截断→丢最旧） |
| `memory` | 记忆系统：`backend`（`mem0` 语义记忆 / `markdown` 旧方案）、`every_n_replies` 提取节奏、`search_top_k`/`search_max_chars`/`search_threshold` 语义检索参数、`mem0.llm` 提取 LLM（默认主通道）、`mem0.embedder`（默认 MiniMax embo-01） |
| `dashboard` | 本地看台：`enabled` / `port`（默认 8788），wxbot 启动时自动拉起 |
| `images` / `stickers` / `files` | 图片库 / 贴纸目录 / 文件解析上限 |

**API key 一律走环境变量**（`api_key_env` 指定的名字），或写入 `~/.openclaw/openclaw.json` 的 `env` 段，不落配置文件。

---

## 模型输出约定

模型回复里可以带标记触发特殊能力（每行一个，由机器人解析后执行）：

| 标记 | 效果 |
|---|---|
| `[SKIP]` | 不回复（这条消息不值得接话） |
| `@昵称 内容` | 第一句开头 = 真 @ 该群成员 |
| `[Q] 内容` | 第一句 = 引用对方那条消息再回复 |
| `[IMG]` / `[IMG:关键词]` | 发图（关键词匹配图片库文件名） |
| `[EMOJI:表情名]` | 发微信表情（如 `旺柴` / `捂脸`） |
| `[STICKER:编号或关键词]` | 发收藏贴纸 |
| `[AUDIO:文件标记]` | 发语音（先经 speak 工具 TTS 合成，以可内联播放的文件卡片发出） |

---

## 容错设计

- **LLM 多通道**：主通道 → `llm.fallbacks` 依次尝试；识别图走独立的 `vision.fallbacks`
- **全局退避**：所有通道都挂时进入退避（30s 起，指数增长，上限 300s），期间不开窗、不标已读，恢复后自动重试漏掉的消息
- **不丢消息**：LLM 失败 / 判边失败不会再误标「已读」而永久漏掉
- **UIA 自愈**：连续 N 次读不到会话列表 → 自动重启微信进程

---

## 安全与合规提示

- 这是以 **你的真人微信身份** 对外发声，务必控制回复范围：黑白名单、延时、概率旋钮、免打扰时段都别图省事关掉
- API key 走环境变量，别写进配置或代码；`.gitignore` 已排除 `wxbot_config.json`、`wxbot_state.json`、`*.log`、`wxbot_images/`、`workspaces/`
- 纯 UI 自动化仍有账号风险，请自行评估并遵守平台规则；本项目仅供学习研究

---

## License

MIT
