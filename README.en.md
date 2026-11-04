<p align="right">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="https://moeproject.net/en/">
    <img src="https://img.shields.io/badge/Website-moeproject.net-1E8BFF?style=for-the-badge" alt="Website · moeproject.net">
  </a>
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

Then come play with it on the [MoeGuard website](https://moeproject.net/en/) first, and [take it home](#jump-right-in) if you like it!

Write a short description or drop in a picture, and you get a pet that's truly yours, animations included. Bored of the look? Give it a makeover whenever you like

Most of the time it hangs out at the edge of your screen while you work (or slack off), reacting when you poke it or pick it up. Lock your screen and walk away, and, only with your explicit consent, it turns on the camera and keeps an eye on things. The moment you're back, the camera goes off and it tells you what happened while you were gone. Who says pets just freeload? (•̀ᴗ•́)و

It isn't a security camera dressed up as a pet. Think of it as a little partner who sometimes takes the job a bit too seriously: keeping you company most of the time, and keeping watch when needed

## MoeGuard today

- 🎨 **Pet Workshop**: turn a few words or a single image into character art, pick your favorite, then bring it to life; beyond the idle loop, you choose which actions it gets
- 🔁 **Always fresh**: add or swap actions, rename, or redesign the whole look anytime, with every earlier version kept safe
- ⏳ **No babysitting**: see real progress and a time estimate, then walk away; the tray and your pet will call you back when it's done
- 📦 **Take it with you**: export any character as a folder and import it again later, even on another PC
- 🐾 **Ready out of the box**: Lumen, Poppy, and Rook are ready to go; pick them up or drag them around, and they'll greet you when you return or peek out from the screen edges
- 📬 **Never miss a note**: announcements and replies from the author pop up in the bottom-right corner; open “信箱…” (Mailbox…) from the tray to read them
- 🛡️ **Watch duty on the side**: start it by hand or, with separate permission, whenever you lock your screen; strangers or motion leave local snapshots and short clips, the tray, right-click menu, or boss key stops it in one click, and if something goes wrong it stops honestly instead of pretending to keep watch

## Jump right in

Download the **Windows x64 installer** and double-click it to get started, with no Python required

When a new version is out, MoeGuard lets you know; just click “更新” (Update) and you're set, with nothing to download again by hand. Your characters, settings, credits, and workshop history all stay put

| 🌐 Official direct link | 🐙 GitHub Releases |
| :---: | :---: |
| [Official installer](https://api.moeproject.net/updates/windows/stable/MoeGuard-Setup.exe) | [Installer on GitHub](https://github.com/V01d666/MoeGuard/releases/latest/download/MoeGuard-Setup.exe) |

Just want the pet? No camera required. The first time you turn on watch duty, MoeGuard explains the risks and asks for your consent

<details>
<summary>🔎 File verification (v0.4.2)</summary>

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `MoeGuard-Setup.exe` | 206874814 | `b37c11a1672f54f792b8954e5468b56cce88322f311f5df63eea823f02d0b866` |

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

MoeGuard itself is free, and the starter characters and watch duty work right away. Pet Workshop generates in the cloud and counts character art and action generations as separate credits: new users get a trial allowance the first time they open it, enough for one complete pet; after that, top up with a redemption code from [Afdian](https://ifdian.net/a/moeguard) and redeem it under “设置 → 通用 → 查看次数 / 兑换码…” (Settings → General → View credits / Redeem code; the app is currently Chinese-only)

## Timeline

<details>
<summary><strong>v0.4 · Connections and everyday improvements</strong></summary>

- [v0.4.2](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.2): fixes duplicate anonymous accounts when the app is opened several times on first launch; only one instance runs per user data folder, and connection retries and renewals reuse the same identity
- [2026/10/26] 🌐 [v0.4.1](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.1): the [MoeGuard website](https://moeproject.net/en/) launches with pet interactions in your browser, while the app gains mailbox alerts, character renaming, and tidier Settings
- [2026/10/18] 🎉 [v0.4.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.0): service connections and generation credits refresh automatically, credit warnings are clearer, and upgrades keep your support ID

</details>

<details>
<summary><strong>v0.3 · Installer updates and mailbox</strong></summary>

- [2026/10/08] 🎉 [v0.3.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.3.0): the installed edition supports updates in place, with a built-in mailbox, support ID, and version display in Settings

</details>

<details>
<summary><strong>v0.2 · Pet Workshop</strong></summary>

- [2026/09/24] 🎉 [v0.2.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0): Pet Workshop arrives with generation progress and time estimates, completion notifications, and character export and import
- [2026/09/01] 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview): a limited trial opens; see the [Afdian announcement](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00)
- [2026/08/23] 🎬 First hands-on demos: [text to pet](.github/assets/text2pet-demo.mp4) and [image to pet](.github/assets/image2pet-demo.mp4)

</details>

<details>
<summary><strong>v0.1 · Three starter pets and watch duty</strong></summary>

- [2026/08/18] 🚀 [v0.1.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0): three starter characters, desktop interactions, and lock-screen watch duty debut

</details>

## What's next?

Pet Workshop lets anyone create a character they love, but that's only the first step. Next, we want them to truly come alive:

- 🧠 **Personality profiles**: give your character a temperament and little habits, and edit them anytime
- 🗣️ **Signature lines**: every character you make gets its own catchphrases
- 💬 **Companion chat**: not just one line per poke, but an actual conversation
- 🎙️ **A voice of their own**: lines and chats spoken in a character's own voice, not just a speech bubble
- 🌱 **Grows with you**: a progression system where the longer you spend together, the better it knows you, with a memory far longer than a goldfish's
- 🎁 **One-click sharing**: pack a character into a single file and send it to a friend to import
- 📡 **Remote alerts**: the moment something stirs during watch duty, a push to your phone, so you can relax while you're out

That's the plan, but a feature only moves into “MoeGuard today” once it's built, tested, and ready to download. Progress is shared on GitHub, the [website](https://moeproject.net/en/), and [Afdian](https://ifdian.net/a/moeguard)

<details>
<summary>🔒 Camera and data</summary>

- **Watch footage**: the camera turns on only for owner registration and watch duty; face features, footage, and evidence stay on your PC, and watch evidence is kept for 7 days by default
- **Cloud generation**: text and images are uploaded only when you start a generation; they are held on the service and cleaned up in campaign batches
- **Anonymous stats**: only a small set of usage events, an online heartbeat, and network timings, with no prompts, images, character names, local paths, or watch data
- **Opt out anytime**: revoking watch consent stops capture and deletes owner features and evidence; for a full wipe, quit the app and delete `%USERPROFILE%\.moeguard\`

The client is fully open source, so every point above can be checked against the code. Use it on your own device in a private space where others know. Detection can miss events, and obstructions, a busy camera, or system sleep can interrupt watch duty; MoeGuard cannot replace professional security equipment

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
