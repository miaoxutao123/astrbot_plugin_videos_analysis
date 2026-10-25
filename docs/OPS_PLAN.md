# 下一阶段运维规划 & 抖音创作中心调研（2026-10-24）

> 本文是 [ROADMAP.md](ROADMAP.md)(ROADMAP.md) P1 收尾 → P3 的细化。调研方式：测试容器里复制一份抖音浏览器配置，
> 只读地打开创作中心 9 个页面，记录页面实际调用的接口、返回结构，并从前端 JS 中提取写接口路径。
> **全程没有点击任何按钮、没有调用任何写接口。** 测试账号：18 个作品，登录态有效。

---

## 1. 抖音创作中心：接口地图

### 1.1 读接口（页面打开即调用，均 `status_code=0`）

| 用途 | 接口 | 关键字段 | 备注 |
|------|------|----------|------|
| 账号概况 | `GET /aweme/v1/creator/user/info/` | `user_profile`（昵称、粉丝、关注）、`douyin_user_verify_info.total_favorited`、`permissions` | 不需要 a_bogus，纯 cookie |
| 作品列表 | `GET /janus/douyin/creator/pc/work_list?status=0&count=12&max_cursor=` | `aweme_list[]`（含 statistics、审核/可见性） | 已在用（`list_works`） |
| 作品选择器 | `GET /aweme/v1/creator/item/list/?cursor=` | `item_info_list[]`：`item_id`（**加密 ID**）、`item_id_plain`、`comment_count`、`title` | 评论管理页用；加密 ID 是评论接口的入参 |
| 作品评论（创作者视角） | `GET /aweme/v1/creator/comment/list/?item_id=<加密ID>&sort=TIME&count=10&cursor=0` | `comment_info_list[]`、`has_vcd_filter` | 比 web 端 `comment/list` 更适合"巡检新评论"，支持按时间排序 |
| 新评论通知 | `GET /aweme/v1/creator/notice/comment/` | `comments[]` | 首页"最新评论"卡片 |
| 未读数 | `GET /aweme/v1/creator/user_message/unread_count/` | `count`、`total_count`、`star_count` | 巡检时的廉价"有没有新动静"探针 |
| 系统消息 | `GET /aweme/v1/creator/user_message/list/` | `title`、`content`、`create_time`、`is_read` | 违规/限流/平台通知都在这里，**适合推给管理员** |
| 数据总览 | `GET /aweme/janus/creator/data/overview/all/?last_days_type=1` | `play/digg/comment/fans/new_fans/cancel_fans…` 每项 `current_count`、`last_period_incr` | 一次拿全部核心指标 |
| 指标趋势 | `POST /janus/douyin/creator/data/overview/dashboard`、`GET …/dashboard/fans?recent_days=7` | `metrics[].trends[]` | 按天的趋势，用于周报折线 |
| 作品贡献 Top | `GET /janus/douyin/creator/data/overview/item_contribution_top?dimension=1&recent_days=7` | `items[]` | "这周哪条视频带量" |
| 同类作者诊断 | `POST /dp/douyin/v1/creator/item/author_diagnosis`（body `{}`） | `PlayCnt/Interact/NewFans/PlayFinishRatio/PublishActivation` 的 `OwnValue` vs `SimilarValue` | **很适合给 LLM 做复盘建议** |
| 投稿数据 | `GET /web/api/creator/item/list?fields=visibility,metrics,review&start_time&end_time` | 每条作品的指标、可见性、审核状态 | 近 90 天无投稿时为空 |
| 合集 / 短剧 | `GET /web/api/mix/list/`、`/web/api/playlet/list/` | | |
| 关注列表 | `GET /aweme/v1/creator/relation/following/list/` | | |
| 精选热门 | `GET /aweme/v1/web/douyin/select/hot/item/list/` | `aweme_list[]`（体积 0.8~1.6MB） | 灵感中心；比热搜词更像"可看的视频" |
| 抖音指数热点 | `GET /api/v2/hot/get_current_hot_topic` | `data` 为**加密串** | 暂不接，热搜词接口已够用 |

