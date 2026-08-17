# wxbot 项目文档（数据库读 + 视觉发 混合架构）

> 本文档是当前架构的权威说明，描述 2026-08 大重构后的实际实现。
> README 中遗留的「UIA 驱动」描述已过时，以本文为准。
> 微信 PC 4.x（实测 4.1.12.55）· Windows · Python 3.11+

---

## 1. 一句话定位

微信 PC 4.x 群聊 AI 机器人（人设：**阿廖沙**，中华田园长毛奶牛猫，傲娇）。
**读消息走解密数据库（权威、精确），发消息走屏幕视觉自动化（OCR + 坐标点击）**，
中间由 LLM（StepFun 主通道 + MiniMax fallback）生成人设回复。

## 2. 架构与数据流

```
┌─────────────────────────── 每轮 poll（默认 5s）───────────────────────────┐
│                                                                          │
│  微信本地库 (SQLCipher)                LLM                               │
│  session.db ──→ db_sessions() ──→ 变化检测(最新消息指纹) ──→ llm_reply() │
│  message_*.db ─→ read_chat_db() ─→ 判边/判群/群员昵称 ──→ 人设/上下文    │
│        │                                  │                              │
│        │ wechatauto.WeChatDB              ▼                              │
│        │ (进程内存提key+解密+WAL增量合并)   回复文本(可分多句)             │
│        │                                  │                              │
│  ┌─────┴────────── 发送（仅此时碰鼠标键盘）──────────────────┐            │
│  │ 等用户键鼠空闲 → 窗口停靠右下角 → 侧边栏点击开会话        │            │
│  │ → 渲染自愈(点击唤醒→托盘复活→重启微信)                    │            │
│  │ → 点输入框 → Ctrl+V → Enter/Ctrl+Enter/发送按钮           │            │
│  │ → 像素判粘贴 → DB 精确匹配判送达 → 焦点还给用户            │            │
│  └──────────────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────────┘
```

模块职责：

| 文件 | 职责 |
|---|---|
| `wxbot.py` | 主守护：DB 驱动轮询、回复策略（冷却/免打扰/@门控/unlimited 群）、人格注入、分句发送、LLM fallback 与全局退避、日志 Tee |
| `wxmini2.py` | 视觉自动化：窗口管理（停靠/前台）、ImageGrab 截图、RapidOCR、侧边栏定位点击、发送验证链、渲染三级自愈、微信重启 |
| `wechatauto`（venv 包） | 微信 4.x 解密库：进程内存提取 SQLCipher key、库解密、WAL 增量合并、消息/会话查询 |
| `personas/*.md` | 人格定义（当前主力 `neko_cow.md`＝阿廖沙） |
| `wxbot_state.json` | 状态：每会话已见指纹、已回复列表、发送记录、回复时间戳 |

## 3. 关键设计决策（为什么这么改）

| 旧方案（纯视觉） | 问题 | 新方案（现实现） |
|---|---|---|
| OCR 会话列表驱动轮询 | 长群名截断（`阿布菠萝终极粉丝后..`）、灰色预览丢失、碎片噪声（`'19:5'` 被当会话） | **DB `session.db` 驱动**：全名 + username，配置精确匹配 |
| OCR 会话预览做变化检测 | `summary` 列常为空串，检测失效 | **最新消息指纹**（`ts+side+text[:60]`） |
| UIA/启发式判群 | 微信 4.x 无 UIA 树；名称关键词误判 | **`username.endswith('@chatroom')`** 权威判定 |
| filehelper sender_id 探测 | `SenderName2Id` 映射表为空时全错；`sid=2=自己`惯例在本机是反的（实测 2 是别的群员，**3 才是自己**） | 优先查映射 → 兜底 filehelper 探测（自 sid 缓存）；群消息剥离 `wxid_xxx:\n` 前缀并反查群员昵称 |
| `read_chat` 比较中文类型名与 `1` | `"文本"==1` 恒假，对方消息永远匹配不上 | 类型映射表：文本→text、图片→image、文件/链接/卡片→file… |
| Ctrl+F 搜索定位会话 | 搜索面板截图抓不到；escape/搜索组合键疑似触发渲染挂死 | **侧边栏直接点击**（ImageGrab 扫描 + 坐标），Ctrl+F 已移除 |
| PrintWindow(pw_shot) 截图 | 对输入框等独立渲染层是**盲区**（截出空白），验证全错 | 验证类截图一律 **ImageGrab（屏幕截取）**；pw_shot 仅用于调试 |
| OCR 文字匹配做发送验证 | 小窗口误字率高（`叫→啪`、`廖→膝`），一半字读错 | **像素判粘贴**（暗像素增量，阈值随文本长度缩放）+ **DB 精确文本匹配判送达**（零 OCR 依赖） |
| 不检查 send_text 返回值 | 谎报成功，state 记假账 | poll 检查返回值；部分成功也算完成（防重复回复） |

## 4. 微信 4.x 踩坑实录（时间换来的，改代码前先读）

1. **Qt/WebView 渲染挂死（最高频）**：切会话后聊天区甚至全窗一片空白、点击无响应，
   用户手动点一下才能复活；偶发恶化到点击也救不回（渲染表面死亡，进程还活着、UI 线程也响应）。
   → **三级自愈**：中性位置唤醒点击 → **点托盘微信图标复活**（最有效）→ `taskkill` 重启微信（约 90s，最后手段）。
