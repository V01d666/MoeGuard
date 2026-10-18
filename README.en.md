<p align="right">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="https://ifdian.net/a/moeguard">
    <img src="https://img.shields.io/badge/Afdian-Support%20MoeGuard-946CE6?style=for-the-badge" alt="Support MoeGuard on Afdian">
  </a>
</p>

# MoeGuard

> An anime desktop pet you can shape any way you like, one that grows along with you and happens to keep watch, too (｡•̀ᴗ-)✧

[![Release](https://img.shields.io/github/v/release/V01d666/MoeGuard?label=release&color=946CE6)](https://github.com/V01d666/MoeGuard/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](https://github.com/V01d666/MoeGuard/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<p align="center">
  <img src=".github/assets/cover.png" alt="MoeGuard cover" width="100%">
</p>

- Want a desktop pet but never found one you truly love?
- Even the cutest pet gets a little old after a few weeks?
- Dreamed of making your own, only to be scared off by modeling and animation?
- Think desktop pets are all cuteness and no use?

Then [give MoeGuard a try](https://github.com/V01d666/MoeGuard/releases)!

Write a short description or drop in a picture, and you get a pet that's truly yours, animations included. Bored of the look? Give it a makeover whenever you like

Most of the time it hangs out at the edge of your screen while you work (or slack off), reacting when you poke it or pick it up. Lock your screen and walk away, and, only with your explicit consent, it turns on the camera and keeps an eye on things. The moment you're back, the camera goes off and it tells you what happened while you were gone. Who says pets just freeload? (•̀ᴗ•́)و

It isn't a security camera dressed up as a pet. Think of it as a little partner who sometimes takes the job a bit too seriously: keeping you company most of the time, and keeping watch when needed

<p align="center">
  <img src=".github/assets/baseCharas.png" alt="Starter characters: Lumen, Poppy, and Rook" width="100%">
  <br>
  <sub>Starter characters: <a href="resources/roles/lumen/idle/0001.png">Lumen</a> · <a href="resources/roles/poppy/idle/0001.png">Poppy</a> · <a href="resources/roles/rook/idle/0001.png">Rook</a></sub>
</p>

## MoeGuard today

- 🎨 **Pet Workshop**: turn a few words or a single image into character art, pick your favorite, then bring it to life; beyond the idle loop, you choose which actions it gets
- 🔁 **Always fresh**: add or swap actions anytime, or redesign the whole look, with every earlier version kept safe
- ⏳ **No babysitting**: see real progress and a time estimate, then walk away; the tray and your pet will call you back when it's done
- 📦 **Take it with you**: export any character as a folder and import it again later, even on another PC
- 🐾 **Ready out of the box**: Lumen, Poppy, and Rook are ready to go; pick them up or drag them around, and they'll greet you when you return or peek out from the screen edges
- 🛡️ **Watch duty on the side**: start it by hand or, with separate permission, whenever you lock your screen; strangers or motion leave local snapshots and short clips, the tray, right-click menu, or boss key stops it in one click, and if something goes wrong it stops honestly instead of pretending to keep watch

## Jump right in

Download the **Windows x64 stable installer (recommended)** and double-click it to get started, with no Python required

Check for updates later in Settings; confirm to install and restart, keeping your settings, characters, and workshop history

Choose any of these channels for the same installer:

| 🐙 GitHub Releases | 🌐 Official direct link | ☁️ Baidu Netdisk |
| :---: | :---: | :---: |
| [Stable installer on GitHub](https://github.com/V01d666/MoeGuard/releases/latest/download/MoeGuard-Setup.exe) | [Official installer](https://api.moeproject.net/updates/windows/stable/MoeGuard-Setup.exe) | [Baidu Netdisk download](https://pan.baidu.com/s/1K_EUUpXF-CR2h0dJ_Dlr7g?pwd=moeg) |

Just want the pet? No camera required. The first launch explains the risks of watch duty; you can enjoy the pet and enable watch duty later in Settings

<details>
<summary>📦 Portable edition</summary>

Download the Portable ZIP, extract it completely, and double-click `MoeGuard.exe`

**To get new features and improvements, you'll need to download the latest version manually; we strongly recommend the installer (｡•̀ᴗ-)✧**

| 🐙 GitHub Releases | 🌐 Official direct link | ☁️ Baidu Netdisk |
| :---: | :---: | :---: |
| [Portable ZIP on GitHub](https://github.com/V01d666/MoeGuard/releases/download/v0.4.0/MoeGuard-0.4.0-win-Portable.zip) | [Official portable ZIP](https://api.moeproject.net/updates/windows/stable/MoeGuard-Portable.zip) | [Baidu Netdisk portable edition](https://pan.baidu.com/s/1089PgfbfbggP7I0Ld2wDFQ?pwd=moeg) |

</details>

<details>
<summary>🔎 File verification (v0.4.0)</summary>

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `MoeGuard-Setup.exe` | 206577756 | `316cde63faed3e4f10b3860d06a2c73a49543aa0e4f44c87e62a573010c16b96` |
| `MoeGuard-0.4.0-win-Portable.zip` | 198851273 | `0047bf7c83b24d41b342ba0dc74e78637112326c04c156147673f4d7dd127dd4` |

</details>

<details>
<summary>🛠️ Running from source</summary>

You'll need **64-bit Windows 10/11 and Python 3.12**:

```powershell
git clone https://github.com/V01d666/MoeGuard.git
cd MoeGuard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start_moeguard.bat
```

</details>

### 🎟️ Want to try Pet Workshop?

The starter characters and watch duty don't need a code. Pet Workshop generates in the cloud and counts character art and action generations separately. For now these credits come only from redemption codes, which aren't for sale yet; they're handed out from time to time in events on our [Afdian page](https://ifdian.net/a/moeguard), so drop by now and then～ Once you have one, redeem it under “设置 → 查看次数 / 兑换码…” (Settings → View credits / Redeem code; the app is currently Chinese-only)

## Timeline

- [2026/10/18] 🎉 [MoeGuard v0.4.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.0) is out! Service connections and generation credits refresh automatically, credit warnings are clearer, and upgrades keep your existing support ID
- [2026/10/08] 🎉 [MoeGuard v0.3.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.3.0) is out! The installed edition now supports updates in place, with a built-in mailbox and support ID for feedback and a version display in Settings
- [2026/09/24] 🎉 [MoeGuard v0.2.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0) is out! Pet Workshop is finally ready for everyone, along with generation progress and time estimates, done notifications, and character export and import
- [2026/09/01] 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview) opened a limited trial, putting Pet Workshop in people's hands for the first time; see the [Afdian announcement](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00)
- [2026/08/23] 🎬 The first hands-on Pet Workshop demos are here: watch [text to pet](.github/assets/text2pet-demo.mp4) and [image to pet](.github/assets/image2pet-demo.mp4) from start to finish
- [2026/08/18] 🚀 [MoeGuard v0.1.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0) is out, debuting three starter characters, desktop interactions, and lock-screen watch duty

## What's next?

Pet Workshop lets anyone create a character they love, but that's only the first step. Next, we want them to truly come alive:

- 🧠 **Personality profiles**: give your character a temperament and little habits, and edit them anytime
- 🗣️ **Signature lines**: every character you make gets its own catchphrases
- 💬 **Companion chat**: not just one line per poke, but an actual conversation
- 🎙️ **A voice of their own**: lines and chats spoken in a character's own voice, not just a speech bubble
- 🌱 **Grows with you**: a progression system where the longer you spend together, the better it knows you, with a memory far longer than a goldfish's
- 🎁 **One-click sharing**: pack a character into a single file and send it to a friend to import
- 📡 **Remote alerts**: the moment something stirs during watch duty, a push to your phone, so you can relax while you're out

That's the plan, but a feature only moves into “MoeGuard today” once it's built, tested, and ready to download. Progress is shared on GitHub and [Afdian](https://ifdian.net/a/moeguard)

<details>
<summary>🔒 Camera, cloud generation, and data handling</summary>

- **Camera**: used only for owner registration and watch duty. Face features, footage, and evidence stay on your PC; watch evidence is kept for 7 days by default
- **Connections and updates**: startup creates or reuses anonymous service credentials and a support ID, and reconnects once the network is back; local pet functions still work if the connection fails. Update requests carry no account credentials or cookies
- **Cloud generation**: active generation uploads text, reference images, and task assets, cleaned up on the service in campaign batches. Limited usage events, a daily heartbeat, and latency statistics include install type, uptime, Windows version and architecture, display scale, and role type and count; they exclude prompts, images, role names, local paths, and watch data
- **Mailbox**: messages are sent only after confirmation. Diagnostics are off by default and limited to the app version, Windows major version, architecture, and up to 5 recent error codes. Only read fingerprints and timestamps are kept locally. Cloud messages are currently not deleted automatically, and deleting local data does not delete messages already sent
- **Stopping and deleting**: revoke watch consent in Settings to stop capture and delete owner features and evidence. For a full wipe, quit the app and delete `%USERPROFILE%\.moeguard\`; this also removes anonymous account credentials

Use it on your own device in a private space where others know. Detection can miss events, and obstructions, a busy camera, or system sleep can interrupt watch duty. MoeGuard cannot replace your system lock screen or professional security equipment

</details>

## Feedback and development

Found a bug or have an idea? Open the mailbox and copy your support ID under “设置 → 通用 → 关于与反馈” (Settings → General → About and feedback), or come chat in [GitHub Issues](https://github.com/V01d666/MoeGuard/issues)! Before sharing logs, remove usernames, local paths, photos, videos, owner features, and API keys, and **never upload the whole `.moeguard`, `evidence`, or `owner` folder**

MoeGuard is an after-hours hobby project, so there's no promise of enterprise response times, every-platform support, or a fixed release schedule. If it made you smile, a ⭐ Star or a little support on [Afdian](https://ifdian.net/a/moeguard) means a lot～

Want to contribute? Start with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

The source code and the three bundled character asset sets are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for a short statement about bundled models
