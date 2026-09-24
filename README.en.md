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

[![Release](https://img.shields.io/github/v/release/V01d666/MoeGuard?label=release&color=946CE6)](https://github.com/V01d666/MoeGuard/releases)
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

Write a short description or drop in a picture, and you get a pet that's truly yours, animations included. Bored of the look? Give it a makeover whenever you like.

Most of the time it hangs out at the edge of your screen while you work (or slack off), reacting when you poke it or pick it up. Lock your screen and walk away, and, only with your explicit consent, it turns on the camera and keeps an eye on things. The moment you're back, the camera goes off and it tells you what happened while you were gone. Who says pets just freeload? (•̀ᴗ•́)و

It isn't a security camera dressed up as a pet. Think of it as a little partner who sometimes takes the job a bit too seriously: keeping you company most of the time, and keeping watch when needed.

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

Grab the Windows x64 ZIP from [GitHub Releases](https://github.com/V01d666/MoeGuard/releases), extract all of it, and double-click `MoeGuard.exe`. No Python needed. Just want the pet? No camera required; it's only used for watch duty.

The first launch walks you through the risks of watch duty. You can simply enjoy the pet and turn on watch duty in Settings whenever you're ready.

<details>
<summary>Running from source?</summary>

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

The starter characters and watch duty don't need a code. Pet Workshop generates in the cloud and counts character art and action generations separately. For now these credits come only from redemption codes, which aren't for sale yet; they're handed out from time to time in events on our [Afdian page](https://ifdian.net/a/moeguard), so drop by now and then～ Once you have one, redeem it under “设置 → 查看次数 / 兑换码…” (Settings → View credits / Redeem code; the app is currently Chinese-only).

## Timeline

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

That's the plan, but a feature only moves into “MoeGuard today” once it's built, tested, and ready to download. Progress is shared on GitHub and [Afdian](https://ifdian.net/a/moeguard).

## Camera, cloud generation, and your data

- The camera opens only for owner registration and watch duty, never when you switch characters or change settings
- Watch footage, face features, and evidence all stay on your PC; there is no cloud face recognition
- Only when you generate something in Pet Workshop are the related text, reference image, and task assets uploaded; to diagnose quality and interrupted tasks, these materials are temporarily retained on the service and cleaned up in campaign batches
- Pet Workshop also reports a small set of usage events (screen, step, duration, error code, task ID) to help troubleshoot, with no prompts, images, local paths, or watch data
- Owner features live in `%USERPROFILE%\.moeguard\owner\` and evidence in `%USERPROFILE%\.moeguard\evidence\`; watch events are kept for 7 days by default
- Revoking watch consent in Settings stops capture and deletes owner features and evidence, and tells you plainly if something can't be deleted; for a full wipe, quit MoeGuard and delete the whole `%USERPROFILE%\.moeguard\` folder
- Optional “blur strangers' faces”: if no face is found the whole frame is downscaled, and if the detector is unavailable nothing is saved at all, though a few may still slip through
- Backlight, obstructions, people passing quickly, a busy camera, or a closed lid, sleep, and power saving can all cause misses or interruptions; please use it only on your own device, in a private space where everyone knows

MoeGuard tries hard to help, but it isn't a professional security device. It can't replace your system lock screen or a real surveillance product, and it doesn't guarantee accurate detection or uninterrupted watch. Open source means you can see for yourself when the camera turns on, where data goes, how it stops when something breaks, and how to delete what you no longer want.

## Feedback and development

Found a bug or have an idea? Come chat in [GitHub Issues](https://github.com/V01d666/MoeGuard/issues)! Before sharing logs, remove usernames, local paths, photos, videos, owner features, and API keys, and **never upload the whole `.moeguard`, `evidence`, or `owner` folder**.

MoeGuard is an after-hours hobby project, so there's no promise of enterprise response times, every-platform support, or a fixed release schedule. If it made you smile, a ⭐ Star or a little support on [Afdian](https://ifdian.net/a/moeguard) means a lot～

Want to contribute? Start with:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

The source code and the three bundled character asset sets are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for a short statement about bundled models.
