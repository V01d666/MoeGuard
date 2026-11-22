<p align="right">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="https://moeguard.com/"><img src="https://img.shields.io/badge/%E5%AE%98%E7%BD%91-moeguard.com-1E8BFF?style=for-the-badge" alt="官网 · moeguard.com"></a>
  <a href="https://ifdian.net/a/moeguard"><img src="https://img.shields.io/badge/%E7%88%B1%E5%8F%91%E7%94%B5-%E6%94%AF%E6%8C%81%E8%90%8C%E5%8D%AB-946CE6?style=for-the-badge" alt="爱发电 · 支持萌卫"></a>
</p>

# 萌卫 MoeGuard

> 一只能随心定制、陪你一起进步，还恰好能看家的二次元桌宠 (｡•̀ᴗ-)✧

[![Release](https://img.shields.io/github/v/release/V01d666/MoeGuard?label=release&color=946CE6)](https://github.com/V01d666/MoeGuard/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](https://github.com/V01d666/MoeGuard/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<p align="center">
  <img src=".github/assets/cover.png" alt="萌卫 MoeGuard 封面" width="100%">
</p>

- 想要一只桌宠，却总找不到真正心动的那一只？
- 再可爱的桌宠，天天看也难免审美疲劳？
- 想亲手做一只，却被建模和动画劝退？
- 总觉得桌宠只会卖萌，派不上什么用场？

那就先去[萌卫官网](https://moeguard.com/)和它玩一会儿，喜欢再[领回家](#马上开玩)吧！

写一段描述，或把立绘拖进工坊，就能做出自己的桌宠。v0.6.0 重做了 Windows 客户端，创作、预览和启用都在同一个工坊里完成。

平时它趴在屏幕边上陪你摸鱼，戳一戳、拎起来都有反应。完成值守授权和主人注册后，也可以让它在锁屏时自动上岗。谁说桌宠只会吃白饭？(•̀ᴗ•́)و

它不是披着桌宠外衣的安防设备，更像一位偶尔认真过头的小搭档：平时陪你，必要时顺手看家

## 现在的萌卫

- 🎨 **桌宠工坊**：从文字生成立绘，或一次拖入最多四张图片。骰子可以随机填入名字和角色设定。候选生成后直接出现在预览列表中。
- 🎬 **边做边看**：首次生成的动作自动存入角色库，点动作列表即可预览。修改已有动作时，预览满意后再点「安装动作」替换。
- 🐾 **桌面互动**：默认内置鲸鱼娘，更多角色可从工坊下载。桌宠支持拖动、滚轮缩放和边缘探头，点击时会播放反应动作。
- 🎚️ **快速设置**：打开「设置 → 桌宠」，可从「当前桌宠」即时切换就绪角色，也可为不同动作设置 1–60 FPS。老板键可在「通用」页修改。
- 📦 **管理角色**：双击本地角色卡片可以改名或删除。在「设置 → 桌宠」可使用「导入角色…」和「导出当前角色…」。
- 📬 **信箱**：右键点击系统托盘中的萌卫图标，在弹出的菜单里选择「萌卫信箱」。也可以从工坊的「使用教程」进入。
- 🛡️ **授权值守**：在「设置 → 值守」可授权值守、选择「预览摄像头」或「管理本地证据…」。采集时屏幕右上角会有提示，可从托盘选择「停止值守」。

## 马上开玩

下载 **Windows x64 安装版**，双击安装就能开玩，不用装 Python

已安装 v0.6.0 后，可以在「设置 → 关于 → 软件更新」点「检查更新」。发现新版本时可选择「更新并重启」。

| 🌐 官网 | 🐙 GitHub Releases |
| :---: | :---: |
| [前往官网下载](https://moeguard.com/#hero-cta) | [v0.6.0 下载与校验信息](https://github.com/V01d666/MoeGuard/releases/tag/v0.6.0) |

只养桌宠用不到摄像头；第一次开启看家时，萌卫会先讲清楚风险、征得你的同意

<details>
<summary>🛠️ 从源码构建</summary>

在 64 位 Windows 10/11 上准备 Node.js 24、Rust MSVC 工具链、Visual Studio C++ Build Tools、Windows SDK、WebView2 Runtime 和 Python 3.12。从 Public 仓库根目录运行以下 PowerShell 命令，构建可直接运行的客户端和值守组件。

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

构建产物在 `desktop/src-tauri/target/release/`，请将整个 `MoeGuard-Patrol` 目录保留在 `MoeGuard.exe` 旁边。启动前请从托盘选择「退出萌卫」，退出已运行的实例。

</details>

### 🎟️ 想试试桌宠工坊？

萌卫本体免费，自带桌宠可以直接使用。工坊里的云端立绘和动作生成分别计算次数。打开「桌宠工坊 → 获取次数」，可点「获取兑换码」前往[爱发电](https://ifdian.net/a/moeguard)，拿到兑换码后返回输入并点「确认兑换」。

## 项目时间线

完整更新记录见 [Releases](https://github.com/V01d666/MoeGuard/releases)

<details open>
<summary><strong>V0.6</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `11-22` | 🐾 [**v0.6.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.6.0) | ✨ Windows 客户端迁移至 Tauri，重做桌宠工坊与桌面互动<br>✨ 双语标签式设置、桌宠快切、逐动作帧率<br>🔧 Windows 安装版可在设置中检查更新 |

</details>

<details>
<summary><strong>V0.5</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `11-14` | 🚀 [**v0.5.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.5.0) | ✨ 立绘与动作生成质量、速度升级<br>✨ 取消后保留候选，安装更顺畅；逐角色记忆播放帧率<br>🔧 改善窗口恢复、动作尺度与透明预览，修复 Windows 空闲内存持续增长 |

</details>

<details>
<summary><strong>V0.4</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `11-04` | 🚀 [**v0.4.2**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.2) | 🔧 修复首次启动快速多开时可能重复创建匿名账户<br>🔧 同一资料目录只保留一个实例；重试与续签沿用原身份 |
| `10-26` | 🚀 [**v0.4.1**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.1) | ✨ [萌卫官网](https://moeguard.com/)上线：戳一戳、拎起来、看桌宠探头<br>✨ 信箱提醒、角色改名、更顺手的设置 |
| `10-18` | 🚀 [**v0.4.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.0) | ✨ 服务连接与生成次数自动刷新，次数不足提示更清楚<br>✨ 升级后保留原支持编号 |

</details>

<details>
<summary><strong>V0.3</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `10-08` | 🚀 [**v0.3.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.3.0) | ✨ 安装版支持原地更新<br>✨ 内置信箱与支持编号，设置页可查看版本 |

</details>

<details>
<summary><strong>V0.2</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `09-24` | 🚀 [**v0.2.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0) | ✨ 桌宠工坊：生成进度、预计等待、完成提醒<br>✨ 角色导出与导入 |
| `09-01` | 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview) | 开启限量内测，详见[爱发电内测公告](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00) |
| `08-23` | 🎬 Demo | 首支实机演示：[文字生成桌宠](.github/assets/text2pet-demo.mp4)<br>[图片生成桌宠](.github/assets/image2pet-demo.mp4) |

</details>

<details>
<summary><strong>V0.1</strong></summary>

| 日期 | 版本 | 更新内容 |
| :--: | :-- | :-- |
| `08-18` | 🚀 [**v0.1.0**](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0) | ✨ 三位初始角色与桌面互动<br>✨ 锁屏看家 |

</details>

## 以后呢？

桌宠工坊让每个人都能做出喜欢的角色，但这只是第一步，接下来想让它们真正“活”过来：

- 🧠 **性格档案**：给角色写上脾气和小习惯，想改随时改
- 🗣️ **专属台词**：每个自制角色都有自己的口头禅
- 💬 **陪伴对话**：不再是戳一下说一句，而是真的能和你聊上几句
- 🎙️ **开口说话**：台词和对话配上专属声线，不再只是冒个气泡
- 🌱 **越陪越懂**：养成系统上线，陪得越久它越懂你，记忆可不只有七秒哦
- 🎁 **一键分享**：把角色打包成一个文件，发给朋友直接导入
- 📡 **远程告警**：看家时一有动静，第一时间推送到你的手机，出门在外也安心

饼先画在这里，但只有真正做好、测过、能下载体验的功能才会搬进“现在的萌卫”。进度会在 GitHub、[官网](https://moeguard.com/)和[爱发电](https://ifdian.net/a/moeguard)同步更新

<details>
<summary>🔒 摄像头与数据</summary>

- **看家画面**：值守、预览摄像头或注册主人时会使用摄像头。人脸特征、画面和证据都只留在本机，值守证据默认保留 7 天。
- **云端生成**：只有你主动生成时才会上传文字和图片，暂存在服务端，按活动批次清理
- **匿名统计**：只记录少量使用事件、在线心跳和网络快慢，不含提示词、图片、角色名、本机路径或值守数据
- **撤回同意**：从托盘「停止值守」后，可在「设置 → 值守」选择「撤回值守同意…」，删除本机的主人特征、证据和相关记录。

客户端代码全部开源，上面每一条都可以对照源码核实。请在本人设备及他人知情的私人空间使用；识别可能漏检，值守也可能因遮挡、摄像头占用或系统睡眠中断，萌卫不能替代专业安防设备

</details>

## 反馈与开发

有 bug 或者新点子，右键点击系统托盘中的萌卫图标，在弹出的菜单里选择「萌卫信箱」。也可以打开「桌宠工坊 → 使用教程」，点顶部反馈提示的「这里」。写好留言后，依次点「发送」和「确认发送」。需要复制客户端 ID 时，打开「设置 → 关于」，在「关于与反馈」中点「复制」。也欢迎来 [GitHub Issues](https://github.com/V01d666/MoeGuard/issues) 聊聊。公开贴日志前，请先移除客户端 ID、用户名、本机路径、照片、视频、主人特征和 API 密钥。

萌卫是一个下班后慢慢养大的兴趣项目，没法承诺企业级的响应速度、全平台兼容或固定的更新节奏。如果它让你会心一笑，欢迎点个 ⭐ Star，或者去[爱发电](https://ifdian.net/a/moeguard)支持一下～

想参与开发的话，可用上面构建步骤创建的 `.venv`，在仓库根目录跑前端和 Python 检查。

```powershell
npm.cmd --prefix desktop ci
npm.cmd --prefix desktop test
.\.venv\Scripts\python.exe -m ruff check moeguard tests scripts/windows
New-Item -ItemType Directory -Path .pytest-tmp -Force | Out-Null
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

代码及随仓库发行的三位角色素材采用 [Apache License 2.0](LICENSE)；随包模型的简要声明见 [NOTICE](NOTICE)
