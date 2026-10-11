TypeVoice 0.2.1 — Your voice. Your words.

### 本次更新

- 润色提示词新增整理后的整段复核：检查语义合理性、必要句子成分、通顺度和日常表达习惯，修复后再次复核，只输出最终文本。
- 允许补全上下文能明确、唯一确定的缺失成分，同时保留自然的口语省略，避免补入原文没有的信息或改变用户原意。
- 目标语言译文也进行相同复核，按目标语言的语法和日常语感修复表达。
- 同步版本号、安装示例和 Debian 更新记录，移除发布流程中已过时的旧品牌 Release 清理步骤。

本次增强适用于进入 AI 润色流程的文本；未指定目标语言时，不超过 10 个有效字符的文本仍直接输出。关闭润色时不调用模型。

### 安装或升级

先退出旧版应用，从本页 **Assets** 下载 `typevoice_0.2.1-1_all.deb`：

```bash
sudo apt install ./typevoice_0.2.1-1_all.deb
typevoice
```

APT 会安装缺少的依赖并替换旧包。不要只运行 `dpkg -i`；如果旧安装尚未配置完成，先运行 `sudo apt --fix-broken install`。

需要 **Ubuntu 22.04 / X11**；Wayland 用户请在登录界面选择 **Ubuntu on Xorg**。
其他发行版尚未实机验证。安装后可在应用菜单搜索 TypeVoice，需要自行向服务商申请并配置自己的 API Key。

下载校验文件后可运行：

```bash
sha256sum -c typevoice_0.2.1-1_all.deb.sha256
```

### 数据与验证

新路径为 `~/.config/typevoice/`、`~/.local/share/typevoice/`、`~/.cache/typevoice/`，支持 XDG 覆盖。
历史的加密格式保持兼容；已有 TypeVoice 数据目录不会被覆盖。

发布流程验证逻辑、界面与投递、数据迁移、deb 权限和校验和、解包启动、自启与退出；不调用云端 API。

### 来源与许可

基于 [kdsz001/typefree](https://github.com/kdsz001/typefree) 的 GPL-3.0 代码，
由 Eaglewzw 独立维护 Linux 移植与修改。TypeVoice 不是上游官方发行版，版权说明见 NOTICE。
