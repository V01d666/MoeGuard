<p align="right">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="https://moeproject.net">
    <img src="https://img.shields.io/badge/%E5%AE%98%E7%BD%91-moeproject.net-1E8BFF?style=for-the-badge" alt="官网 · moeproject.net">
  </a>
  <a href="https://ifdian.net/a/moeguard">
    <img src="https://img.shields.io/badge/%E7%88%B1%E5%8F%91%E7%94%B5-%E6%94%AF%E6%8C%81%E8%90%8C%E5%8D%AB-946CE6?style=for-the-badge" alt="爱发电 · 支持萌卫">
  </a>
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

那就先去[萌卫官网](https://moeproject.net)和它玩一会儿，喜欢再[领回家](#马上开玩)吧！

写一段描述或者丢一张图，就能做出只属于你的桌宠，连动作都能自己生成；看腻了，随时给它换个造型

平时它趴在屏幕边上陪你摸鱼，戳一戳、拎起来都有反应；等你锁屏离开，它会在你明确同意后打开摄像头替你看家，你一回来就乖乖关掉，再告诉你刚才发生了什么。谁说桌宠只会吃白饭？(•̀ᴗ•́)و

它不是披着桌宠外衣的安防设备，更像一位偶尔认真过头的小搭档：平时陪你，必要时顺手看家

## 现在的萌卫

- 🎨 **桌宠工坊**：一段文字或一张图就能生成立绘，选中心仪的那张再让它动起来；除了待机，想要哪些动作随你挑
- 🔁 **常换常新**：装好的角色随时补动作、换动作、改名字，甚至整个重新设计，旧版本都会留着
- ⏳ **不用干等**：进度和预计时间一目了然，走开也没关系，做好了托盘和桌宠都会喊你回来
- 📦 **随身带走**：角色能导出成文件夹备份，换台电脑也能原样导入
- 🐾 **开箱即玩**：Lumen、Poppy、Rook 三位角色随时待命，拎起来、拖着走都有反应，还会欢迎你回来、扒着屏幕边缘探头
- 📬 **消息不漏**：公告和作者回复会在右下角提醒你，托盘里点「信箱…」就能看
- 🛡️ **顺手看家**：手动开启，或授权后随锁屏自动上岗；有陌生人或异动就在本地留下截图和短视频，托盘、右键、老板键一键叫停，出了状况会老实停下，绝不假装还在站岗

## 马上开玩

下载 **Windows x64 安装版**，双击安装就能开玩，不用装 Python

有新版本时萌卫会提醒你，点「更新」就好，什么都不用重新下载；角色、设置、额度和工坊记录都会保留

| 🌐 官网直链 | 🐙 GitHub Releases |
| :---: | :---: |
| [官网安装包直链](https://api.moeproject.net/updates/windows/stable/MoeGuard-Setup.exe) | [GitHub 安装版](https://github.com/V01d666/MoeGuard/releases/latest/download/MoeGuard-Setup.exe) |

只养桌宠用不到摄像头；第一次开启看家时，萌卫会先讲清楚风险、征得你的同意

<details>
<summary>🔎 文件校验（v0.4.2）</summary>

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MoeGuard-Setup.exe` | 206874814 | `b37c11a1672f54f792b8954e5468b56cce88322f311f5df63eea823f02d0b866` |

</details>

<details>
<summary>🛠️ 从源码运行</summary>

需要 **64 位 Windows 10/11 与 Python 3.12**：

```powershell
git clone https://github.com/V01d666/MoeGuard.git
cd MoeGuard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start_moeguard.bat
```

</details>

### 🎟️ 想试试桌宠工坊？

萌卫本体免费，自带角色和看家功能都可以直接用。桌宠工坊在云端生成，按“立绘生成”和“动作生成”分别计算额度：新用户第一次打开就会获赠一份体验额度，够做一只完整的桌宠；用完之后可以用兑换码补充，兑换码在[爱发电](https://ifdian.net/a/moeguard)获取，到“设置 → 通用 → 查看次数 / 兑换码…”里兑换就好

## 项目时间线

<details>
<summary><strong>v0.4 · 连接与日常体验</strong></summary>

- [v0.4.2](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.2)：修复首次启动快速多开可能重复创建匿名账户的问题；同一用户资料目录只保留一个应用实例，连接重试与续签继续复用原身份
- [2026/10/26] 🌐 [v0.4.1](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.1)：[萌卫官网](https://moeproject.net)上线，可以在网页里戳一戳、拎起来、看桌宠探头；客户端带来信箱提醒、角色改名和更顺手的设置
- [2026/10/18] 🎉 [v0.4.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.4.0)：服务连接与生成次数自动刷新，次数不足提示更清楚，升级后保留原支持编号

</details>

<details>
<summary><strong>v0.3 · 安装版更新与信箱</strong></summary>

- [2026/10/08] 🎉 [v0.3.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.3.0)：安装版支持原地更新，新增内置信箱与支持编号，设置页也能查看当前版本

</details>

<details>
<summary><strong>v0.2 · 桌宠工坊</strong></summary>

- [2026/09/24] 🎉 [v0.2.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0)：桌宠工坊正式登场，带来生成进度与预计等待、完成提醒，以及角色导出和导入
- [2026/09/01] 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview)：开启限量内测，详见[爱发电内测公告](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00)
- [2026/08/23] 🎬 首支实机 Demo：[文字生成桌宠](.github/assets/text2pet-demo.mp4)与[图片生成桌宠](.github/assets/image2pet-demo.mp4)

</details>

<details>
<summary><strong>v0.1 · 三位初始桌宠与看家</strong></summary>

- [2026/08/18] 🚀 [v0.1.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0)：三位初始角色、桌面互动和锁屏看家一起登场

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

饼先画在这里，但只有真正做好、测过、能下载体验的功能才会搬进“现在的萌卫”。进度会在 GitHub、[官网](https://moeproject.net)和[爱发电](https://ifdian.net/a/moeguard)同步更新

<details>
<summary>🔒 摄像头与数据</summary>

- **看家画面**：摄像头只在主人注册和值守时打开，人脸特征、画面和证据都只留在本机，值守证据默认保留 7 天
- **云端生成**：只有你主动生成时才会上传文字和图片，暂存在服务端，按活动批次清理
- **匿名统计**：只记录少量使用事件、在线心跳和网络快慢，不含提示词、图片、角色名、本机路径或值守数据
- **随时退出**：撤回值守同意会停止采集并删除主人特征和证据；想彻底清除，退出后删掉 `%USERPROFILE%\.moeguard\` 即可

客户端代码全部开源，上面每一条都可以对照源码核实。请在本人设备及他人知情的私人空间使用；识别可能漏检，值守也可能因遮挡、摄像头占用或系统睡眠中断，萌卫不能替代专业安防设备

</details>

## 反馈与开发

有 bug 或者新点子，可以在“设置 → 通用 → 关于与反馈”里打开信箱留言、复制支持编号，也欢迎来 [GitHub Issues](https://github.com/V01d666/MoeGuard/issues) 聊聊！贴日志前请先删掉用户名、本机路径、照片、视频、主人特征和 API 密钥，**不要上传整个 `.moeguard`、`evidence` 或 `owner` 目录**

萌卫是一个下班后慢慢养大的兴趣项目，没法承诺企业级的响应速度、全平台兼容或固定的更新节奏。如果它让你会心一笑，欢迎点个 ⭐ Star，或者去[爱发电](https://ifdian.net/a/moeguard)支持一下～

想参与开发的话，可以先跑一遍：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

代码及随仓库发行的三位角色素材采用 [Apache License 2.0](LICENSE)；随包模型的简要声明见 [NOTICE](NOTICE)
