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
│  session.db ──→ db_sessions() ──→ 变化检测(最新消息指纹) ──┐             │
│  message_*.db ─→ read_chat_db() ─→ 判边/判群/群员昵称 ──┤             │
│        │                                                ▼              │
│        │ wechatauto.WeChatDB              wxbot_agent.reply_dispatch() │
│        │ (进程内存提key+解密+WAL增量合并)   ├─ 快路径: llm_reply()      │
│        │                                  │   (闲聊, 2-5s 体验不变)    │
│        │                                  └─ 慢路径: agent_reply()     │
│        │                                     (tools 循环: 查群史/统计/  │
│        │                                      antony.best 13 玄学工具/  │
│        │                                      send_message 预约发送)    │
│        │                                  │                              │
│        │                                  ▼                              │
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
| `wxbot_agent.py` | **agent 层路由与 builtin 引擎**：混合路由（快/慢双路径，快路径永远不进框架）、手写 tools 循环（builtin 引擎，保底可回滚）、内部工具 5 个（查群史/成员/统计/预约发送/AI 画图）+ antony.best 网关工具注入、**收口层 finalize_reply（双引擎共用：outbound 预约/[IMG:] 兜底/截断）** |
| `wxbot_agent_py.py` | **pydantic 引擎（阶段 A，当前默认）**：PydanticAI v2——typed 工具校验、FallbackModel（StepFun→MiniMax 零手写）、output validator+retries 治 StepFun 空响应、UsageLimits 兜底；预算回填在 `_budgeted` 包装层（先于框架触发）。`agent.engine` 一行切回 builtin |
| `wxbot_gateway.py` | antony.best 工具网关客户端：目录缓存、相关性挑选（路由信号）、curl_cffi/urllib 双通道、调用永不抛异常 |
| `wxbot_genimg.py` | **StepFun 文生图**（step_plan 套餐内，与 LLM 同 key 同域名）：`step-image-edit-2`，b64_json 落盘 `wxbot_images/generated/`，同 prompt 同 seed 一天内可复现，目录自动清理 |
| `wxmini2.py` | 视觉自动化：窗口管理（停靠/前台）、ImageGrab 截图、RapidOCR、侧边栏定位点击、发送验证链、渲染三级自愈、微信重启、**zstd 消息解压补丁** |
| `wxapi.py` | **操作 API 层**（2026-08-18）：固定坐标快路径 open_chat（DB 行号+标题模板验证，OCR 退兜底）、剪贴板发图/发文件（CF_DIB/CF_HDROP）、HTTP API（127.0.0.1+token+单飞锁）、CLI 直调；详见 `docs/WXAPI.md` |
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
| wechatauto 读长/多行消息得 `[文本]` 占位符 | 微信 4.x 会把长/多行文本、表情 XML 的 `message_content` **zstd 压缩**存储；`_extract_text_from_blob` 只是跳头找明文的启发式，压实的 blob 解不出 → 读不到内容、发送确认误报失败 | wxmini2 猴子补丁 `_friendly_content`：文本类先真解压（`zstandard` 包）；表情/图片保持占位符语义 |
| 停靠虚拟屏后侧边栏定位偶发 miss | 有新消息的会话顶到列表第一行，固定 `LIST_Y1=0.10` 上边距在小窗口（约 1000px 高）把首行群名切掉，OCR 只看到第二行预览 | `_find_sidebar_row` 常规裁剪 miss 后用 `Y1=0.02` 扩展裁剪再扫一遍 |

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
- 微信 PC 4.x 已登录且主窗口存在（daemon 会自动停靠，见下方"虚拟显示器"）
- LLM key 就位：`wxbot_config.json` 的 `llm.api_key`（内联，该文件已 gitignore）或环境变量

### 虚拟显示器（零桌面打扰方案）

已安装 Amyuni usbmmidd 虚拟显示驱动，系统常驻一块**物理上不存在的屏幕**
（本机：主屏 2560×1600@125% + 虚拟屏 1600×1200@100%，在主屏右侧 x≥2560）。
微信停靠在虚拟屏：操作系统认为它"可见"，ImageGrab 能截取，点击落在虚拟屏坐标——
**用户物理屏幕完全看不到微信、感受不到鼠标键盘**。

- 驱动安装备忘（已装好）：解压 usbmmidd_v2.zip → 管理员运行
  `deviceinstaller64 install usbmmidd.inf usbmmidd` + `enableidd 1`；
  分辨率用 `ChangeDisplaySettingsEx` API 调（`setresolution` 子命令在 v2 无效）
- 实现要点（wxmini2.py）：
  - `ImageGrab.grab` 默认只截主屏、副屏区域全黑——必须 `all_screens=True`
  - 跨屏移动触发 WM_DPICHANGED，应用自缩放（125%→100% 即 ×0.8）——park 两步法：
    先收敛尺寸、再纯移位置（尺寸不动不触发重缩放）；位置必须钳制到
    **目标监视器矩形**（EnumDisplayMonitors 实测；虚拟桌面边界盒在多屏高度
    不一致时给出错误高度，窗口掉屏外截出黑图）
  - 无副屏时自动退回"主屏右下角停靠"，行为不变

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