### 1.2 写接口（从前端 JS 提取，**未调用**）

| 用途 | 接口 | 现状 |
|------|------|------|
| 创作者回复评论 | `/aweme/v1/creator/comment/reply` | 现在回复走 web 端 `comment/publish` |
| 评论操作（置顶/删除/点赞等） | `/aweme/v1/creator/comment/action` | 未实现 |
| 批量回复 / 批量删评 / 评论点赞 | `/aweme/janus/creator/comment/aweme/v1/web/comment/{multi_publish,multi_delete,digg}` | 未实现；**批量回复与"一次审批多条"天然契合** |
| 删除作品 | `/web/api/media/aweme/delete` | 现在是页面点"删除作品"，脆弱 |
| 修改作品（含"设置权限"=可见性） | `/web/api/media/aweme/update_v2/` | 未实现；可做"改为仅自己可见"，与 B站一致 |
| 发布作品 | `/web/api/media/aweme/create_v2/`（上传凭证 `GET /aweme/mid/video/sts2/`） | 现在是页面自动化上传 |
| 私信 | IM：`/aweme/v1/creator/im/user_token/` + `/v1/message/send` | 长连接协议，成本高，放到 P4 |

**关键约束**：前端安全 SDK（`ct.setConfig({scene:"web_protect", certType:"cookie"|"header", signVersion:2})`）
把上述写接口全部列为 `consumerPathList`——请求必须带浏览器生成的设备证书票据（bd-ticket-guard）。
结论：

1. 写接口**只能在创作中心页面上下文里由页面自己的 fetch 发出**（SDK 会挂钩 fetch 自动补票据），
   不能从 aiohttp 直接打。这和现在 `BrowserSigner.fetch_signed_api` 的做法一致，可复用。
2. 这也解释了"新设备写操作必须先过本人验证"：票据绑定设备，新设备首次使用会触发短信 / 原设备扫码。
   **设备验证一次后，同一个浏览器配置目录长期有效**——所以浏览器配置目录必须持久化、不能随意重建。

---

## 1.3 协议化评估（v0.11，实测 2026-10-25）

| 能力 | 现在怎么做 | 结论 |
|------|-----------|------|
| 解析公开视频/图文 | `iesdouyin.com/share/note/{id}` 的 SSR `_ROUTER_DATA`，`playwm→play` + `ratio=1080p` | ✅ 纯 HTTP，约 0.5 秒，不带 cookie；偶发空数据换路径重试，私密/受限作品回退浏览器 |
| 下载视频 | `aweme.snssdk.com/aweme/v1/play/` → douyinvod CDN | ✅ 纯 HTTP，不需要 cookie |
| 创作中心读接口（user/info、overview、diagnosis、消息、work_list、item/list） | aiohttp + cookie | ✅ 纯 HTTP，约 1 秒；失败回退页面内 fetch |
| www.douyin.com 读接口（评论列表、搜索、推荐、详情） | 页面内 fetch，SDK 自动补 a_bogus | ❌ 需要 a_bogus；内置纯 Python 算法 2026-06 起失效，继续用浏览器 |
| 所有写接口（评论、删稿、改资料、发布） | 页面内 fetch / 页面自动化 | ❌ web_protect 设备证书（bd-ticket-guard）绑定浏览器配置目录，只能在浏览器里发 |
| 扫码登录 | 浏览器 | ❌ 登录后还要过新设备验证，留在浏览器 |
| B站全部功能 | aiohttp | ✅ 本来就是纯协议 |

浏览器常驻成本：抖音首页渲染进程约 40%~100% CPU（推荐视频 + Lottie 动画，几乎都是渲染不是 JS）。
v0.11 在空闲时隐藏整页、停动画（`douyin_browser_low_cpu`），实测约 7%~12%，签名与页面内 fetch 不受影响。

---

## 2. 下一步规划（按优先级）

### P1.5 · 抖音创作中心"接口化"（替换脆弱的页面点击）

