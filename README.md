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

插件注册了两个 LLM Tool，供 Agent 主动调用：

| 工具名 | 功能 | 支持输入 |
|--------|------|----------|
| `parse_video_link` | 解析视频链接，返回标题、封面、直链等信息 | 抖音链接、B站链接 |
| `understand_video` | 深度理解视频内容 | 抖音/B站链接、HTTP直链、本地路径 |

> Agent 可以先用 `parse_video_link` 获取视频直链，再用 `understand_video` 理解直链视频内容。

### 🔑 登录管理
- **抖音**：QR码扫码登录（Playwright）/ 手动粘贴 Cookie
- **B站**：QR码扫码登录
- Cookie 自动持久化到 `data/plugin_data/astrbot_plugin_videos_analysis/accounts/`（插件更新不会丢失，旧版本数据首次启动自动迁移）
- `/va_accounts` 一览各平台登录态与 AI 服务连通性

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
│   └── admin_handler    # 管理命令
├── tools/               # LLM Agent 工具
│   ├── parse_tool       # parse_video_link
│   └── understand_tool  # understand_video
├── services/            # 平台服务层
│   ├── bilibili_service # B站解析下载
│   ├── douyin_service   # 抖音下载
│   ├── mcmod_service    # MC百科
│   ├── gemini_service   # Gemini REST 客户端
│   ├── account_manager  # 账号状态 / 注销（账号运维扩展点）
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

### v0.4.0（当前版本）
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
