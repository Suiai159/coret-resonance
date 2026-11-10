# CoreT Resonance

CoreT 桌面采集与核心件质检软件。当前 Windows 正式版为 **v0.3.1**，在 `windows` 分支维护；
`main` 保留原 Linux 基线。

当前分支 `windows-learning` 为 **0.3.2-learning-dev1 学习功能测试版**，
合并 Linux 的 `feature/qc-reference-learning` 与已验证的 Windows 适配，不替换 GitHub 上的 v0.3.1 正式版。
学习操作和样本要求见 `README-Windows.md` 第 6 节。

## Windows

从本仓库的 GitHub Releases 下载 `CoreT-Desktop-Windows-x64.zip` 与 `SHA256SUMS.txt`。
完整解压后运行 `CoreT-Desktop/CoreT-Desktop.exe`，无需安装 Python，必须保留 `_internal` 文件夹。

- 运行及构建说明：`README-Windows.md`
- 本次变更及发布边界：`RELEASE-NOTES.md`
- 质检规则：`QC-使用说明.txt`
- 第三方声明及许可证：`THIRD-PARTY-NOTICES.txt`、`licenses/`

软件启动不会自动连接设备或开始测量。正式版 ZIP 未做代码签名；
算法既有的试运行提示、参数和使用边界保持不变。

## 开发

Windows 使用 Python 3.12 x64，先运行 `setup-windows.bat`，再运行 `start-source.bat`。
通过 `build-windows.bat` 重建，输出仍覆盖 `dist/` 中的固定发布位置。

Linux 原启动脚本为 `start-source.sh`，原打包脚本为 `build-linux.sh`；Windows Release 不提供 Linux 二进制。
