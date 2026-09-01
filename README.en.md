<p align="right">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="https://ifdian.net/a/moeguard">
    <img src="https://img.shields.io/badge/Afdian-Support%20MoeGuard-946CE6?style=for-the-badge" alt="Support MoeGuard on Afdian">
  </a>
</p>

# MoeGuard

> An anime desktop companion that also happens to keep watch.

[![Status](https://img.shields.io/badge/status-experimental%20pre--alpha-orange)](.)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](.)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<p align="center">
  <img src=".github/assets/cover.png" alt="MoeGuard cover" width="100%">
</p>

MoeGuard starts with a simple idea: let a small companion live on your desktop.

It stays quietly nearby while you work. Poke it, pick it up, drag it around, or move it to the edge of the screen, and it reacts in its own way. When you lock your PC and step away, MoeGuard can—only after you explicitly opt in—turn on the camera and watch the room for a while. When you return, it closes the camera, goes back to being a desktop companion, and tells you whether anything happened.

It is not surveillance software wearing a cute skin. Think of it as an occasionally over-serious companion: company first, watching over the room when needed.

<p align="center">
  <img src=".github/assets/baseCharas.png" alt="The three starter characters: Lumen, Poppy, and Rook" width="100%">
  <br>
  <sub>The three starter characters: <a href="resources/roles/lumen/idle/0001.png">Lumen</a> · <a href="resources/roles/poppy/idle/0001.png">Poppy</a> · <a href="resources/roles/rook/idle/0001.png">Rook</a></sub>
</p>

## MoeGuard today

- Includes Lumen, Poppy, and Rook, each with its own animations and click dialogue.
- v0.2.0 Preview adds **Pet Workshop**: create a character from text or a reference image, choose a portrait, and generate only the desktop-pet actions you want.
- `idle` is the sole required action. Missing optional interactions safely fall back to idle.
- Installed characters can receive new or replacement actions, or a redesigned appearance, while older package versions remain available.
- Remote Preview tasks use separate portrait-generation and action-generation allowances. The first round uses only a small set of free redemption codes; no paid product is enabled.
- Supports idle, click, pickup, dragging, welcome, guarding, edge docking, and peek animations.
- Guarding can be started manually or, with separate permission, when Windows is locked.
- Unknown-person or motion events can create local screenshots and short clips that you can review and delete in the evidence manager.
- The tray menu, context menu, and boss key can stop guarding immediately. If owner data is missing, the camera is unavailable, or capture fails, MoeGuard exits guard mode instead of pretending it is still active.

The project is still an **experimental pre-alpha**. It may help keep an eye on things, but it is not professional security equipment and cannot replace your system lock screen or a dedicated monitoring product.

## Release

- [2026/09/01] 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview) has completed its first hands-on validation of Pet Workshop, remote generation allowances, task recovery, and post-install editing. A small free redemption-code trial is being prepared.
- [2026/08/23] 🎬 The first working Pet Workshop demos were completed. Watch the full [text-to-pet](.github/assets/text2pet-demo.mp4) and [image-to-pet](.github/assets/image2pet-demo.mp4) workflows.
- [2026/08/18] 🚀 [MoeGuard v0.1.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0) is out with three starter characters, complete desktop interactions, manual guarding, and separately authorized Windows lock-screen guarding.

## What comes next?

Pet Workshop is only the first step toward making each character feel alive. Future work may add editable personality profiles, character-specific lines, conversations, progression, and richer interactions. Plans will be shared on both GitHub and [Afdian](https://ifdian.net/a/moeguard). A feature will only be described as delivered after it has been built, tested, and made available to download or use.

## Run on Windows

For regular use, download the Windows x64 ZIP from [GitHub Releases](https://github.com/V01d666/MoeGuard/releases), extract the entire archive, and double-click `MoeGuard.exe`. Python is not required. The camera is unnecessary if you only want the desktop pet; it is used only when you enable guarding.

To run from source or inspect the local security logic, you need **64-bit Windows 10/11 and Python 3.12**:

```powershell
git clone https://github.com/V01d666/MoeGuard.git
cd MoeGuard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start_moeguard.bat
```

The first launch explains the risks of guard mode. You can use MoeGuard only as a desktop pet, or register yourself as the owner and authorize lock-screen guarding later in Settings.

## Camera, cloud generation, and your data

- Opening Settings, switching characters, or changing ordinary options does not probe or open the camera. MoeGuard attempts to open it only during owner registration or when guard mode starts.
- The camera is used continuously only while guarding. Frames, face features, and evidence remain on your computer; the base application has no cloud face recognition.
- Text, reference images, and task assets are sent to the MoeGuard generation service only when you explicitly create a portrait or action in Pet Workshop. Guard frames, owner features, and local evidence never enter this workflow.
- During Preview, related requests, prompts, reference images, and generated results may be retained for up to 30 days to diagnose generation quality, connectivity, and task recovery. The client shows a short notice before first use.
- Owner features are stored under `%USERPROFILE%\.moeguard\owner\`, and evidence under `%USERPROFILE%\.moeguard\evidence\`. Events are retained for seven days by default.
- Revoking guard consent in Settings stops capture and deletes owner features, evidence, and related records. If a file is locked or permissions prevent deletion, MoeGuard reports that cleanup is incomplete and preserves a retry path. To remove everything manually, exit MoeGuard and delete `%USERPROFILE%\.moeguard\`.
- The optional “blur unknown faces” setting blurs detected faces. If no valid face region is found, MoeGuard falls back to a low-resolution full-frame image; if the detector is unavailable, that raw frame is not saved. This still cannot eliminate every missed detection.
- Backlighting, occlusion, fast motion, another application using the camera, sleep, lid closure, and power-saving behavior can cause missed detections or interruptions. Use guard mode only on your own device and in a private space where everyone present is informed.

Open source lets you inspect when the camera starts, where data is written, how failures stop capture, and how local data is deleted. It does not guarantee detection accuracy or continuous guarding.

## Feedback and development

Issues and ideas are welcome through [GitHub Issues](https://github.com/V01d666/MoeGuard/issues). Before sharing logs, remove usernames, local paths, photos, videos, owner features, and API keys. **Do not upload the entire `.moeguard`, `evidence`, or `owner` directory.**

This is a hobby project developed outside regular working hours. It does not promise enterprise response times, every platform, or a fixed release schedule. To contribute, run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

The source code and the three bundled character asset sets are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for a short statement about bundled models.
