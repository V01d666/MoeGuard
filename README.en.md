<p align="right">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <a href="https://moeguard.com/en/"><img src="https://img.shields.io/badge/Website-moeguard.com-1E8BFF?style=for-the-badge" alt="Website · moeguard.com"></a>
  <a href="https://ifdian.net/a/moeguard"><img src="https://img.shields.io/badge/Afdian-Support%20MoeGuard-946CE6?style=for-the-badge" alt="Support MoeGuard on Afdian"></a>
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

Then come play with it on the [MoeGuard website](https://moeguard.com/en/) first, and [take it home](#jump-right-in) if you like it!

Write a short description or drop artwork into the Workshop to make a pet of your own. Version 0.6.0 rebuilds the Windows client around one Workshop for creating, previewing, and enabling your pets.

Most of the time it hangs out at the edge of your screen while you work (or slack off), reacting when you poke it or pick it up. Once you've given patrol consent and registered the owner, you can also have it start patrol automatically when you lock the screen. Who says pets just freeload? (•̀ᴗ•́)و

It isn't a security camera dressed up as a pet. Think of it as a little partner who sometimes takes the job a bit too seriously: keeping you company most of the time, and keeping watch when needed

## MoeGuard today

- 🎨 **Pet Workshop**. Generate artwork from a description or drop in up to four images. Roll the dice for a name and character traits. Generated candidates appear directly in the preview list.
- 🎬 **Preview actions**. A pet's first generated actions save automatically and can be played from the action list. After previewing an edit to an existing action, select **Install action** to replace it.
- 🐾 **Desktop interactions**. A whale-themed pet is included, with more available in the Workshop. Drag your pet, resize it with the mouse wheel, or let it peek out at the edge of the screen. Click it for a reaction.
- 🎚️ **Quick settings**. Go to **Settings → Desktop pet** to switch instantly using **Current pet** or set each action to 1–60 FPS. Edit the **Boss key** under **General**.
- 📦 **Manage your pets**. Double-click a local card to rename or delete the pet. Use **Import role…** and **Export current role…** under **Settings → Desktop pet**.
- 📬 **Inbox**. Right-click the MoeGuard icon in the system tray and select **MoeGuard Inbox** from the menu. You can also open it from the **Workshop guide**.
- 🛡️ **Opt-in patrol**. Open **Settings → Patrol** to grant consent, select **Preview camera**, or open **Manage local evidence…**. A top-right indicator shows when patrol is capturing. Select **Stop patrol** from the tray to stop it.

## Jump right in

Download the **Windows x64 installer** and double-click it to get started, with no Python required

After installing v0.6.0, open **Settings → About → Software updates** and select **Check for updates**. When a new version is available, select **Update and restart**.

| 🌐 Official website | 🐙 GitHub Releases |
| :---: | :---: |
| [Download from the official website](https://moeguard.com/en/#hero-cta) | [v0.6.0 downloads and verification](https://github.com/V01d666/MoeGuard/releases/tag/v0.6.0) |

Just want the pet? No camera required. The first time you turn on watch duty, MoeGuard explains the risks and asks for your consent

<details>
<summary>🛠️ Build from source</summary>

On 64-bit Windows 10/11, install Node.js 24, the Rust MSVC toolchain, Visual Studio C++ Build Tools, the Windows SDK, WebView2 Runtime, and Python 3.12. Run these PowerShell commands from the public repository root to build the runnable client and its patrol component.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath artifacts/source-build/patrol --workpath artifacts/source-build/work scripts/windows/moeguard_patrol.spec
Set-Location desktop
npm.cmd ci
npm.cmd run build:windows:patrol
Copy-Item -Recurse -Force ../artifacts/source-build/patrol/MoeGuard-Patrol ./src-tauri/target/release/
& ./src-tauri/target/release/MoeGuard.exe
```

The build is in `desktop/src-tauri/target/release/`. Keep the full `MoeGuard-Patrol` folder beside `MoeGuard.exe`. If MoeGuard is already running, select **Quit MoeGuard** from its tray menu before launching the build.

</details>

### 🎟️ Want to try Pet Workshop?

MoeGuard is free and comes with a ready-to-use pet. Cloud generation uses separate credits for artwork and actions. Open **Pet Workshop → Get credits** and select **Get a code** to visit [Afdian](https://ifdian.net/a/moeguard). Enter the code in the same dialog and select **Redeem code**.

## Timeline

See [Releases](https://github.com/V01d666/MoeGuard/releases) for the full release history

<details open>
<summary><strong>V0.6</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `11-22` | 🐾 [**v0.6.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.6.0) | ✨ A Tauri-based Windows client with a redesigned Workshop and desktop interactions<br>✨ Bilingual Settings tabs, instant pet switching, and per-action FPS<br>🔧 Check for updates from Settings on installed copies |

</details>

<details>
<summary><strong>V0.5</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `11-14` | 🚀 [**v0.5.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.5.0) | ✨ Better character-art and action generation, with faster results<br>✨ Cancellation keeps candidates, installation stays responsive, and each character remembers its FPS<br>🔧 Better window restoration, action scale and transparent previews; fix growing idle memory use on Windows |

</details>

<details>
<summary><strong>V0.4</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `11-04` | 🚀 [**v0.4.2**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.2) | 🔧 Fix duplicate anonymous accounts from rapid first launches<br>🔧 One instance per data folder; retries and renewals keep the same identity |
| `10-26` | 🚀 [**v0.4.1**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.1) | ✨ The [MoeGuard website](https://moeguard.com/en/) launches: poke, pick up, and watch pets peek out<br>✨ Mailbox alerts, character renaming, and tidier Settings |
| `10-18` | 🚀 [**v0.4.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.0) | ✨ Automatic connection and credit refresh; clearer credit warnings<br>✨ Upgrades keep your support ID |

</details>

<details>
<summary><strong>V0.3</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `10-08` | 🚀 [**v0.3.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.3.0) | ✨ Update the installed app in place<br>✨ Built-in mailbox and support ID; view the version in Settings |

</details>

<details>
<summary><strong>V0.2</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `09-24` | 🚀 [**v0.2.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0) | ✨ Pet Workshop: progress, time estimates, and completion alerts<br>✨ Character export and import |
| `09-01` | 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview) | A limited trial opens; see the [Afdian announcement](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00) |
| `08-23` | 🎬 Demo | First hands-on demos: [text to pet](.github/assets/text2pet-demo.mp4)<br>[image to pet](.github/assets/image2pet-demo.mp4) |

</details>

<details>
<summary><strong>V0.1</strong></summary>

| Date | Version | Highlights |
| :--: | :-- | :-- |
| `08-18` | 🚀 [**v0.1.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0) | ✨ Three starter characters and desktop interactions<br>✨ Lock-screen watch duty |

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

That's the plan, but a feature only moves into “MoeGuard today” once it's built, tested, and ready to download. Progress is shared on GitHub, the [website](https://moeguard.com/en/), and [Afdian](https://ifdian.net/a/moeguard)

<details>
<summary>🔒 Camera and data</summary>

- **Watch footage**. The camera is used for patrol, camera preview, and owner registration. Face features, footage, and evidence stay on your PC. Patrol evidence is kept for seven days by default.
- **Cloud generation**: text and images are uploaded only when you start a generation; they are held on the service and cleaned up in campaign batches
- **Anonymous stats**: only a small set of usage events, an online heartbeat, and network timings, with no prompts, images, character names, local paths, or watch data
- **Withdraw consent**. Select **Stop patrol** in the tray menu. Then use **Withdraw patrol consent…** under **Settings → Patrol** to delete local owner features, evidence, and related records.

The client is fully open source, so every point above can be checked against the code. Use it on your own device in a private space where others know. Detection can miss events, and obstructions, a busy camera, or system sleep can interrupt watch duty; MoeGuard cannot replace professional security equipment

</details>

## Feedback and development

Found a bug or have an idea? Right-click the MoeGuard icon in the system tray and select **MoeGuard Inbox** from the menu. You can also go to **Pet Workshop → Workshop guide** and select **here** in the feedback prompt. Write a message, then select **Send** followed by **Confirm and send**. To copy your Client ID, open **Settings → About** and select **Copy** in **About and feedback**. You can also use [GitHub Issues](https://github.com/V01d666/MoeGuard/issues). Before posting logs publicly, remove your Client ID, usernames, local paths, photos, videos, owner features, and API keys.

MoeGuard is an after-hours hobby project, so there's no promise of enterprise response times, every-platform support, or a fixed release schedule. If it made you smile, a ⭐ Star or a little support on [Afdian](https://ifdian.net/a/moeguard) means a lot～

Want to contribute? Use the `.venv` created in the build steps above and run the frontend and Python checks from the repository root.

```powershell
npm.cmd --prefix desktop ci
npm.cmd --prefix desktop test
.\.venv\Scripts\python.exe -m ruff check moeguard tests scripts/windows
New-Item -ItemType Directory -Path .pytest-tmp -Force | Out-Null
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

The source code and the three bundled character asset sets are licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for a short statement about bundled models
