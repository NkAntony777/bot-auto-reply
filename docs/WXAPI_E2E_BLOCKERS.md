# wxapi 端到端验收：问题清单与接手记录

> 2026-08-18 15:05 更新 · **验收已 13/13 全过（连续两轮）**，见文末"五、15:05 收尾"。
> 以下为问题清单的原始记录 + 本次排查结论，供后续维护参考。

## 一、已交付且验证过的（不需要重做）

| 项 | 状态 | 证据 |
|---|---|---|
| 虚拟屏恢复（usbmmidd 1600×1200 @ x=2560） | ✅ | `wxapi_test.py` health 断言过；工具包备份在 `%TEMP%\usbmmidd\usbmmidd_v2` 和 `E:\tmp_usbmmidd` |
| 微信停靠虚拟屏 1000×1150 | ✅ | 多次 health/screenshot 验证 rect=[2860,25,1000,1150] |
| `wxapi.py` HTTP API（127.0.0.1:8765 + token + 单飞锁） | ✅ | **13/13 全过 ×2**（15:02、15:05） |
| 标定（行距 65px、row0 实测校验自动纠偏、发送按钮） | ✅ | `wxapi_layout.json` |
| 剪贴板发图（CF_DIB 24bpp）/发文件（CF_HDROP） | ✅ | e2e DB 断言通过 |
| wxmini2 修复（park 最小化 bug、发送按钮 crop 偏移、restart 登录确认页、**托盘图标定位重写**） | ✅ | 见 B2 |
| 文档 | ✅ | `docs/WXAPI.md` |

## 二、问题清单（含 15:05 排查结论）

### B1 微信反复掉到登录态 → 已有免扫码复活路径
- **原现象**：每次 `taskkill /F Weixin.exe` 后重启回登录页，后期变成扫码页。
- **15:05 结论**：**优雅关闭可以保住登录态**。对僵死窗口发 `WM_CLOSE`（窗口秒关、
  子进程残留无妨）→ 直接再启动 `Weixin.exe` → 新实例**直接进主界面，无需扫码**。
  今天实测一次成功。怀疑 `taskkill /F` 破坏会话状态文件才触发扫码验证，
  优雅退出则不会。`restart_wechat` 的强杀路径建议改为先 `WM_CLOSE` 等几秒再补刀。
- 掉登录检测进 `/health` 的 `logged_in` 字段仍然值得做（告警用）。

### B2 渲染"死循环"→ 重新定性：虚拟屏 Qt 间歇停绘/输入迟滞
- **15:05 结论**：今天反复出现的"整窗空白"大多是**停绘假死**，不是进程死：
  - ImageGrab 会拿到**冻结帧**（连续采样像素数恒定不变，如 133563 精确重复出现）或全白帧；
  - 点击在迟滞期被丢弃（点了侧边栏行但打不开会话），恢复期一切正常；
  - 纯 `force_foreground` 有时能唤回，**最可靠的复活是用户指出的托盘图标切换**
    （点开任务栏"^"溢出弹窗 → 点里面的微信图标：隐藏→唤出，强制重排渲染）。
- **已修的代码问题**：
  1. `revive_via_tray` 一直点错图标——旧 `_find_tray_wechat_button` 模糊匹配名字，
     先命中的是**任务栏固定按钮**（'微信 - N 个运行窗口'，点了只是最小化/聚焦）。
     已重写：优先开溢出弹窗（`TopLevelWindowForOverflowXamlIsland`）在弹窗内精确名匹配；
     弹窗是开关式的，已开时不再点 chevron；第二次点击重新查找（弹窗点完即关）。
  2. `open_chat_fast` 连续 ≥3 行 miss 时自动 `revive_via_tray` 后重试一轮（15:02 验收
     的 open #1 就是靠这条路径过的，52s）。
  3. 标定抓到全白侧边栏时，复活重抓一次再判死。
- **待复核**：停绘的根因（usbmmidd 无真实 vsync？WeChat 4.1.12.55 Qt bug？）未定位，
  目前靠复活韧性兜底。`wxbot.py` 主循环里的 `ensure_chat_rendered` 调用模式仍建议排查。

### B3 图像判读可靠性（已补偿，保留记录）
- CDN 图片缓存返回旧图导致误判 → 关键判读改纯像素剖面 + 本地截图留证，不再依赖外部图像服务。
- **15:05 补充**：识别"冻结帧"的方法——连续两次采样像素数完全相等即为 stale frame。

### B4 会话顺序漂移 → 根因已定位并修复
- **根因**：**置顶会话**。侧边栏把置顶会话（如 filehelper）放在最顶，但 DB 按
  `sort_timestamp` 排序，导致 DB 第 N ≠ 侧边栏第 N。filehelper 置顶时占侧边栏
  第 0 行、DB 却排第 1，快路径点 row1 实际点中下面的"已退出群聊"（空白 520px，
  低于 800 渲染阈值被误判 miss）。
- **修复**：`open_chat_fast` 探行扩到 dy∈(0,±1,±2,±3)，并允许**负行上探**
  （y < t+100 截止，不碰搜索框）。
- **连带修复**：标题 OCR 抓到聊天区首条时间戳（'昨天 20:09' 误识为 '距天20:09'），
  导致 verify 假负 + 标定锚点漂移。已将 `title_bbox_pct` y2 0.09→0.078、x1 0.33→0.31，
  且 `_verify_open` 对空 OCR / 时间戳样式结果自动重试 4 次。

## 三、日常使用

```bat
cd E:\vibe_coding_project\ALYOSHKA\bot-auto-reply
.venv/Scripts/python.exe -X utf8 wxapi.py --serve      :: 起服务（自动停靠+标定）
.venv/Scripts/python.exe -X utf8 wxapi_test.py          :: 全量验收 13 项
```

窗口又"空白/点不动"时：点任务栏"^" → 点微信图标（隐藏）→ 再点一次（唤出）。
代码路径已内置同样逻辑（`revive_via_tray`），open/标定遇连续 miss 会自动走。

## 四、改动文件清单（均未 commit，建议验收方确认后提交）

- 新增：`wxapi.py`、`wxapi_test.py`、`docs/WXAPI.md`
- 修改：`wxmini2.py`（park 最小化 bug、发送按钮 crop 偏移、restart 登录确认页、
  **_find_tray_wechat_button 重写 + revive_via_tray 二次点击重找图标**）、
  `wxapi.py`（**负行上探、_verify_open 时间戳重试、title_bbox 收紧、open/标定复活韧性**）、
  `docs/PROJECT.md`、`.gitignore`
- 自动生成（已 gitignore）：`wxapi_config.json`（token）、`wxapi_layout.json`、`wxapi_titles/`
- 另注：仓库里 wxbot.py / wxbot_gateway.py / wxbot_config.example.json 有**并行会话的未提交改动**，
  提交时注意区分（不是本次 wxapi 工作的一部分）。

## 五、15:05 收尾记录

- 13:55 接手时微信已登录但窗口僵死（整窗空白）。WM_CLOSE + 重启新实例免扫码恢复（B1 结论）。
- 排查链路：渲染假死 → 点击迟滞 → 置顶错位 → 标题 OCR 时间戳干扰，四层问题逐一分离。
- 修复后验收：**15:02、15:05 连续两轮 13/13**（含 open 冷/热路径、发文字/图/文件 + DB 断言）。
- 遗留：停绘根因未定位（靠复活兜底）；wxbot.py 主循环 ensure_chat_rendered 调用模式待排查；
  restart_wechat 建议改优雅退出优先。
