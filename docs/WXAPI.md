# wxapi — 微信操作本地 API

> 2026-08-18 · 把 wxmini2 的视觉自动化升级为可编程操作层：
> **固定坐标快路径 + 剪贴板媒体发送 + HTTP API + 单飞锁**

## 定位

读消息走解密 DB（不变），**操作（打开会话/发文字/发图/发文件）走本 API**。
OCR 从主路径挤到兜底位：会话定位用「DB 行号 → 侧边栏固定坐标点击 + 标题模板比对验证」，
发图/发文件用剪贴板 `CF_DIB`/`CF_HDROP` 直接 Ctrl+V（零弹窗零坐标）。

## 用法

```bash
# 服务（默认 127.0.0.1:8765，token 在 wxapi_config.json，首次自动生成）
.venv/Scripts/python.exe -X utf8 wxapi.py --serve

# 标定（侧边栏行距 + 发送按钮；换窗口尺寸/微信改版后重跑）
.venv/Scripts/python.exe -X utf8 wxapi.py --calibrate

# 命令行直调（脚本化控制，不经 HTTP）
.venv/Scripts/python.exe -X utf8 wxapi.py --cli send-text 文件传输助手 "hello"
.venv/Scripts/python.exe -X utf8 wxapi.py --cli send-image 文件传输助手 D:/x.png
.venv/Scripts/python.exe -X utf8 wxapi.py --cli open 阿布菠萝终极粉丝后援团

# 端到端测试（对 filehelper 发文字/图/文件并做 DB 断言）
.venv/Scripts/python.exe -X utf8 tests/wxapi_test.py
```

## HTTP API（127.0.0.1 + token）

鉴权：`Authorization: Bearer <token>` 或 `X-Token` 头（`/health` 匿名只回极简信息）。
所有 UI 动作经全局单飞锁串行（物理鼠标只有一套）；锁忙时排队 ≤90s，超时 409。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 微信窗口/虚拟屏/登录态/layout 概览 |
| `/sessions?limit=` | GET | DB 会话列表（权威顺序） |
| `/chat?username=&limit=` | GET | 读指定会话消息（DB） |
| `/open` | POST | `{contact}` 打开会话（快路径，返回命中方式） |
| `/send_text` | POST | `{contact, text, wait_idle?}` 发文字，DB 确认 |
| `/send_image` | POST | `{contact, path}` 剪贴板 DIB 粘贴发图，DB 类型确认 |
| `/send_file` | POST | `{contact, paths[]}` 剪贴板 HDROP 粘贴发文件 |
| `/screenshot` | POST | 截微信窗口存盘，返回路径 |
| `/click` | POST | `{x, y}` 按窗口百分比坐标点击（通用控制原语） |
| `/hotkey` | POST | `{keys[]}` 组合键 |
| `/calibrate` | POST | 重标定 layout |

`contact` 接受 username（wxid/@chatroom/filehelper）、群名/昵称。

## 快路径机制与兜底链

```
open(contact)
 ├─ 路线0 标题模板/OCR 验证当前已打开 → 直接用（毫秒级）
 ├─ 路线1 DB 行号 → 固定坐标点第 N 行（±2 行纠偏：打开/读取会话会刷新
 │        sort_timestamp，DB 序与侧边栏序可能漂移 1~2 位）
 │        验证：标题区截图与缓存模板归一化相关（≥0.90 过）；无缓存/模糊时
 │        小区域 OCR 兜底，OCR 确认后回填模板（wxapi_titles/）
 └─ 兜底 Esc 关浮层 → wxmini2 OCR 扫描（open_chat_by_click）

send_text → 复用 wxmini2._send_text_core（粘贴像素判定 + Enter/Ctrl+Enter/
             发送按钮三级发送 + DB 精确匹配确认），注入快路径 opener
send_image/file → 剪贴板 → Ctrl+V → Enter（失败点发送按钮）→ DB 等待
                  对应类型(图片/文件)+自发侧消息
```

渲染检查一律用**安静轮询**（`_rendered_quiet`）——`ensure_chat_rendered` 的
唤醒点击+托盘复活只用于真正挂死场景；点错行停在「无会话打开」合法空白态时
误用托盘复活会把窗口反复开关，微信会趁机弹回主屏（实测事故）。

## 生产部署要点

1. **虚拟屏**：重启后虚拟屏可能消失（usbmmidd 不随开机自启）。恢复：
   管理员跑 `enable_virtual_display.bat`（工具包在 `%TEMP%\usbmmidd\usbmmidd_v2`，
   备份在 `E:\tmp_usbmmidd`）。启用后显示设置里确认虚拟屏为 1600×1200@100%。
2. **登录态**：强杀 Weixin.exe 数次后微信可能要求确认登录；
   `restart_wechat` 会自动点「进入微信」确认页，但**扫码页无法自动化**，
   `health.weixin.logged_in=false` 时需人工扫码。
3. **窗口最小化时 park 无效**（MoveWindow 对 -32000 坐标的窗口不起作用）——
   已在 `park_wechat` 里先 SW_RESTORE（2026-08-18 修复）。
4. 标定产物 `wxapi_layout.json` 绑定窗口尺寸；窗口尺寸变化会触发自动重标。

## 2026-08-18 实测记录

- 虚拟屏恢复 + 停靠 1000×1150 + 截图链路：✅
- 标定（行距 65px / row0 实测校验自动纠偏 +65px / 发送按钮 [0.94,0.964]）：✅
- HTTP：health / token 鉴权 401 / sessions / screenshot：✅
- open / send_text / send_image / send_file 端到端：**代码就绪，验收中断**——
  微信中途退到扫码登录页（多次强杀触发），扫码需人工。恢复登录后跑
  `tests/wxapi_test.py` 即可完成验收。

## 与 Phase 1 agent 的关系

`docs/TASK_PHASE1_AGENT_LOOP.md` 的 `send_message` 工具可直接调
`wxapi --cli send-text` 或 HTTP `/send_text`；`send_image/send_file` 是
Phase 1 之外的增量能力（旧 wxmini2 全是 stub）。
