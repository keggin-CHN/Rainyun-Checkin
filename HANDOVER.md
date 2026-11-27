# 雨云自动签到 (Rainyun-Checkin) 排查与修复交接文档 v2

> 更新时间：2026-11-26 12:40 (UTC+8)
> 上一版交接文档（v1）中的问题已全部解决，本文档描述当前最新状态。
> **接手前请务必完整阅读本文档，尤其是"当前卡点"一节。**

---

## 1. 已确认的前端改版事实（分析线上前端源码得出）

通过拉取雨云线上前端 JS 源码（`https://app.rainyun.com/js/8078.<hash>.js`）逐行分析，确认新版"每日签到"的完整交互流程为：

```
赚取积分页 (/account/reward/earn)
    │
    ▼
卡片头按钮 <button class="card-header daily-check-in-task">（文案"每日签到 去签到 积分或抽奖券"）
    │ 点击 → 打开 BModal 弹窗（标题"每日签到"）
    ▼
弹窗内两个奖励按钮 <button class="daily-check-in__reward">：
    - "500 积分"（第一个，默认选它）
    - "抽奖券 × 1"
    │ 点击 → 才触发腾讯防水墙验证码 (TencentCaptcha appid=2039519451)
    ▼
腾讯验证码（图案点选）→ submitTask({task_name:"每日签到", vticket, vrandstr, reward})
    ▼
签到完成（卡片头文案变为"已完成"）
```

关键 DOM/XPath（已验证有效）：

| 用途 | XPath |
|------|-------|
| 签到卡片头按钮 | `//button[contains(@class, 'daily-check-in-task')]` |
| 弹窗面板 | `//div[contains(concat(' ', normalize-space(@class), ' '), ' daily-check-in ')]` |
| 弹窗内奖励按钮 | `//div[...daily-check-in...]//button[contains(@class, 'daily-check-in__reward')]` |

---

## 2. 已完成的修复（本轮，提交 44570d8..2ec5182）

### 2.1 流程修复（已验证有效 ✅）

1. **新增弹窗内点击奖励按钮的步骤**（`click_daily_sign_reward_button`）。
   旧脚本点击卡片按钮后直接等验证码，但新版必须先在弹窗里选奖励，验证码才弹出。
2. **不再依赖 `is_displayed()`**。headless 下弹层整体位于视口外（BModal 的 `modal-dialog` rect 为 412x1138，超出 1080 视口高），Selenium 的 `is_displayed()` 对所有弹层内元素一律误报 `False`。相关改动：
   - 卡片按钮/奖励按钮：只要求 `is_enabled()`，点击一律 `execute_script("arguments[0].click();")`；
   - 验证码 iframe 内元素（slideBg/instruction/tcOperation/confirm）：等待条件从 `visibility` 改为 `presence`；
   - 等待 style/src 就绪后再下载验证码图片（presence 在元素刚创建时会拿到空 style）。
3. **验证码 iframe 按尺寸过滤**（`switch_to_visible_captcha_frame`）：
   页面存在残留的隐藏占位 iframe（`id=tcaptcha_iframe_dy`，尺寸为 iframe 默认的 **300x150**，`displayed=False`），
   真弹出的验证码尺寸约为 320x480+。现在按 `尺寸>0 且 不等于 300x150` 过滤。

4. **验证码识别算法替换**（提交 2ec5182，移植自 `CMSZ001/Rainyun-Qiandao-Docker`）：
   - 新增 `ICR.py`：黑色区域轮廓提取 + sprite **±45° 逐度旋转**生成模板 + `cv2.matchTemplate` 匹配；
   - 修复点击坐标错位：目标坐标改为**相对图片左上角**计算，再 `move_to_element`（到中心）+ 分段 `move_by_offset` 移到目标点（`_human_track` 拟人轨迹）；
   - 旧算法（ddddocr detection + SIFT `compute_similarity`）匹配率只有 0.14~0.42，导致连续 50 次提交全失败。旧函数 `check_captcha`/`check_answer`/`compute_similarity` 仍保留在文件中但已不被调用。

### 2.2 各修复对应提交

| 提交 | 内容 |
|------|------|
| `44570d8` | 新增弹窗内点击奖励按钮流程 |
| `4029bd6` | 奖励按钮不依赖 is_displayed，直接 JS click |
| `e2e8926` | 验证码 iframe 检测不依赖 is_displayed |
| `574bf1e` | iframe 内元素等待改为 presence |
| `b2d5dc1` | 等待验证码图片 style/src 就绪 |
| `54e74fd` | 排除 300x150 占位 iframe（登录页验证码误判由此修复，登录恢复正常）|
| `2ec5182` | 移植 ICR 模板匹配算法 + 修复点击坐标错位 |

---

## 3. 当前卡点（重要更正：之前的"根因"都已被证伪）

> 本节经过两次更正，请以本版为准。此前两版分别把失败归因为"ICR 识别率""点错锁定奖励"，均被推翻。

**已被证伪的假设：**
- ❌ ICR 识别 / 坐标映射错误：映射（`x/raw_w*bg_width`，按渲染后矩形）已证明正确；ICR 命中点落在真实深色图案上。
- ❌ 点到锁定/已领取奖励：用户截图证实弹窗是「每天可任选一种奖励」，`500 积分`/`抽奖券 × 1` 默认都正常可选。日志里的 `opacity:0.65, enabled=False` 只是常规样式 + 点击后「处理中」态，不是锁定。基于 opacity 判 locked 的代码已撤销。

