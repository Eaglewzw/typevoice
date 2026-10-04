<p align="center"><img src="assets/typevoice.svg" width="88" alt="TypeVoice"></p>
<h1 align="center">TypeVoice</h1>
<p align="center">Your voice. Your words.</p>

Voice typing for Linux: hold Alt to speak, release to transcribe and paste into your focused application.

[Download](https://github.com/Eaglewzw/typevoice/releases/latest) · [中文](README.md) · [GPL-3.0](LICENSE)

Actively developed and maintained by [Eaglewzw](https://github.com/Eaglewzw).

## Features

- Global hold-to-talk or tap-to-toggle shortcut; Esc cancels recording.
- Compact bottom overlay with microphone icon, live audio waveform and timer.
- Cloud transcription, optional polishing, translation commands and terminology corrections.
- Automatic paste, terminal shortcut support, and text clipboard restoration.
- Settings, tray menu, optional autostart and encrypted local transcript history.
- Obtain and enter your own API key from your provider. No API keys are included; audio is sent to your chosen provider and usage is billed by that provider.

## Install and use

Supports **Ubuntu 22.04 / X11**. For Wayland sessions, select **Ubuntu on Xorg** at login.

Download the deb from [Releases](https://github.com/Eaglewzw/typevoice/releases/latest), then run:

```bash
sudo apt install ./typevoice_0.2.0-1_all.deb
typevoice
```

APT installs dependencies automatically. Obtain your own API key from your speech recognition provider,
then open settings, select that provider, and enter the key. Hold **Alt** to speak.

To upgrade, quit the old application and install the new package. Legacy configuration and history
are imported on first launch, with the original files kept as a backup.

## Settings and data

Use the settings window for common options, or edit `~/.config/typevoice/config.json` for advanced settings.

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
