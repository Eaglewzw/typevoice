TypeVoice 0.2.0 — Your voice. Your words.

### 本次更新

- 精简托盘菜单，移除暂停／恢复功能。
- 新增历史记录窗口：查看与复制识别原文、最终文本，加载更早记录；可开启后续录音保存，并播放、停止已保存的录音。设置窗口与托盘菜单均提供入口。
- 更新 0.2.0 安装包的录音提示音：启动音改为 140 毫秒的轻柔短音；结束音使用相同音色，音量比启动音提高约 9.5 dB。
- 明确 API Key 需由用户自行向服务商申请并填写；软件不附带个人或共享密钥，相关说明已同步到设置界面和配置模板。
- 正式统一命名为 TypeVoice：应用、命令 `typevoice`、Python 模块、deb 包、桌面入口、图标与配置目录全部更新。
- 工程仅保留 Linux 代码，移除原 macOS 程序、官网和宣传素材；GPL 许可证和上游署名保留在 LICENSE / NOTICE。
- 保留深色录音胶囊、实时波形、设置窗口、托盘与语音输入功能。
- 首次启动自动导入旧配置、加密历史与密钥、录音存档，保留原文件作为备份，不覆盖已有 TypeVoice 数据。
- 支持从旧安装包升级，转换旧登录自启入口。

### 安装或升级

先退出旧版应用，从本页 **Assets** 下载 `typevoice_0.2.0-1_all.deb`：

```bash
sudo apt install ./typevoice_0.2.0-1_all.deb
typevoice
```

APT 会安装缺少的依赖并替换旧包。不要只运行 `dpkg -i`；如果旧安装尚未配置完成，先运行 `sudo apt --fix-broken install`。

需要 **Ubuntu 22.04 / X11**；Wayland 用户请在登录界面选择 **Ubuntu on Xorg**。
其他发行版尚未实机验证。安装后可在应用菜单搜索 TypeVoice，需要自行向服务商申请并配置自己的 API Key。

下载校验文件后可运行：

```bash
sha256sum -c typevoice_0.2.0-1_all.deb.sha256
```

### 数据与验证

新路径为 `~/.config/typevoice/`、`~/.local/share/typevoice/`、`~/.cache/typevoice/`，支持 XDG 覆盖。
历史的加密格式保持兼容；已有 TypeVoice 数据目录不会被覆盖。

发布流程验证逻辑、界面与投递、数据迁移、deb 权限和校验和、解包启动、自启与退出；不调用云端 API。

### 来源与许可

基于 [kdsz001/typefree](https://github.com/kdsz001/typefree) 的 GPL-3.0 代码，
由 Eaglewzw 独立维护 Linux 移植与修改。TypeVoice 不是上游官方发行版，版权说明见 NOTICE。