2. **PrintWindow 盲区**：输入框内容、搜索面板不随主窗口 HDC 渲染，`pw_shot` 截不到；
   聊天气泡区反而能截到。凡是要"看界面当下状态"的场景必须 ImageGrab（前提：窗口在屏内+前台）。
3. **HDC 64 位句柄溢出**：`GetWindowDC` 返回值偶尔超 32 位，ctypes 默认按 int 截断 →
   `int too long to convert` 间歇性崩溃。必须给 `_GetWindowDC.restype=c_void_p`、
   `_ReleaseDC/_PrintWindow.argtypes` 显式声明。
4. **多 TextIOWrapper 覆盖写**：对同一 `stdout.buffer` 建第二个 wrapper（`io.TextIOWrapper(sys.stdout.buffer,...)`），
   两个 wrapper 各记各的写指针互相覆盖 → 输出随机丢失/截断。教训：只 `reconfigure`，不要替换；
   守护进程日志用 `_Tee` 双写 `wxbot_run.log`。
5. **DB 副本同步**：读的是解密副本，`_open` 每次按源库/WAL 的 mtime+size 做增量合并；
   新消息落库有 **数秒到 30 秒延迟**，发送后的 DB 送达确认要轮询等待，别只查一次。
6. **消息库分片**：`message_N.db` 按 md5 表名分片，`list_message_chats()` 可查哪些会话本地有表；
   没有本地历史的会话读出来是空（正常跳过）。
7. **Enter 不发送**：焦点漂移或微信发送键设置差异时 Enter 只换行不发送。
   发送链：`Enter → Ctrl+Enter → 重新聚焦后点「发送」按钮`；失败**不删草稿**（旧版会 Ctrl+A 全选删掉，
   表现为"全选又自己删掉但没发出"）。
8. **窗口被遮挡时的一切判读都是错的**：ImageGrab 截到的是挡在上面的窗口。
   每次截图判读前 `_ensure_fg` 确认微信在前台。
9. **群消息格式**：发送者非自己时 content 带 `wxid_xxx:\n` 前缀；自己的消息干净无前缀。
10. **OCR 可用窗口下限**：实测 1000px 宽（760px 时一半字读错）；验证逻辑不得依赖 OCR 文字精确性。

## 5. 运行手册

```bash
cd bot-auto-reply
.venv/Scripts/python.exe wxbot.py            # 守护进程（前台）
.venv/Scripts/python.exe wxbot.py --once     # 跑一轮就退出（调试用）
tail -f wxbot_run.log                        # 运行日志（_Tee 双写，终端+文件）
```

前置条件：
- 微信 PC 4.x 已登录且主窗口存在（daemon 启动时会自动停靠到屏幕右下角 1000×1150）
- LLM key 就位：`wxbot_config.json` 的 `llm.api_key`（内联，该文件已 gitignore）或环境变量

行为要点：
- **不抢输入**：发送前等系统键鼠空闲 ≥6s（最多等 2 分钟超时强发）；发完把焦点还给用户原窗口
- 群回复间隔 `unlimited_group_interval_s`（30s）；只回 10 分钟内的消息（陈旧保护）
- 全部 LLM 通道挂掉进入指数退避，期间不标已读不丢消息，恢复后自动补回

## 6. LLM 通道

| 通道 | 模型 | 说明 |
|---|---|---|
| 主 | StepFun `step-3.7-flash` | `api.stepfun.com/step_plan/v1`；**推理型模型，思考也计 token，`max_tokens:2400` 才够复杂任务**（实测简单聊天思考约 200-400 tokens；复杂推理任务思考 1200+ tokens，900 上限会被截断；400 连正文都没有）；thinking 无法关闭（官方只提供 effort 档位且实测无效，全系列都思考） |
| 备 | MiniMax `minimax-m3` | 主通道故障自动接管（实测 401 → 1.6s fallback）；`thinking:{"type":"disabled"}` 生效 |

换主模型：改 `llm.model`（`step-3.5-flash` 思考更短更快、稍直白）。key 支持内联 `llm.api_key`
或 `api_key_env` 环境变量；fallback 同理（`fallbacks[].api_key / api_key_env`）。

## 7. 配置速查（wxbot_config.json）

- `reply.unlimited_groups`：免 @ 无条件回复的群（**必须与 DB 全名完全一致**）
- `reply.group_persona` / `personas.per_group`：群 → 人设映射
- `reply.context_messages`：每会话上下文条数（default + 按群覆盖）
- `reply.deny_contacts`：文件传输助手/微信团队等内置入口，绝不回复
- `personas.behaviors`：@/表情/贴纸/图片/引用 的 0~1 频率（发送时掷骰子节流；贴纸/表情/图片发送当前为 stub，日志会提示跳过）
- `state_file`：状态持久化（seen 指纹 / replied / sent）

## 8. 已知限制

- 发送仍需真实鼠标键盘事件 + 微信前台可见（视觉方案本质），已用"等空闲+停靠+还焦点"把干扰降到最低
- 微信渲染挂死无法根治，靠三级自愈兜底（最坏重启约 90 秒）
- `send_image` / `send_sticker` / `send_emoji` / `quote_reply` 是占位 stub；贴纸目录
  `wxbot_images/stickers/catalog.json` 不存在时会打无害告警
- 对方发图目前只传占位文本给 LLM（DB 拿不到气泡截图，识图链路待接）
- state 的 seen 指纹以"最新消息"为键：批量离线消息涌来时只处理最新一条（设计取舍，防刷屏）