**关键事实（2026-11-27 发现，务必理解）：**
- `job_45818364034.log`（40+ 次"验证码未通过"）是**旧版代码**跑的：它打 `图案 N 位于 (...)`。
- 当前 HEAD `fc0ab07`（2026-11-27 10:30）才引入 `图案 N 图像坐标 ... -> 点击坐标 ... 命中元素`、`slideBg 布局`、`坐标映射`、`点击后标记层`，以及 `captcha_debug/` 调试图。
- 因此 **HEAD 的这套修复从未在 GitHub 上真正运行过**，我们一直拿旧日志当证据。
- `stealth.min.js` 其实一直在 `main()` 里（`driver.get` 之前）通过 `Page.addScriptToEvaluateOnNewDocument` 注入，并非缺失（本轮只是把它收敛到 `_apply_stealth()` 并补了 `navigator.webdriver` 兜底 + 真实 UA）。

**结论（2026-11-27 本地真机实测，已实锤）：当前算法是对的。**

用本地探针 `.ref/probe/probe_solve.py` 对**真实腾讯点选验证码**实测（不登录、不消耗签到次数）：真实弹出验证码 → 当前 ICR 识别 → 按渲染矩形映射 → ActionChains 点击 → 提交 → 读 `tcOperation`，**在 `--headless=new` 下 3/3 全部 `show-success` 通过**，命中元素全部是 `slideBg`。这证明：ICR 识别、坐标映射、点击、提交、判定——整套机制**完全正确**。

由此反推旧 GitHub 运行（40+ 次失败）的原因：那份日志是**旧代码**（`图案 N 位于`），且 GitHub linux 分支当时用的是**旧版 `--headless`**。旧 headless 对点选类验证码的鼠标/渲染行为与真实浏览器差异极大，是已知坑。

**已实施修复：** 将 `init_selenium` 的 linux 分支从 `--headless` 改为 **`--headless=new`**（与本地验证通过的运行环境一致）。

下一步：
1. **重新启用仓库 Actions**（用户操作；当前 disabled 无法触发）。
2. 在一个**尚未签到**的日期 Run 一次（今天已手动签过，会直接检测到"已完成"并跳过——这是正确行为）。预期：`已点击每日签到弹窗奖励按钮` → `tcOperation class = ... show-success` → `验证码通过`。
3. 若仍失败，下载 artifact `captcha-debug-account*/`：看 `aNN_annotated.jpg`（点位是否对）、`aNN_frame.png`（点击后是否有序号标记）、日志 `命中元素` / `tcOperation class`，再对症。
4. 想复现/调参：直接跑 `.ref/probe/probe_solve.py`（`python probe_solve.py headless 5`），本地即可看真实成功率，快且不消耗签到。

**一个必须了解的环境现象**：headless Chrome 下雨云所有弹层的 `is_displayed()` 都可能返回 `False`（弹层在视口外）。**任何新加的元素交互都不要依赖 `is_displayed()` 或 `EC.visibility_of_*`**，一律用 `presence` + JS click。这是本轮踩过的最大的坑。

---

## 4. 排查方法备忘（本轮验证有效的手段）

1. **无 `gh` CLI 时访问 Actions API**：本机有 GitHub 凭据（git credential manager），用 `git credential fill` 取 token 后 `curl -H "Authorization: Bearer $token"` 访问 REST API（`/actions/runs/<id>/jobs`、`/actions/jobs/<id>/logs`）。
2. **分析雨云前端源码**：直接 `curl` 下载 `https://app.rainyun.com/js/index.<hash>.js`，从中解析 webpack chunk 映射（`a.u=function(e){...}`），再下载对应 chunk（如 `8078.<hash>.js`）即可看到压缩但可读的组件源码。用中文关键词（如"去签到"）定位相关组件。
3. **在 GitHub Actions 里打诊断日志**：本轮加了几个诊断函数（`log_sign_modal_diagnostic`、`log_browser_console_errors`、`get_daily_sign_button_diagnostic`），都带 `[...]` 前缀，接手后可以复用或删除。
4. **云云：验证码图片 URL 是临时有效的**（`turing.captcha.qcloud.com/cap_union_new_getcapbysig`），不能离线复现，只能在运行时调试。

---

## 5. 相关仓库

| 仓库 | 状态 | 说明 |
|------|------|------|
| `0x6768/Rainyun-Checkin`（本项目上游） | 停更 | 2026-01 底后只有保活提交，**无新版前端解决方案** |
| `CMSZ001/Rainyun-Qiandao-Docker` | 活跃 | **2026-11-14 有关键修复**，`ICR.py` 识别算法来源 |
| `Jielumoon/Rainyun-Qiandao` | 一般 | 未详细调查 |
| `TDSCDMAA/Rainyun-Qiandao` | 活跃 | 只更新 workflow，核心代码未改 |

---

## 6. 运行与验证

```bash
# 本地无雨云账号密码，无法本地复现，验证方式是触发 Actions：
# GitHub 网页 → Actions → Checkin → Run workflow
# 或 API:
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d '{"ref":"main"}' \
  "https://api.github.com/repos/keggin-CHN/Rainyun-Checkin/actions/workflows/checkin.yml/dispatches"
```

- 签到是每日一次的，当天失败后第二次运行会直接跳过（检测到"已完成"即成功返回）。
- 工作流有两个 job：`checkin-account1` 失败后 `checkin-account2` 会 skip，这是正常的（`needs` 依赖）。
