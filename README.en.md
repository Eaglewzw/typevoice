<p align="center"><img src="assets/typevoice.svg" width="88" alt="TypeVoice"></p>
<h1 align="center">TypeVoice</h1>
<p align="center">Your voice. Your words.</p>

Voice typing for Linux: hold Alt to speak, release to transcribe and paste into your focused application.

[Download](https://github.com/Eaglewzw/typevoice/releases/latest) · [中文](README.md) · [GPL-3.0](LICENSE)

Actively developed and maintained by [Eaglewzw](https://github.com/Eaglewzw).

## Features

- **Speech to text**: Record with a global shortcut and transcribe in the cloud.
- **Text refinement**: Optional AI polishing, spoken output-language commands, and custom terminology corrections.
- **Automatic input**: Insert the result directly into the focused application.

## Install and use

Supports **Ubuntu 22.04 / X11**. For Wayland sessions, select **Ubuntu on Xorg** at login.

Download the deb from [Releases](https://github.com/Eaglewzw/typevoice/releases/latest), then run:

```bash
sudo apt install ./typevoice_0.2.0-1_all.deb
typevoice
```

APT installs dependencies automatically. Obtain your own API key from your speech recognition provider,
then open settings, select that provider, and enter the key. Hold **Alt** to speak.

No API keys are included. Audio is sent to your chosen provider, which bills you for usage.

Hold Alt alone for about 0.18 seconds, speak after the cue, and release to finish. Tap and release to keep recording, tap again to finish, or press Esc to cancel. Alt+S, Alt+Tab and other combinations keep their usual behavior.

To upgrade, quit the old application and install the new package. Legacy configuration and history
are imported on first launch, with the original files kept as a backup.

## Settings and data

Use the settings window for common options, or edit `~/.config/typevoice/config.json` for advanced settings.

Open History from settings or the tray to view and copy original transcripts and final text. Enable recording storage in that window to play back future recordings; past audio cannot be restored. You can also open it with `typevoice history --window`.

- **Configuration**: `~/.config/typevoice/`
- **History and recordings**: `~/.local/share/typevoice/`
- **Logs**: `~/.cache/typevoice/`

## Develop and build

Source development requires Python 3.10+, GTK3/Cairo and audio utilities:

```bash
git clone https://github.com/Eaglewzw/typevoice.git
cd typevoice
sudo apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 alsa-utils pulseaudio-utils
./run.sh                        # Run from source
python3 scripts/build_deb.py     # Build the deb in dist/
```

Tests live in `tests/`, build scripts in `scripts/`. Version tags trigger automated testing and release publishing.
