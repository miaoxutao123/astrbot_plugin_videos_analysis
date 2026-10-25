# AstrBot 视频解析与 AI 理解插件

<div align="center">

![访问次数](https://count.getloli.com/@astrbot_plugin_videos_analysis?name=astrbot_plugin_videos_analysis&theme=3d-num&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

</div>

一个功能强大的 AstrBot 插件，支持多平台视频内容解析、下载和 AI 智能分析。Agent 可主动调用视频解析与理解工具。

## ✨ 主要功能

### 📱 多平台视频解析（被动解析，默认关闭）
- **抖音（Douyin）**：图片、视频、多段视频无水印解析下载
- **哔哩哔哩（Bilibili）**：视频解析、高清下载（支持登录获取高画质）
- **MC百科（MCMod）**：模组和整合包信息解析

> 被动解析需在配置中开启 `auto_parse_enabled`，群聊中发送链接即自动触发。

### 🤖 AI 视频理解
- **多模型支持**：
  - **Gemini**：智能视频内容分析（推荐，REST 直连，兼容官方与 new-api/one-api 中转站）
  - **MiMo**：小米多模态模型（支持官方API / OpenRouter）
  - **本地 ASR**：FunASR 语音识别 + 智能抽帧（离线方案）
- **智能分析策略**：
  - 小视频（≤`gemini_inline_limit_mb`）：base64 内联发送整段视频
  - 更大的视频：尝试 Google File API；不可用或超过 `gemini_direct_max_mb` 时回退为音频转录 + 关键帧提取
  - `understand_video` 按 `video_understand_method` 首选，失败自动按 gemini → mimo → local_asr 回退
  - 图片内容：多图片批量 AI 分析
- **个性化回应**：结合人格设定提供自然回复

### 🔧 Agent 主动调用工具

插件注册了三个 LLM Tool，供 Agent 主动调用：

| 工具名 | 功能 | 支持输入 |
|--------|------|----------|
| `parse_video_link` | 解析视频链接，返回标题、封面、直链等信息 | 抖音链接、B站链接 |
| `understand_video` | 深度理解视频内容 | 抖音/B站链接、HTTP直链、本地路径 |
| `creator_upload_video` | 准备把视频发布到管理员账号（只生成确认码，管理员 `/va_confirm` 后才发布；非管理员调用直接拒绝） | 视频直链、data 目录下的本地视频 |

> Agent 可以先用 `parse_video_link` 获取视频直链，再用 `understand_video` 理解直链视频内容。

### 🔑 登录管理
- **抖音**：QR码扫码登录（Playwright）/ 手动粘贴 Cookie
- **B站**：QR码扫码登录
- Cookie 自动持久化到 `data/plugin_data/astrbot_plugin_videos_analysis/accounts/`（插件更新不会丢失，旧版本数据首次启动自动迁移）
- `/va_accounts` 一览各平台登录态与 AI 服务连通性

### 🛠️ 账号运维
- **定时巡检**：后台每 `health_check_interval_minutes` 分钟深度检查抖音/B站登录态（含创作者中心与登录过期时间）和 Gemini/MiMo 连通性；
  只在状态变化时（正常 → 异常 / 即将过期 / 恢复）推送到 `/va_notify` 设置的会话，不会重复刷屏
- **多账号池 + 自动轮换**：`/va_pool save` 把当前登录保存为命名账号；抖音空响应/风控、B站 -352/-412/cookie 失效
  连续 `rotate_fail_threshold` 次后自动切到池里下一个可用账号，换下来的账号冷却 `rotate_cooldown_minutes` 分钟，并通知管理员
- **WebUI 状态页**：AstrBot WebUI → 插件页面 → 账号运维：平台状态、上次巡检结果、账号池（可切换）、待确认操作（可取消）。
  接口需 WebUI 登录，返回内容不含 cookie / key

### 📺 B站账号运维
- **登录与续期**：`/bili_login` 扫码后保存 cookie、`refresh_token` 并补齐 buvid3/4；定时巡检时按 B站提示自动续期并写回账号池，
  `/bili_refresh [force]` 可手动续期；`/va_accounts` 显示过期时间和"自动续期: 可用/不可用"
- **浏览**：`/bili_feed [hot]` 首页推荐（登录后按账号口味）/热门，`/bili_search <关键词>`，
  `/bili_watch <序号|BV|链接>` 下载 360P → **抽帧 + 语音转写** → 关键帧图片和转写一起交给**当前对话模型**（如新版 DeepSeek，可看图）总结，
  附 3 条热评，并（可选）写入观看历史；模型不支持图片时改用 Gemini 看图，再不行只用转写
  - 转写按机器压力从轻到重：B站字幕（零算力，会校验字幕确实属于本视频）→ 线上 ASR（`browse_asr_*`，如 SiliconFlow 免费 SenseVoiceSmall）
    → AstrBot 框架 STT → 本地 funasr
- **评论**：`/va_works bili` 我的稿件，`/va_reply bili <序号>` 精选回复，`/va_comment <序号|BV> [内容]` 对任意视频发评论，
  不写内容时结合浏览结果由 AI 起草；全部走预览 + `/va_confirm`
- **资料**：`/bili_profile` 查看头像/签名/等级/粉丝，`/bili_sign <新签名>`、`/bili_avatar`（附带或引用图片，自动裁成方形）均需确认
- **多账号**：`/va_pool save bilibili <名称>` 存多个号；命令加 `-a <名称>`、LLM 工具传 `account` 即可指定账号，
  待确认操作会记住发起时的账号，确认时仍用那个号执行；定时巡检对池里每个号分别续期
- **抗风控**：所有请求串行并随机间隔；自动补 buvid3/4 与 bili_ticket；写操作前随机停顿；按账号计的频率闸门
  （回复 30 次/天、发评论 10 次/天、改资料 5 次/天）；同一账号 24 小时内不发重复内容；内容带链接直接拦截；
  写操作遇到 -352/-412/验证码/重复评论等风控码时，该账号写操作熔断 `risk_cooldown_minutes`（默认 60 分钟）并推送告警

### 🎵 抖音账号运维
- **浏览**：推荐流、热搜词不需要登录；搜索需要 `/dy_login`。对机器人说"刷一下抖音推荐""抖音热榜有什么""在抖音搜猫咪"即可，
  列表同样带序号，"看第 3 个"会跟随最近一次列表的平台
- **评论**：`/va_comment douyin <序号|链接|ID> [内容]` 或让 AI 发评论，走审批；按账号计频（10 次/天，间隔 ≥60 秒）
- **资料**：`video_action` 的 `sign` / `avatar` 传 `platform=douyin`（或对 AI 说"把我抖音签名改成…"），在已登录的浏览器里打开"编辑资料"
  弹窗、逐字输入后保存（5 次/天，间隔 ≥5 分钟——抖音在上次修改后约 2 分钟内会忽略保存）
- **本人验证**：抖音把机器人浏览器当作新设备，**首次写操作（评论、改资料）会要求短信验证码或"原设备扫码"**。
  插件检测到后会点取消退出（不会触发发短信），返回提示并暂停该账号写操作；管理员用抖音 App 扫一次"原设备扫码"后即可正常使用
- 写接口返回空响应 / 验证码时，该账号写操作熔断 `risk_cooldown_minutes` 分钟并推送告警

### 🧾 操作审计
每个待审批操作从申请到结束都写入 `data/plugin_data/astrbot_plugin_videos_analysis/audit.jsonl`：申请人、平台/账号、操作摘要、
批准/拒绝人、执行结果（成功 / 失败 / 部分成功）和失败原因，过期、无人可审批被取消也有记录。
`/va_audit [条数] [平台] [申请码]`（别名「操作记录」）查看；AI 可用 `video_account` 的 `audit` 查询；WebUI 接口 `GET .../audit`。

### 📊 运营数据与消息巡检（抖音 / B站，只读）
- **数据看板** `/va_report [平台] [天数]`（别名「运营数据」）：粉丝、播放、获赞等累计值，与 N 天前的对比，平台自带的近 7 天指标；
  抖音还会给出**同类作者对比**（播放、互动率、完播率、新增粉丝、发布数）。插件每天自动记录一次数据，第二天起可看变化。
- **消息概况** `/va_inbox [平台]`（别名「消息概况」）：未读数、最新评论 / 回复 / @我、系统通知（带日期）。
- **消息巡检**：每 `inbox_patrol_minutes` 分钟（默认 30）检查一次，**只把新出现的**评论、@ 和系统通知推送给 `/va_notify` 设置的会话；
  第一次巡检只记录现有消息、不推送，避免刷屏。
- AI 可直接问：“看看我这周的运营数据，给点建议”“我账号有没有新评论”（`video_account` 的 `report` / `inbox`）。
- 抖音数据在创作中心页面里读取（与页面自己的请求同源），B站走创作中心 / 消息中心接口，全部只读，不需要审批。

### 🧰 调试管理面板（WebUI 内嵌，默认关闭）
调试时在插件配置里打开 `debug_webui_enabled`（保存后插件自动重载），即可在 AstrBot WebUI 左侧「视频插件管理面板」里：
- **概览**：平台状态、账号池切换、立即巡检、配置体检（还缺什么一目了然）
- **审批**：批准 / 拒绝待审批操作（等同管理员本人确认，审计记录处理人 `WebUI:<用户名>`）
- **审计**：申请人、处理人、结果、失败原因
- **数据**：运营数据报表、消息概况、立即巡检消息
- **设置**：可视化修改插件设置，密钥打码显示、留空不修改，保存后自动重载
- **登录**：B站 / 抖音扫码登录（二维码直接显示在页面里）
- **调试**：解析链接、搜索、热门、看视频（显示转写来源、挑帧方式与时间、转写节选、总结模型与耗时）；缓存占用与一键清理

关闭时只保留一个状态接口，其余管理接口不会注册；页面会提示如何开启。开源发布与日常使用建议保持关闭。

### 🤖 全部功能支持 LLM 调用（危险操作需管理员审批）
面向小白设计：直接对机器人说"搜一下罗翔的视频""看看这个讲了什么""把这个视频发我""插件还缺什么配置"即可。

| 工具 | 权限 | action |
|------|------|--------|
| `video_browse` | 所有人 | `search` 搜索 · `recommend` 推荐 · `hot` 热门（抖音为热搜词）· `watch` 看视频（抽帧+转写总结）· `info` 链接信息；B站 / 抖音 |
| `video_download` | 所有人（30 秒限频） | 下载视频/抖音图文并发到当前聊天 |
| `video_account` | 仅管理员 | `status` 账号状态 · `works` 我的作品 · `comments` 评论 · `profile` 资料 · `audit` 操作记录 |
| `video_action` | 只能**提交申请** | `comment` · `reply` · `sign` · `avatar` · `delete` · `switch_account` · `upload` |
| `video_plugin_settings` | 仅管理员 | `show` 查看 · `check` 体检 · `set` 修改（需确认） |

5 个工具的定义合计约 1000 tokens（原 16 个约 1970），描述为静态文本，不影响模型的提示词缓存；详见 [docs/ROADMAP.md](docs/ROADMAP.md#6-上下文与缓存评估结论v08-实测)。

**提权审批**：
- 管理员自己让 AI 操作 → 预览发回当前会话，管理员本人回复「确认 1234」（或 `/va_confirm 1234`）才执行，「拒绝 1234」取消
- 普通用户让 AI 操作 → 申请推送到管理员审批会话（管理员私聊发 `/va_notify` 设置），申请人只收到"已提交审批"，结果回告申请人
- 没有任何工具能代为确认；每人最多 3 个待审批；`llm_allow_non_admin_requests=false` 可禁止普通用户发起申请

**看视频**：先转写（B站字幕 → 线上 ASR → 框架 STT → 本地 funasr），再由对话模型读带时间轴的转写**挑出需要看画面的时刻**抽帧；
没有转写时按时长均匀抽帧。关键帧 + 转写交给当前对话模型（如可看图的 DeepSeek）总结。超过 `browse_max_minutes` 的视频只看开头，
超过 `browse_sync_max_minutes` 的转后台看完再推送。Gemini / MiMo **默认关闭**（`gemini_enabled` / `mimo_enabled`）。

### 🎬 创作者操作（抖音 / B站，平台可扩展）
> 发布、删除、回复评论都是**危险操作**：命令只生成预览和 4 位确认码，管理员发送 `/va_confirm <码>` 后才执行（默认 60 秒内有效）。
> 操作作用于账号池里**当前使用中**的账号，预览里会写明账号名。

- **发布视频**：`/va_upload 标题 #话题` 时附带视频、引用一条视频消息，或在 2 分钟内补发视频；也可让 Agent 通过 `video_action upload` 发起
  - **B站投稿**：`/va_upload bilibili 标题 #话题 [tid=分区]`，preupload → upos 分片上传（失败自动重试 3 次）→ 上传封面 → `add/v3` 提交；
    同一条消息里带一张图片作为封面，不带就取视频第 1 秒；至少 1 个话题；分区默认 `bili_upload_tid`（21=日常）；按账号 5 次/天、间隔 ≥10 分钟
- **删除作品**：`/va_works` 列出最近作品，`/va_delete <序号|链接|ID>`。B站 Web 端删稿要求短信验证码，插件会自动改为「仅自己可见」，要彻底删除再到 App / 创作中心操作
- **评论精选回复**：`/va_reply [序号|链接|ID]`（默认最新作品）读取 48 小时内的一级评论，按点赞取前 10 条、随机挑 1~5 条，
  结合视频内容（复用 `understand_video` 的分析流程，按作品缓存）和 `creator_reply_style` 账号风格由 LLM 起草回复；
  可 `/va_edit <码> <序号> <新回复>` 修改、`/va_confirm <码> 1 3` 只发部分。作者本人、已回复过的评论自动跳过，多条之间随机间隔 4~9 秒
- 浏览器自动化失败时会把创作者中心截图发回，方便判断是未登录、验证码还是页面改版
- 新平台只需在 `services/creator/` 实现 `CreatorPlatform`（`list_works / upload / delete / list_comments / reply_comment` 等）并登记到 `REGISTRY`，
  命令里写 `/va_works bilibili` 这样的平台参数即可复用全部流程

## 🚀 安装使用

### 安装方式
在 AstrBot 插件市场搜索 `astrbot_plugin_videos_analysis` 安装。

### 依赖要求
- **Python 3.10+**
- **FFmpeg**：用于音视频分离、关键帧提取
- Python 依赖详见 [`requirements.txt`](requirements.txt)
- **抖音浏览器（可选）**：公开视频解析、创作中心数据、消息巡检都走 HTTP，不需要浏览器；
  搜索、评论、发布、扫码登录需要 Chromium + 显示环境（抖音已拦截纯 Python 签名与无头浏览器）。
  插件发现装了 playwright 但没下载 Chromium 时会**自动下载**；系统依赖和 Xvfb 需要自己装：
  ```bash
  playwright install --with-deps chromium
  # Linux 无桌面环境（含 Docker）还需要 Xvfb，插件会自动启动 :99
  apt install -y xvfb
  ```
  官方 Docker 镜像（`soulter/astrbot`）默认不含以上组件，需在容器内安装，或基于官方镜像构建：
  ```dockerfile
  FROM soulter/astrbot:latest
  RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
      && pip install playwright && playwright install --with-deps chromium \
      && rm -rf /var/lib/apt/lists/*
  ```
  装好后发送 `/dy_restart` 即可，无需重启 AstrBot。

### 快速开始
1. 安装插件（无需重启 AstrBot；浏览器在后台启动，插件加载约 1 秒）
2. 私聊机器人发送 **`/va_setup`**（别名「插件体检」），按清单逐项处理：每项都写了下一步怎么做
3. 常用设置直接说中文即可，例如“用硅基流动的免费语音转写，密钥是 sk-xxx”“打开自动解析链接”“看视频抽 6 帧”；
   修改需要管理员确认，**密钥只能私聊设置**
4. 要用账号功能：私聊发送 `/bili_login`、`/dy_login` 扫码；再私聊发送 `/va_notify` 接收审批和告警

## ⚙️ 配置选项

### 基础配置
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `auto_parse_enabled` | 被动解析开关 | `false` |
| `nap_server_address` | NAP 服务器地址 | `localhost` |
| `nap_server_port` | NAP 服务器端口 | `3658` |
| `delete_time` | 文件清理时间（分钟） | `60` |
| `max_video_size` | 视频大小限制（MB） | `100` |

### AI 视频理解
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `gemini_api_key` | Gemini API 密钥 | `""` |
| `gemini_base_url` | Gemini 地址，中转站填根地址（不带 `/v1beta`） | `""`（官方） |
| `gemini_model` | 模型名，中转站请填其实际提供的模型 | `gemini-2.5-flash` |
| `gemini_inline_limit_mb` | 内联发送阈值 | `20` |
| `gemini_direct_max_mb` | 整段视频分析上限，超过走音频+关键帧 | `200` |
| `video_understand_method` | 首选理解方式 `gemini`/`mimo`/`local_asr` | `gemini` |
| `mimo_api_key` | MiMo API 密钥 | `""` |
| `mimo_api_base` | MiMo API 地址 | `https://api.xiaomimimo.com/v1` |
| `mimo_model` | MiMo 模型 | `mimo-v2-omni` |
| `url_video_comprehend` | 链接视频自动理解 | `false` |
| `upload_video_comprehend` | 用户视频注入上下文 | `false` |
| `private_auto_comprehend` | 私聊自动理解 | `true` |

### 哔哩哔哩
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `bili_quality` | 视频清晰度 | `64`（720P） |
| `bili_reply_mode` | 回复模式 | `3`（图片+视频） |
| `bili_url_mode` | 显示直链 | `false` |
| `bili_use_login` | 使用登录状态 | `false` |
| `Merge_and_forward` | 合并转发 | `true` |

### 抖音
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `douyin_video_comprehend` | 深度理解功能 | `false` |
| `show_progress_messages` | 显示进度提示 | `true` |
| `douyin_proxy` | 代理地址 | `""` |
| `douyin_browser_signer` | 启用抖音浏览器（搜索/评论/发布/扫码登录需要；解析公开视频不需要） | `true` |
| `douyin_browser_low_cpu` | 浏览器空闲省电：空闲时隐藏页面、停动画，CPU 约 50% → 10% | `true` |

### 账号运维
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `health_check_interval_minutes` | 定时巡检间隔，0 关闭 | `360` |
| `inbox_patrol_minutes` | 消息巡检间隔（新评论/@/系统通知推送 + 每日数据采样），0 关闭 | `30` |
| `rotate_fail_threshold` | 连续失败几次后轮换账号 | `2` |
| `rotate_cooldown_minutes` | 换下来的账号冷却时间 | `30` |

### 创作者操作
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `confirm_timeout_seconds` | 危险操作确认有效期（评论回复至少 300 秒） | `60` |
| `reply_max_comments` | 每次精选回复最多几条（1~5） | `5` |
| `reply_window_hours` | 只看最近多少小时的评论 | `48` |
| `creator_reply_style` | 账号回复风格（给 LLM 的人设/语气） | `""` |
| `creator_reply_provider_id` | 起草回复用的模型提供商，留空用会话默认 | `""` |
| `creator_reply_video_understanding` | 起草前先理解视频内容 | `true` |
| `bili_upload_tid` | B站投稿默认分区 ID（21=日常 17=单机游戏 138=搞笑 201=科学科普 95=数码） | `21` |

## 🎯 使用示例

### 被动解析（需开启 auto_parse_enabled）
```
用户：https://v.douyin.com/xxxxxx/
Bot：正在解析抖音链接...
Bot：[发送无水印视频]

用户：https://www.bilibili.com/video/BVxxxxxxx
Bot：📜 视频标题：xxx  👀 12345  👍 567 ...
Bot：[发送封面+视频]
```

### Agent 工具调用
```
用户：帮我解析一下这个视频 https://b23.tv/xxx
Agent → parse_video_link("https://b23.tv/xxx")
Bot：这个B站视频标题是"xxx"，时长10分30秒，播放量12345次...

用户：分析一下这个视频讲了什么
Agent → understand_video("https://v.douyin.com/xxx")
Bot：这个视频展示了...（AI详细分析）
```

### 管理员命令（仅 AstrBot 管理员可用）
```
/va_accounts [deep]  → 查看各平台账号与 AI 服务状态（别名 /账号状态；deep 实测连通性）
/va_logout <平台>     → 注销 douyin / bilibili 登录态
/va_setup            → 插件体检：环境、账号、模型、转写、告警会话逐项检查并给出下一步（别名「插件体检」「快速配置」）
/bili_login          → B站扫码登录（仅私聊）
/dy_login            → 抖音扫码登录（仅私聊）
/dy_cookie xxx       → 手动设置抖音cookie（仅私聊）
/dy_restart          → 重启抖音浏览器签名引擎

# 账号运维
/va_check            → 立即深度巡检（不推送，直接回复结果）
/va_notify [off]     → 把当前会话设为（或取消）账号告警接收方
/va_pool             → 查看账号池
/va_pool save <douyin|bilibili> <名称>   → 保存当前登录为命名账号
/va_pool use|del <平台> <名称>            → 切换 / 删除
                       添加第二个号：/va_logout douyin → /dy_login 扫新号 → /va_pool save douyin 小号

# B站
/bili_refresh [force]               → cookie 续期（平时巡检自动处理）
/bili_feed [hot] · /bili_search <词> → 推荐/热门/搜索（带序号）
/bili_watch <序号|BV|链接>           → 浏览：抽帧 + 语音转写总结，记入观看历史
/bili_profile                       → 账号资料
/bili_sign <新签名> · /bili_avatar   → 改签名 / 换头像（需确认）
/va_comment [平台] <序号|链接|BV> [内容] → 发评论（不写内容由 AI 起草，需确认）

# 创作者操作（[平台] 可省略，默认 douyin；B站链接/BV 号会自动识别为 bilibili）
/va_works [平台]                    → 最近作品（带序号）
/va_upload [平台] <标题> #话题 [tid=分区] → 发布（附带/引用视频，或随后补发；B站可带一张图当封面）
/va_delete [平台] <序号|链接|ID>     → 删除作品
/va_reply [平台] [序号|链接|ID] [@抖音号] → 评论精选回复（LLM 起草；带 @ 时只回复这个人）
/va_confirm <码> [序号...]          → 执行待确认操作（评论回复可只发部分）
/va_edit <码> <序号> <新回复>        → 修改待发送的回复
/va_cancel <码> · /va_pending       → 取消 / 查看待确认操作
/va_audit [条数] [平台] [申请码]     → 操作审计（申请人、批准人、结果、失败原因）
/va_report [平台] [天数]            → 运营数据（累计值、N 天变化、同类作者对比）
/va_inbox [平台]                    → 消息概况（未读、最新评论/@、系统通知）
```

评论精选回复示例：
```
管理员：/va_reply 1
Bot：⚠️ 待确认 [6265] douyin·主号
     回复作品 7644535719297355023 的 2 条评论
     [1] 青蛙🐸爱好者😃（👍46，3 小时前）: 啥时候更新朋友收集梦想生活？
         ↳ 安排上了！尽快更新[偷笑]
     [2] 格了个蕾丝（👍22，5 小时前）: 这到底啥游戏啊？
         ↳ 叫《GasNCar》哈，VR游戏，置顶评论里有写～
     /va_confirm 6265 全部发送 · /va_confirm 6265 1 3 只发部分 ...
管理员：/va_confirm 6265 2
```

### 通用命令
```
/理解视频 URL   → 主动触发视频AI理解（支持抖音 / B站 BV、av、b23 短链）
```

> Agent 调用 `understand_video` 时，下载 + 模型分析可能超过 AstrBot 默认的 120 秒工具超时，
> 建议在 AstrBot 配置中把 `tool_call_timeout` 调到 300。

## 📁 项目结构

```
astrbot_plugin_videos_analysis/
├── main.py              # 入口（配置 + @filter 代理）
├── handlers/            # 消息处理器
│   ├── douyin_handler   # 抖音被动解析
│   ├── bilibili_handler # B站被动解析
│   ├── mcmod_handler    # MC百科解析
│   ├── video_handler    # 视频分析 + 直链注入
│   ├── admin_handler    # 管理命令
│   ├── account_ops_handler # 账号运维 / 创作者操作命令（只做解析与渲染）
│   └── web_api          # WebUI 插件页接口
├── pages/accounts/      # WebUI 插件页「账号运维」
├── tools/               # LLM Agent 工具
│   ├── parse_tool       # parse_video_link
│   ├── understand_tool  # understand_video
│   └── creator_tool     # creator_upload_video
├── services/            # 平台服务层
│   ├── bilibili_service # B站解析下载
│   ├── douyin_service   # 抖音下载
│   ├── mcmod_service    # MC百科
│   ├── gemini_service   # Gemini REST 客户端
│   ├── account_manager  # 账号状态 / 注销 / 账号池轮换 / 定时巡检
│   ├── creator_ops      # 二次确认、评论挑选、LLM 起草（平台无关）
│   ├── audit_log        # 操作审计日志（audit.jsonl）
│   ├── creator/         # 创作者平台实现：base.CreatorPlatform + douyin + bilibili
│   ├── mimo_service     # MiMo 多模态
│   └── video_analysis   # 本地 ASR + 抽帧
├── utils/               # 工具函数
│   ├── file_utils       # 文件发送 + 清理
│   ├── media_utils      # FFmpeg 封装
│   ├── config_helper    # 配置辅助
│   └── paths            # 持久化数据路径 + 旧数据迁移
├── douyin_scraper/      # 抖音爬虫
└── douyin_login.py      # 抖音登录
```

## 🙏 特别鸣谢

**[Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)**
- 提供了完整的抖音视频解析方案
- 感谢 [@Evil0ctal](https://github.com/Evil0ctal) 及所有贡献者

## 🔄 更新日志

### v0.11.0（当前版本）
- ⚡ 抖音解析改为纯 HTTP：先读公开分享页（约 0.5 秒，无水印 1080p），私密/受限作品再用浏览器签名；
  支持 `douyin.com/video|note`、`modal_id` 等长链接
- ⚡ 创作中心数据、消息、作品列表改为直接 HTTP 读取（10~20 秒 → 约 1 秒），接口异常时自动回退页面读取
- ⚡ 浏览器空闲省电（CPU 约 50% → 10%）；浏览器后台启动，插件加载约 1 秒；缺 Chromium 时自动下载
- 🧰 `/va_setup` 插件体检（也在 WebUI「数据」页）；对 AI 说“插件还缺什么”给出同样的清单
- 🔒 安全加固：扫码登录、手动 cookie、密钥类设置只允许私聊；抖音登录 cookie 只发给抖音主站（不再发给 CDN 与跳转地址）；
  发给聊天、模型、面板的报错统一脱敏；修改转写/Gemini 地址或抖音代理时审批消息提示“密钥/cookie 会发往新地址”

### v0.10.0
- 📊 运营数据看板：`/va_report`、`video_account report`、WebUI「数据」页；每日自动采样，按天数对比；抖音同类作者对比
- 📬 消息巡检：抖音创作中心 + B站消息中心的新评论 / @ / 系统通知，定时只推送新增；`/va_inbox`、`video_account inbox`
- 🛡️ B站删稿遇到短信验证码时自动改为仅自己可见（v0.9.2）；WebUI 内嵌调试管理面板（v0.9.1，默认关闭）

### v0.9.0
- 🎵 抖音浏览：推荐流 / 热搜词 / 搜索（登录后）接入 `video_browse`，平台可由参数、关键词前缀或用户原话自动识别；按序号观看跟随最近列表的平台
- 💬 抖音发评论、改签名、换头像（浏览器内"编辑资料"）；识别"短信验证 / 原设备扫码"本人验证并安全退出
- 📤 B站投稿：分片上传 + 封面（自定义或自动截帧）+ 分区 + 标签；`publish_request` 支持 `cover / tid / desc`
- 🧾 操作审计：`/va_audit`、`video_account audit`、WebUI 接口；记录申请人、批准人、结果与失败原因
- 🛡️ 抖音写操作改为按账号计频，并接入账号级风控熔断

### v0.8.0
- 🧭 面向小白：自然语言配置（`video_plugin_settings`：查看 / 体检 / 修改需确认，改完自动重载）；审批可直接回复「确认 1234」
- 🎞️ 看视频：按转写挑帧（对话模型判断哪些时刻要看画面），无转写按时长切帧；临时文件独立目录、用完即删；长视频截取开头、超时转后台
- 📥 下载发送：`/va_download`（别名「下载视频」）与 `video_download` 工具，B站/抖音
- 🔕 Gemini / MiMo 默认关闭，默认链路只用当前对话模型
- 🧩 工具合并为 5 个、结果瘦身（schema 约 1974 → 931 tokens）；“序号”列表按会话保存，群聊不再串号
- 🔌 `publish_request` 对外接口，供视频生成插件联动；新增 [docs/ROADMAP.md](docs/ROADMAP.md) 完整运维设计与路线图

### v0.7.0
- 🖼️ 浏览总结改为"关键帧 + 转写"交给当前对话模型（适配可看图的新版 DeepSeek），Gemini/纯文字逐级降级
- 👥 B站多账号：操作绑定账号、确认时用发起时的账号；账号池逐个续期
- 🛡️ 抗风控：bili_ticket、请求节流、按账号频率闸门、风控熔断、重复内容/链接拦截
- 🤖 全部功能注册为 LLM 工具；危险操作走管理员审批（提权）流程，结果回告申请人
- 🐛 WBI 签名空格编码修正（含空格的搜索词不再返回空结果）

### v0.6.0
- 🐛 修复 B站扫码登录：新版接口通过 Set-Cookie 下发登录态，旧代码从 URL 取 cookie 必然失败
- 🐛 B站无登录下载改为流式写盘（长视频不再 30 秒超时）
- 🔑 B站 refresh_token 自动续期；BilibiliCreator（稿件/评论/回复/发评论）；浏览（推荐/热门/搜索/抽帧+ASR）；资料（签名/头像）
- ♻️ Gemini 备用模型 `gemini_fallback_models`；浏览总结逐级降级

### v0.5.0
- 🛠️ 账号运维：定时巡检 + 状态变化告警（`/va_check` `/va_notify`），登录过期提前提醒
- 🔁 多账号池（`/va_pool`），抖音/B站风控或 cookie 失效连续失败时自动轮换
- 📊 WebUI 插件页「账号运维」
- 🎬 创作者操作（抖音）：发布 / 删除 / 评论精选回复，全部二次确认；新增 LLM 工具 `creator_upload_video`

### v0.4.0
- 🐛 修复抖音 `/理解视频` 与 `understand_video` 未使用浏览器签名导致 403
- 🐛 修复框架 Provider 查找报错（`provider_manager.providers` 已不存在）、人格注入失效
- 🐛 修复 av 号解析（改用 aid 直查，并更新为新版 AV/BV 算法）
- 🐛 B站 cookie 失效时不再在解析中途阻塞 40 秒等待扫码
- ♻️ Gemini 改为 REST 直连，移除已废弃的 `google-generativeai`；模型可配置；兼容中转站
- 🔒 登录/cookie 命令限管理员；`understand_video` 限制本地路径与内网地址
- 🧹 插件卸载/重载时关闭 Chromium 与 Xvfb；抖音风控空响应自动刷新重试
- 🔑 新增 `/va_accounts` `/va_logout` `/dy_restart`，账号数据迁移到 `plugin_data`

### v0.3.0
- 🔄 **模块化重构**：`main.py` 从 1349 行精简到 189 行
- 📦 **新架构**：拆分为 `handlers/` `tools/` `services/` `utils/` 四个包
- 🤖 **Agent 工具增强**：`understand_video` 支持 HTTP 直链视频
- 🔌 **MiMo 模型集成**：新增小米多模态视频理解
- 🔑 **抖音登录重构**：Playwright 全代理扫码 + Cookie 手动粘贴
- 🧹 **清理**：移除小红书解析、无用依赖（`browser-cookie3`、`asyncio`）

### v0.2.x
- Gemini AI 视频理解集成
- 抖音/B站/MC百科被动解析
- 本地 ASR + 关键帧分析
- 多种回复模式和合并转发

## 🤝 贡献

- Bug 报告：[提交 Issue](https://github.com/miaoxutao123/astrbot_plugin_videos_analysis/issues)
- 代码贡献：Fork 项目并提交 PR

## 📄 许可证

[GNU Affero General Public License v3.0](LICENSE)

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

Made with ❤️ by 喵喵

</div>