**agent 层（Phase 1，2026-08-18 上线）**：`wxbot_agent.reply_dispatch` 混合路由——
闲聊走原快路径（延迟不变）；网关相关性命中/路由关键词/点名问句走 `agent_reply`
tools 循环（StepFun 与 MiniMax 的 tool_calls 均为标准 OpenAI 格式，已实测）。
工具：内部 5 个（query_chat_history / query_member_info / query_group_stats /
send_message / **generate_image**）+ antony.best 网关 13 个玄学工具（`antony_` 前缀，
seed 自动按消息指纹注入保证同问同答）。send_message 是**预约发送**（每入站 1 次限额 +
内容过滤），真正发送仍走 poll 的 delay/分句/发送链，循环内不碰窗口。
generate_image 走 StepFun 同 key 生图（`wxbot_genimg`），回复带 `[IMG:gen_xxx]`
标记由 harness 兜底保证不丢，poll 的 IMG 分支对生成图跳过行为节流直发。
依赖新增：`pip install zstandard`（微信 4.x zstd 压缩消息解压）。

## 7. 配置速查（wxbot_config.json）

- `reply.unlimited_groups`：免 @ 无条件回复的群（**必须与 DB 全名完全一致**）
- `reply.group_persona` / `personas.per_group`：群 → 人设映射
- `reply.context_messages`：每会话上下文条数（default + 按群覆盖）
- `reply.deny_contacts`：文件传输助手/微信团队等内置入口，绝不回复
- `personas.behaviors`：@/表情/贴纸/图片/引用 的 0~1 频率（发送时掷骰子节流；贴纸/表情/图片发送当前为 stub，日志会提示跳过）
- `agent.*`：agent 层开关与预算——`engine`（`builtin` 手写循环 / `pydantic` PydanticAI v2，
  当前默认 pydantic，一行回滚）、`enabled`（总开关）、`max_rounds`（LLM 轮数，
  默认 5）、`tool_budget`（工具执行次数，默认 8）、`max_tokens`（agent 轮生成
  预算，默认 3600——思考型模型带工具结果的轮次 2400 不够会空响应）、
  `allow_send_message`（一键关掉自主发送）、`route_keywords`（慢路径触发词，
  跑一周再调；含画图触发词）
- `imagegen.*`：StepFun 文生图——`enabled`、`model`（step-image-edit-2）、
  `steps`/`cfg_scale`、`max_side`（发送前缩边）、`keep_files`（generated/ 保留张数）
- `gateway.*`：antony.best 工具网关（入口/token 文件/超时/每消息最多注入工具数）
- `state_file`：状态持久化（seen 指纹 / replied / sent）

## 8. 已知限制

- **跨屏 DPI 往返后的输入僵死**（2026-08-17 实测，**已修复 2026-08-18**）：
  bot 退出把微信从虚拟屏（100%）还回主屏（125%）后，偶发渲染正常但点击/关闭
  全部无响应（Qt 输入管线僵死）。修复：`restore_wechat_to_primary` 现在自动做
  一次托盘复活切换（隐藏→唤出）重置输入状态；托盘不可用时退化为最小化/恢复。
  托盘查找已支持 Win11 溢出弹窗（chevron 点开后再找，见 `_find_tray_wechat_button`）。
  根治建议（可选手动一次）：设置→显示→选中虚拟屏→缩放改 125%，与主屏一致后
  跨屏不再触发 WM_DPICHANGED。
- **虚拟显示器不保证跨重启持久**：Amyuni IDD 在重启/驱动宿主回收后可能掉线。
  **实测（2026-08-18）：驱动装好之后，重新挂载 `deviceinstaller64 enableidd 1`
  不需要管理员权限**（只有首次装驱动要 UAC）；重挂后用 `enableidd 0` 再
  `enableidd 1` 可清掉多挂的屏。已提供 `enable_virtual_display.bat`；
  bot 检测不到副屏时自动退回主屏右下角停靠模式，功能不中断。


- 发送仍需真实鼠标键盘事件 + 微信前台可见（视觉方案本质），已用"等空闲+停靠+还焦点"把干扰降到最低
- 微信渲染挂死无法根治，靠三级自愈兜底（最坏重启约 90 秒）
- **发图已可用（双实现）**：`wxmini2.send_image`（CF_DIB 剪贴板粘贴 → Enter →
  DB 图片消息确认，AI 画图链路 2026-08-18 实弹验证）与 `wxapi.py` 的
  `send_image`/`send_file`（同思路独立实现 + HTTP/CLI 封装）；`send_sticker` /
  `send_emoji` / `quote_reply` 在 wxmini2 仍是占位 stub；贴纸目录
  `wxbot_images/stickers/catalog.json` 不存在时会打无害告警
- 对方发图目前只传占位文本给 LLM（DB 拿不到气泡截图，识图链路待接）
- state 的 seen 指纹以"最新消息"为键：批量离线消息涌来时只处理最新一条（设计取舍，防刷屏）
