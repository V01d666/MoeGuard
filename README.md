<p align="center">
  <a href="https://ifdian.net/a/moeguard">
    <img src="https://img.shields.io/badge/%E7%88%B1%E5%8F%91%E7%94%B5-%E6%94%AF%E6%8C%81%E8%90%8C%E5%8D%AB-946CE6?style=for-the-badge" alt="爱发电 · 支持萌卫">
  </a>
</p>

# 萌卫 MoeGuard

> 一只恰好会看家的二次元桌宠。

[![Status](https://img.shields.io/badge/status-experimental%20pre--alpha-orange)](.)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](.)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<p align="center">
  <img src="resources/roles/lumen/idle/0001.png" alt="Lumen" width="180">
  <img src="resources/roles/poppy/idle/0001.png" alt="Poppy" width="180">
  <img src="resources/roles/rook/idle/0001.png" alt="Rook" width="180">
  <br>
  <sub>Lumen · Poppy · Rook</sub>
</p>

萌卫的想法很简单：让桌面上住着一位小小的搭档。

你忙碌时，它安静地待在一旁；你戳戳它、拎起它，或者把它推到屏幕边缘，它也会有自己的反应。等你锁屏离开，它还能在你明确同意后打开摄像头，替你留意一会儿周围的动静。回来时，摄像头会关闭，桌宠会恢复陪伴，再告诉你刚才有没有发生什么。

所以它并不是披着桌宠外衣的监控软件——更像一位偶尔认真过头的小搭档：平时负责陪你，必要时顺手看家。

## 现在的萌卫

- 自带 Lumen、Poppy、Rook 三位角色，可以随时切换；动作和点击台词各有一点小脾气。
- 支持待机、点击、抓起、拖动、欢迎、值守，以及在桌面四边吸附和探头。
- 支持手动值守，也可以在单独授权后随 Windows 锁屏自动开始。
- 陌生人或画面运动可触发本地截图和短视频，并在证据管理器中查看、删除。
- 托盘、右键菜单和老板键都能立即停止值守；没有主人资料、摄像头不可用或读取异常时会退出值守，不会假装自己仍在工作。

项目目前仍是 **experimental pre-alpha**。它可以认真帮忙，但不是专业安防设备，也不能替代系统锁屏或监控产品。

## 以后呢？

希望将来的萌卫不只是“从几只桌宠里选一只”，而是能让你用文字或一张图片做出喜欢的角色，再为它慢慢添上性格、陪伴对话、养成和更多互动。<sup>*</sup>

<sub>* 这些是[爱发电共创支持版](https://ifdian.net/a/moeguard)的规划方向，尚未上线或出售，不是当前 GitHub 基础版已经交付的功能。只有真正做成、测试过并能下载的内容，才会成为支持版权益。</sub>

## 在 Windows 上运行

当前 GitHub 基础版提供免费源码，适合自行运行和审查本地安防逻辑；需要 **64 位 Windows 10/11 与 Python 3.12**。只想养桌宠不需要摄像头，启用值守时才会用到。

```powershell
git clone https://github.com/V01d666/MoeGuard.git
cd MoeGuard
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start_moeguard.bat
```

首次启动会先说明值守风险。你可以只使用桌宠，也可以稍后再从设置中注册主人、授权锁屏值守。

未来若上架爱发电共创支持版，会提供普通用户可直接双击的 `MoeGuard.exe`，不要求安装 Python、输入命令或配置开发环境；在做到这一点之前，不会拿源码压缩包冒充成品。

## 关于摄像头和你的数据

- 打开设置、切换角色或修改普通选项不会探测摄像头；只有主人注册或开始值守时才会尝试打开设备。
- 摄像头只在值守时持续使用，画面、人脸特征和证据都留在本机；基础版没有云端人脸识别。
- 主人特征保存在 `%USERPROFILE%\.moeguard\owner\`，证据保存在 `%USERPROFILE%\.moeguard\evidence\`，事件默认保留 7 天。
- 在设置中撤回值守同意，会停止采集并删除主人特征、证据和相关记录；若文件被占用或权限不足，萌卫会明确提示未完成并保留重试入口，不会把失败显示成成功。若要完整清除，退出萌卫后再删除整个 `%USERPROFILE%\.moeguard\` 目录。
- 可选的“模糊陌生人脸”会模糊检测到的人脸；检测不到有效人脸区域时改为整帧低分辨率保护，检测器不可用时拒绝保存该次原始画面。它仍不能消除所有漏检风险。
- 逆光、遮挡、快速经过、摄像头占用，以及合盖、睡眠或系统省电都可能造成漏检或中断。建议只在本人设备和已知情的私人空间使用。

开源的意义，是让你能够亲自确认摄像头何时开启、数据写到哪里、出错时怎样停下，以及东西不想留了该怎么删；它不是准确率或持续值守的保证。

## 反馈与开发

欢迎通过 GitHub Issues 报告问题或聊聊想法。涉及日志时，请先删掉用户名、本机路径、照片、视频、主人特征和 API 密钥；**不要上传整个 `.moeguard`、`evidence` 或 `owner` 目录**。

这是一个下班后慢慢养大的兴趣项目，不承诺企业级响应速度、全平台兼容或固定更新频率。想参与开发的话，可以先运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/local
```

代码及随仓库发行的三位角色素材采用 [Apache License 2.0](LICENSE)；随包模型的简要声明见 [NOTICE](NOTICE)。
