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

### 🎬 创作者操作（抖音，平台可扩展）
> 发布、删除、回复评论都是**危险操作**：命令只生成预览和 4 位确认码，管理员发送 `/va_confirm <码>` 后才执行（默认 60 秒内有效）。
> 操作作用于账号池里**当前使用中**的账号，预览里会写明账号名。

- **发布视频**：`/va_upload 标题 #话题` 时附带视频、引用一条视频消息，或在 2 分钟内补发视频；也可让 Agent 通过 `creator_upload_video` 发起
- **删除作品**：`/va_works` 列出最近作品，`/va_delete <序号|链接|ID>`
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
- **抖音解析必需**：Chromium + 显示环境（抖音已拦截纯 Python 签名与无头浏览器）
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
1. 安装插件后重启 AstrBot
2. 配置 Gemini API（插件配置优先；未填写时复用框架里的 Gemini Provider）
3. 可选：开启 `auto_parse_enabled` 启用被动解析
4. Agent 可直接调用 `parse_video_link` / `understand_video` 工具

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
| `douyin_browser_signer` | 启用浏览器签名引擎（抖音解析必需） | `true` |

### 账号运维
| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `health_check_interval_minutes` | 定时巡检间隔，0 关闭 | `360` |
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
/bili_login          → B站扫码登录
/dy_login            → 抖音扫码登录
/dy_cookie xxx       → 手动设置抖音cookie（建议私聊发送）
/dy_restart          → 重启抖音浏览器签名引擎

# 账号运维
/va_check            → 立即深度巡检（不推送，直接回复结果）
/va_notify [off]     → 把当前会话设为（或取消）账号告警接收方
/va_pool             → 查看账号池
/va_pool save <douyin|bilibili> <名称>   → 保存当前登录为命名账号
/va_pool use|del <平台> <名称>            → 切换 / 删除
                       添加第二个号：/va_logout douyin → /dy_login 扫新号 → /va_pool save douyin 小号

# 创作者操作（[平台] 可省略，默认 douyin）
/va_works [平台]                    → 最近作品（带序号）
/va_upload [平台] <标题> #话题       → 发布（附带/引用视频，或随后补发）
/va_delete [平台] <序号|链接|ID>     → 删除作品
/va_reply [平台] [序号|链接|ID] [@抖音号] → 评论精选回复（LLM 起草；带 @ 时只回复这个人）
/va_confirm <码> [序号...]          → 执行待确认操作（评论回复可只发部分）
/va_edit <码> <序号> <新回复>        → 修改待发送的回复
/va_cancel <码> · /va_pending       → 取消 / 查看待确认操作
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
│   ├── creator/         # 创作者平台实现：base.CreatorPlatform + douyin
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

### v0.5.0（当前版本）
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
