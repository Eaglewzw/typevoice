<p align="center"><img src="assets/typevoice.svg" width="88" alt="TypeVoice"></p>
<h1 align="center">TypeVoice</h1>
<p align="center">Your voice. Your words.</p>
<p align="center">Linux 语音输入：按住说话，松开后文字直接进入输入框。</p>

<p align="center">
  <a href="https://github.com/Eaglewzw/typevoice/releases/latest">下载 deb 安装包</a> ·
  <a href="README.en.md">English</a> ·
  <a href="LICENSE">GPL-3.0</a>
</p>

由 [Eaglewzw](https://github.com/Eaglewzw) 持续开发与维护。

## 功能

- 全局快捷键录音：按住 Alt 说话，松开结束；轻按开始，再按结束；Esc 取消录音。
- 深色录音胶囊：麦克风图标、实时音量波形、计时与处理动画，固定在主屏底部。
- 云端识别与可选 AI 润色；失败时保留已识别原文，支持语音指定输出语言。
- 自动粘贴到当前输入框，支持终端快捷键与文本剪贴板恢复。
- 设置窗口、托盘、可选登录自启、本地加密文字历史。
- 用户需自行向服务商申请并填写自己的 API Key；软件不附带 API Key，音频发送到你选择的服务商，费用由对应服务商收取。

## 安装与使用

支持 **Ubuntu 22.04 / X11**。Wayland 用户请在登录时选择 **Ubuntu on Xorg**。

从 [Releases](https://github.com/Eaglewzw/typevoice/releases/latest) 下载 `.deb`，然后执行：

```bash
sudo apt install ./typevoice_0.2.0-1_all.deb
typevoice
```

`apt` 会自动安装依赖。先向服务商申请自己的 API Key，再打开设置，选择对应的识别服务并填写 Key，即可按住 **Alt** 开始说话。

升级时先退出旧版，再安装新包；首次启动会自动导入旧配置与历史，并保留原文件。

## 设置与数据

常用选项在设置窗口调整；高级设置可编辑 `~/.config/typevoice/config.json`。
API Key 可从 [火山引擎](https://console.volcengine.com/speech/app) 或
[阿里百炼](https://bailian.console.aliyun.com/) 申请，选择与 Key 对应的识别服务。

- **配置**：`~/.config/typevoice/`
- **历史与录音**：`~/.local/share/typevoice/`
- **日志**：`~/.cache/typevoice/`

## 开发与构建

源码运行需要 Python 3.10+、GTK3/Cairo 和录音工具：

```bash
git clone https://github.com/Eaglewzw/typevoice.git
cd typevoice
sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 alsa-utils pulseaudio-utils
./run.sh                        # 从源码启动
python3 scripts/build_deb.py     # 构建 deb，输出到 dist/
```

测试位于 `tests/`，打包脚本位于 `scripts/`。推送版本标签后，GitHub Actions 自动测试并发布安装包。

## 反馈

请在 [本仓库 Issues](https://github.com/Eaglewzw/typevoice/issues) 报告问题，并注明发行版、
X11 会话、安装版本和复现步骤。提交日志前请移除个人文本和凭证。