| # | 事项 | 做法 | 需要用户在场？ |
|---|------|------|:---:|
| 1 | ✅ v0.10 **数据看板（只读）** | `stats()`：`overview/all` + `author_diagnosis` + `item_contribution_top`；B站对应 `member/x/web/index/stat`（已验证可用）+ `x/web/data/pandect` | 否，现在就能做 |
| 2 | ✅ v0.10 **消息巡检（只读）** | `unread_count` → 有变化再拉 `notice/comment` / `user_message/list`；B站 `x/msgfeed/unread` + `x/msgfeed/reply`（已验证可用） | 否 |
| 3 | 评论读取切到创作者接口 | `creator/item/list` 拿加密 ID → `creator/comment/list` | 否（只读） |
| 4 | 删除作品走接口 | 页面内 fetch `/web/api/media/aweme/delete`，失败再回退页面点击 | **是**（要拿一条测试作品验证） |
| 5 | 可见性（仅自己可见） | `update_v2` 改权限；作为独立审批操作 `video_action(action="hide")`，删除失败时同样回退 | **是** |
| 6 | 回复走创作者接口 / 批量回复 | `creator/comment/reply`、`multi_publish`；一次审批多条回复只发一个请求 | **是**（需要一条测试评论） |
| 7 | **设备验证引导** | 检测到"短信验证码 / 原设备扫码"弹窗时：截取原设备二维码推给管理员；或管理员回复「验证码 123456」由插件填入（走审批同款权限：仅管理员、带操作编号） | **是** |

### P3 · 内容运营（数据跑通后）

1. **`/va_report` 日报/周报**：每天巡检时把 1.1 的核心指标追加到 `stats.jsonl`（每平台每账号一行，体积小），
   周报对比上周，交给当前对话模型写 3 条建议（`author_diagnosis` 的同类对比是现成的论据）。
   LLM 工具：给 `video_account` 加 `action="report"`，结果只返回摘要数字，不进长表。
2. **评论巡检 → 起草 → 一次审批**：未读数变化 → 拉新评论 → 按风格起草 → 打包成一个审批（复用现有 `reply` + picks）。
   默认只推送"有新评论"，自动起草需管理员在设置里开启。
3. **系统通知转发**：`user_message/list` 中的违规、限流、作品下架等通知直接推送给管理员会话（高价值、低成本）。
4. **定时发布**：B站 `add/v3` 的 `dtime`；抖音 `create_v2` 的定时字段（待在发布页确认字段名）。
   审批通过后进入队列，到点发布，结果回推。

### P4 · 安全与矩阵（不变，补充两点）

- **浏览器配置目录 = 设备身份**：多账号时每个抖音账号必须有独立、持久的配置目录，否则每次切换都要重新设备验证。
  现在抖音"只能用当前账号"的限制就是因为只有一个目录——多目录是抖音多账号的前提。
- **账号健康分**：审计日志（成功率）+ 风控熔断次数 + 登录剩余有效期 + 系统通知中的违规记录。

---

## 3. 等用户能扫码 / 在场时要做的测试清单

1. 抖音：确认设备验证仍有效 → 用测试作品试 **接口删除** 与 **改为仅自己可见**（建议先发一条测试作品）。
2. 抖音：在测试作品下发一条评论 → 用创作者接口回复 → 批量删除。
3. 抖音：故意触发一次设备验证（如换新配置目录），验证"二维码推送给管理员"的引导流程。
4. B站：定时发布（`dtime`）发一条 2 小时后的测试稿件，确认后改为仅自己可见。

---

## 4. 风险与取舍

- 抖音创作中心改版频繁：读接口按"页面打开时自动调用"的路径拦截（`expect_response`）比直接拼 URL 更抗改版，
  `list_works` 已是这种写法，数据看板也用同样方式。
- 写接口一律页面内发出、频率闸门照旧；接口化的目标是**更稳、更少点击**，不是更快——节流参数不放宽。
- 不接加密的抖音指数、不做私信（IM 协议成本高、风控敏感），直到前面几项稳定。
