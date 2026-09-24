<p align="right">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="https://ifdian.net/a/moeguard">
    <img src="https://img.shields.io/badge/%E7%88%B1%E5%8F%91%E7%94%B5-%E6%94%AF%E6%8C%81%E8%90%8C%E5%8D%AB-946CE6?style=for-the-badge" alt="爱发电 · 支持萌卫">
  </a>
</p>

# 萌卫 MoeGuard

> 一只能随心定制、陪你一起进步，还恰好能看家的二次元桌宠 (｡•̀ᴗ-)✧

[![Release](https://img.shields.io/github/v/release/V01d666/MoeGuard?label=release&color=946CE6)](https://github.com/V01d666/MoeGuard/releases)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](https://github.com/V01d666/MoeGuard/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<p align="center">
  <img src=".github/assets/cover.png" alt="萌卫 MoeGuard 封面" width="100%">
</p>

- 想要一只桌宠，却总找不到真正心动的那一只？
- 再可爱的桌宠，天天看也难免审美疲劳？
- 想亲手做一只，却被建模和动画劝退？
- 总觉得桌宠只会卖萌，派不上什么用场？

那就来[试试萌卫](https://github.com/V01d666/MoeGuard/releases)吧！

写一段描述或者丢一张图，就能做出只属于你的桌宠，连动作都能自己生成；看腻了，随时给它换个造型。

平时它趴在屏幕边上陪你摸鱼，戳一戳、拎起来都有反应；等你锁屏离开，它会在你明确同意后打开摄像头替你看家，你一回来就乖乖关掉，再告诉你刚才发生了什么。谁说桌宠只会吃白饭？(•̀ᴗ•́)و

它不是披着桌宠外衣的安防设备，更像一位偶尔认真过头的小搭档：平时陪你，必要时顺手看家。

<p align="center">
  <img src=".github/assets/baseCharas.png" alt="初始三角色：Lumen、Poppy、Rook" width="100%">
  <br>
  <sub>初始三角色：<a href="resources/roles/lumen/idle/0001.png">Lumen</a> · <a href="resources/roles/poppy/idle/0001.png">Poppy</a> · <a href="resources/roles/rook/idle/0001.png">Rook</a></sub>
</p>

## 现在的萌卫

- 🎨 **桌宠工坊**：一段文字或一张图就能生成立绘，选中心仪的那张再让它动起来；除了待机，想要哪些动作随你挑
- 🔁 **常换常新**：装好的角色随时补动作、换动作，甚至整个重新设计，旧版本都会留着
- ⏳ **不用干等**：进度和预计时间一目了然，走开也没关系，做好了托盘和桌宠都会喊你回来
- 📦 **随身带走**：角色能导出成文件夹备份，换台电脑也能原样导入
- 🐾 **开箱即玩**：Lumen、Poppy、Rook 三位角色随时待命，拎起来、拖着走都有反应，还会欢迎你回来、扒着屏幕边缘探头
- 🛡️ **顺手看家**：手动开启，或授权后随锁屏自动上岗；有陌生人或异动就在本地留下截图和短视频，托盘、右键、老板键一键叫停，出了状况会老实停下，绝不假装还在站岗

## 马上开玩

到 [GitHub Releases](https://github.com/V01d666/MoeGuard/releases) 下载 Windows x64 ZIP，完整解压后双击 `MoeGuard.exe` 就能开玩，不用装 Python。只养桌宠用不到摄像头，开启看家时才需要。

第一次启动会先讲清楚值守的风险；你可以只养桌宠，看家功能随时再到设置里开启。

<details>
<summary>想从源码运行？</summary>

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

自带角色和看家功能都不需要兑换码。桌宠工坊在云端生成，按“立绘生成”和“动作生成”分别计次，次数目前只能通过兑换码获得，暂未开放购买。兑换码会不定期在[爱发电主页](https://ifdian.net/a/moeguard)随活动发放，记得常来蹲一蹲～拿到码后，到“设置 → 查看次数 / 兑换码…”里兑换就好。

## 项目时间线

- [2026/09/24] 🎉 [MoeGuard v0.2.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0) 正式发布！桌宠工坊总算是能上桌了，还带来了生成进度与预计等待、完成提醒，以及角色导出和导入
- [2026/09/01] 🧪 [v0.2.0 Preview](https://github.com/V01d666/MoeGuard/releases/tag/v0.2.0-preview) 开启限量内测，桌宠工坊第一次交到大家手上，详见[爱发电内测公告](https://ifdian.net/p/d2c072d4a61d11f1bd1c5254001e7c00)
- [2026/08/23] 🎬 桌宠工坊首支实机 Demo 出炉，来看看[文字生成桌宠](.github/assets/text2pet-demo.mp4)和[图片生成桌宠](.github/assets/image2pet-demo.mp4)的完整流程
- [2026/08/18] 🚀 [MoeGuard v0.1.0](https://github.com/V01d666/MoeGuard/releases/tag/v0.1.0) 正式发布，三位初始角色、桌面互动和锁屏看家一起登场

## 以后呢？

桌宠工坊让每个人都能做出喜欢的角色，但这只是第一步，接下来想让它们真正“活”过来：

- 🧠 **性格档案**：给角色写上脾气和小习惯，想改随时改
- 🗣️ **专属台词**：每个自制角色都有自己的口头禅
- 💬 **陪伴对话**：不再是戳一下说一句，而是真的能和你聊上几句
- 🎙️ **开口说话**：台词和对话配上专属声线，不再只是冒个气泡
- 🌱 **越陪越懂**：养成系统上线，陪得越久它越懂你，记忆可不只有七秒哦
- 🎁 **一键分享**：把角色打包成一个文件，发给朋友直接导入
- 📡 **远程告警**：看家时一有动静，第一时间推送到你的手机，出门在外也安心

饼先画在这里，但只有真正做好、测过、能下载体验的功能才会搬进“现在的萌卫”。进度会在 GitHub 和[爱发电](https://ifdian.net/a/moeguard)同步更新。

## 关于摄像头、云端生成和你的数据

- 摄像头只在注册主人和值守时打开，平时切角色、改设置都不会碰它
- 值守画面、人脸特征和证据全部留在本机，没有云端人脸识别
- 只有你主动在桌宠工坊生成时，对应的文字、参考图和任务素材才会上传；为了排查生成质量和任务中断，这些内容会暂存在服务端，并按活动批次清理
- 桌宠工坊还会上报少量使用事件（界面、步骤、耗时、错误码、任务编号）帮忙排查问题，但不含提示词、图片、本机路径或值守数据
- 主人特征存在 `%USERPROFILE%\.moeguard\owner\`，证据存在 `%USERPROFILE%\.moeguard\evidence\`，值守事件默认保留 7 天
- 在设置中撤回值守同意，会停止采集并删除主人特征和证据，删不掉会明确告诉你；想彻底清除，就退出萌卫后删掉整个 `%USERPROFILE%\.moeguard\`
- 可选“模糊陌生人脸”：找不到人脸就整帧降低分辨率，检测器不可用就干脆不存，但仍可能有漏网之鱼
- 逆光、遮挡、快速经过、摄像头被占用，或合盖、睡眠、省电都可能导致漏检或中断，请只在自己的设备和知情的私人空间使用

萌卫会认真帮忙，但它不是专业安防设备，不能替代系统锁屏或监控产品，也不保证识别准确、值守永不中断。开源的意义，是让你亲眼确认摄像头何时打开、数据存在哪里、出错时怎么停、不想留了怎么删。

## 反馈与开发

有 bug 或者新点子，都欢迎来 [GitHub Issues](https://github.com/V01d666/MoeGuard/issues) 聊聊！贴日志前请先删掉用户名、本机路径、照片、视频、主人特征和 API 密钥，**不要上传整个 `.moeguard`、`evidence` 或 `owner` 目录**。

萌卫是一个下班后慢慢养大的兴趣项目，没法承诺企业级的响应速度、全平台兼容或固定的更新节奏。如果它让你会心一笑，欢迎点个 ⭐ Star，或者去[爱发电](https://ifdian.net/a/moeguard)支持一下～

想参与开发的话，可以先跑一遍：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

代码及随仓库发行的三位角色素材采用 [Apache License 2.0](LICENSE)；随包模型的简要声明见 [NOTICE](NOTICE)。
