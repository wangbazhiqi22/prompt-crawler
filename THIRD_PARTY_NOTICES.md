# 第三方组件

本仓库的 MIT 许可仅适用于本项目代码，不替代第三方组件或模型各自的许可。

- Phosphor Icons：MIT，完整许可见 [图标许可证](static/bubble/icons/LICENSE.txt)。
- Flask、Waitress、Playwright、Requests、Beautiful Soup、OpenPyXL、python-docx、OpenCC、Pillow、RapidOCR、ONNX Runtime、faster-whisper、yt-dlp：通过 pip 安装，许可随各自软件包分发。
- FFmpeg：由使用者独立安装，适用许可取决于所选构建及启用的组件；本仓库不打包二进制。
- llama.cpp：MIT；可选安装器从官方 GitHub Releases 下载运行时。
- Qwen3.5-2B GGUF 与多模态投影：可选安装器从 `unsloth/Qwen3.5-2B-GGUF` 下载；请查阅该模型仓库及原始模型的许可证、模型卡。
- faster-whisper 首次识别时下载所选 Whisper 模型；模型及其许可由上游提供。

本仓库不包含用户下载的第三方视频、图片、音频、账号登录状态或私人数据库。界面的背景由项目内的 WebGL 程序渲染。
