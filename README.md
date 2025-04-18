# AstrBot Plugin Videos Analysis

访问次数

</div>
<div align="center">




![:name](https://count.getloli.com/@astrbot_plugin_videos_analysis?name=astrbot_plugin_videos_analysis&theme=3d-num&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

</div>

AstrBot 插件，用于解析和下载抖音和 Bilibili 视频。（测试版已上线，有问题请提issue）

**~~注意，本插件只测试了napcat下的qq，其他协议无法保证可用性~~**

预计下个大版本支持gewe平台

**注意⚠️：过大的视频会导致发送失败，qq对视频大小及长度做了限制，抖音默认解析API不保证100%有效，**
       **有条件建议本地部署，插件默认30min清理一次下载视频缓存，nap端🐱拉的💩需要手动去铲或者设置定时脚本**
       **视频最重要的‼️‼️一定要正确安装ffmpeg！！！！！**

## 功能

- 自动解析抖音和 Bilibili 视频分享链接
- 直接发送无水印视频到qq
- 支持多段视频和图片的解析与下载
- 支持bilibili登陆下载高质量视频

## 安装

直接在插件市场安装

## 使用
**新版登录下载由于视频音频不在一个流的原因，需要正确安装ffmpeg**

**注意，本插件只测试了napcat下的qq，其他协议无法保证可用性**

~~请先部署该项目：https://github.com/Evil0ctal/Douyin_TikTok_Download_API~~

~~本插件使用该项目的api以获取抖音聚合数据解析，由于部分分p视频该api无法正确解析，所以本插件会使用自带的解析脚本进行解析，可能需要手动配置部分cookie及请求头，如果遇到问题可以提issues。~~

默认使用了项目提供的解析api，速度略慢于自主部署，如有需要仍可自行部署后更改解析api

配置完成后可以自动解析抖音分享链接和bilibili分享链接，并发送无水印视频，速度取决于您的服务器带宽和视频大小，效果如下图所示

**如果打开了bilibili登陆请前往控制台扫码登陆**

![1742202464173](image/README/1742202464173.png)

![1742202476295](image/README/1742202476295.png)

![1742202484215](image/README/1742202484215.png)

## 贡献

欢迎提交问题和拉取请求。

## todo

增加视频下载限制，避免导致服务器爆炸

~~增加对小程序分享的解析~~

~~增加视频详情信息~~

已知bug：无法解析抖音的视频图片混合作品链接，无法解析类似于"https://v.douyin.com/Xap7_ju9FgM"这样的奇怪链接（正在修复），无法解析旧版bilibili分享的小程序

增加更多平台的解析

增加过大视频转为文件发送

## 许可证

此项目基于 [GNU Affero General Public License](LICENSE) 许可证。
