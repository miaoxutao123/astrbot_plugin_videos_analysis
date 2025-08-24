# AstrBot 视频解析插件

<div align="center">

![访问次数](https://count.getloli.com/@astrbot_plugin_videos_analysis?name=astrbot_plugin_videos_analysis&theme=3d-num&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

</div>

一个功能强大的 AstrBot 插件，支持多平台视频内容解析、下载和 AI 智能分析。

## ✨ 主要功能

### 📱 多平台视频解析
- **抖音（TikTok）**：图片、视频、多段视频无水印解析下载
- **哔哩哔哩（Bilibili）**：视频解析、高清下载（支持登录获取高画质）
- **小红书（XiaoHongShu）**：图片、视频内容解析
- **MC百科（MCMod）**：模组和整合包信息解析

### 🤖 AI 视频理解
- **Gemini AI 集成**：智能视频内容分析
- **多种分析模式**：
  - 小视频：直接上传 Gemini 分析
  - 大视频：音频转录 + 关键帧提取 + 综合分析
- **智能配置检测**：自动使用框架 Gemini 配置或插件独立配置
- **个性化回应**：结合人格设定提供自然回复

### 🔧 技术特性
- **本地文件下载**：所有媒体先下载到本地再发送，稳定可靠
- **智能文件管理**：
  - 根据文件大小自动选择发送方式（视频/文件）
  - 自动清理过期文件
  - 避免重复下载
- **NAP 服务器支持**：支持通过 NAP 服务器进行文件传输
- **错误容错处理**：完善的异常处理和重试机制

### 📊 用户体验
- **实时进度提示**：解析和下载进度实时反馈
- **合并转发支持**：多文件内容支持合并发送
- **多种回复模式**：纯文本、图片、视频等多种回复格式

## 🚀 安装使用

### 安装方式
直接在 AstrBot 插件市场搜索并安装 `astrbot_plugin_videos_analysis`

### 依赖要求
- **FFmpeg**：用于视频处理（音视频分离、关键帧提取等）
- **Python 3.8+**：基础运行环境

### 基础使用
1. 安装插件后重启 AstrBot
2. 发送支持的平台分享链接即可自动解析
3. 如需使用 AI 视频理解功能，请配置 Gemini API

### 高级功能配置
- **Bilibili 高清下载**：开启登录选项，扫码登录获取高画质视频
- **AI 视频分析**：配置 Gemini API Key 启用智能视频理解
- **文件传输**：配置 NAP 服务器实现跨服务器文件传输

## ⚙️ 配置选项

| 配置项 | 描述 | 默认值 |
|--------|------|--------|
| `nap_server_address` | NAP 服务器地址 | localhost |
| `nap_server_port` | NAP 服务器端口 | 3658 |
| `delete_time` | 文件清理时间（分钟） | 60 |
| `max_video_size` | 视频大小限制（MB） | 200 |
| `bili_quality` | B站视频清晰度 | 32 (480P) |
| `bili_reply_mode` | B站回复模式 | 3 (图片+视频) |
| `url_video_comprehend` | 链接视频分析功能 | false |
| `gemini_api_key` | Gemini API密钥 | "" |
| `doyin_cookie` | 抖音Cookie | 预设值 |

## 🎯 使用示例

### 基础解析
```
用户：https://v.douyin.com/xxxxxx/
Bot：正在解析抖音链接...
Bot：正在下载媒体文件...
Bot：[发送无水印视频]
```

### AI 视频理解
```
用户：https://www.bilibili.com/video/BVxxxxxxx
Bot：我看到了一个B站视频链接，让我来仔细分析一下内容...
Bot：视频大小为 45.2MB，直接上传视频进行分析...
Bot：[发送关键帧图片]
Bot：[AI分析回复] 这个视频讲述了...我觉得这个观点很有趣...
```

## 🙏 特别鸣谢

本项目的抖音解析功能基于以下开源项目：

**[Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)**
- 提供了完整的抖音视频解析方案
- 贡献了核心的加密算法和请求处理逻辑
- 感谢 [@Evil0ctal](https://github.com/Evil0ctal) 及所有贡献者的辛勤工作

如果觉得本插件好用，请考虑为原项目点个 ⭐ Star！

## 🔄 更新日志

### v0.2.8
- ✅ 修复抖音视频下载 403 错误
- ✅ 优化下载重试机制和错误处理
- ✅ 改进 Cookie 处理和请求头配置
- ✅ 增强本地文件发送稳定性

### 历史版本
- ✅ 集成 Gemini AI 视频理解
- ✅ 支持多平台内容解析
- ✅ 添加文件自动清理功能
- ✅ 实现本地下载发送模式

## 📋 已知问题 & 计划

### 已知问题
- 部分抖音混合作品（图片+视频）可能解析失败
- QQ 对视频大小和时长有限制，过大视频会发送失败

### 开发计划
- [ ] 支持更多视频平台（YouTube、Instagram 等）
- [ ] 优化大文件处理性能
- [ ] 增加视频转码功能
- [ ] 支持批量下载和处理

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

- Bug 报告：[提交 Issue](https://github.com/miaoxutao123/astrbot_plugin_videos_analysis/issues)
- 功能建议：[讨论区](https://github.com/miaoxutao123/astrbot_plugin_videos_analysis/discussions)
- 代码贡献：Fork 项目并提交 PR

## 📄 许可证

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 开源许可证。

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

Made with ❤️ by 喵喵

</div>
