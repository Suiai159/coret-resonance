# CoreT Desktop — Windows 测试版

基于 `main` 的 `31f179c`，Windows 适配在独立的 `windows` 分支进行。
目标平台：Windows 10 / 11 **x64**。不支持 Windows 7 或 32 位系统。
本分支保留原采集协议、测量参数、质检配置和算法；不是另写一套 Windows 算法。

## 1. 直接测试 EXE（不需要安装 Python）

1. 将 `CoreT-Desktop-Windows-x64.zip` **完整解压**到本地目录。
2. 双击 `CoreT-Desktop/CoreT-Desktop.exe`。
3. 保持 EXE 旁的 `_internal` 文件夹完整；不能只复制 EXE。
4. 程序启动时**不会自动连接串口，也不会自动开始测量或驱动电磁阀**。

这是未签名的本地测试包，尚不是带签名的正式安装程序。请只运行你自己构建或确认来源的包。
如果 Windows 显示来源/信誉提示，应先核对包的来源；不需要关闭杀毒软件或以管理员运行。

### 数据和日志

Windows 默认将设置、默认会话和诊断日志写到当前用户的：

```text
%LOCALAPPDATA%\CoreT\CoreT Desktop\
```

程序目录无需可写，也不依赖当前工作目录。可通过命令行指定独立测试目录：

```powershell
& '.\CoreT-Desktop.exe' --data-dir 'D:\CoreT 测试数据'
& '.\CoreT-Desktop.exe' --open 'D:\历史数据\测试.coret'
```

软件内仍可通过保存/打开对话框选择其他目录。Linux 的 `.coret` 文件可直接打开，
新保存的文件继续使用原格式。旧 Linux `user-data` 不会自动搬迁，请手工打开其中的会话。
诊断日志为 `runtime.log`，最多保留当前文件和两个轮转备份，各约 2 MiB。

## 2. 建议的人工验收顺序

### 不接硬件

- 切换到“演示 · PC 正弦模拟”，连接后分别试 5 / 10 / 15 / 20 / 30 秒采集。
- 测试提前停止、重复采集、保存会话、导出 CSV 和 PNG，以及再次打开 `.coret` 回放。
- 用包含中文和空格的目录保存和读取。
- 试 F11 / Esc、最大化、设置与日志面板，在系统 100% / 125% / 150% 缩放下查看布局。
- 用已有真实会话检查离线质检结果。演示正弦波只验证采集链路，不表示真实质检合格。

### 接 ESP32-S3

1. 先确认设备烧录的是与当前上位机配套的 0.3 联动固件。
2. 使用 USB 数据口，避免选择 UART 日志口；在设备管理器中确认设备正常枚举为 COM 端口。
3. 软件仅展示识别为 ESP32-S3 原生 USB（VID `303A` / PID `1001`）的数据口，
   COM10 及以上也支持。点击刷新后再连接，等待同步完成。
4. 保持设备、电磁阀和机械动作处于安全状态，再人工触发测量。
5. 验证各时长、各阀频率、提前停止、拔线、重新连接、关闭软件；确认停止和故障时关阀。
6. 按 F7 验证质检固定配置及连续 10 秒规则，具体见 `QC-使用说明.txt`。

若连接失败：先关闭占用同一 COM 口的串口助手/烧录器；检查数据线、固件、设备管理器和端口编号。
不要为了一个未识别设备盲目安装第三方驱动。自动验证只使用模拟器和合成离线数据，
**不能替代真实 USB 通信、持续吞吐量、拔插和电磁阀动作的硬件验收**。

## 3. 从源码运行 / 重新打包

使用 **64 位 Python 3.12**（依赖固定版本也兼容 3.10 / 3.11）。
默认通过 `py -3.12` 寻找解释器；没有 `py` 时使用 PATH 中的 `python`。
安装依赖需要网络，但只写入项目自己的 `.venv`，不会安装到全局 Python。

```powershell
.\setup-windows.bat
.\start-source.bat
.\build-windows.bat
```

也可以显式指定 Python，无需激活虚拟环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Python 'C:\Python312\python.exe'
```

`ExecutionPolicy Bypass` 仅针对这次脚本进程，不会修改系统执行策略。
打包必须在 Windows x64 上执行，使用 PyInstaller 的 **onedir/windowed** 模式；
不会打入 Linux 的 `libxcb-cursor.so.0`。运行包包含配置 JSON、SVG、Qt 插件、NumPy/SciPy、
第三方许可和 `source/` 重建源码。实际环境版本记录在包内 `build-environment.txt`。

输出：

```text
dist/CoreT-Desktop/CoreT-Desktop.exe
dist/CoreT-Desktop-Windows-x64.zip
```

## 4. 自动验证（不接物理设备）

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --smoke-test outputs/source-smoke
.\.venv\Scripts\python.exe tools/check_windows_package.py --output 'outputs/Windows 验证'
```

包验证会复制整个运行包到中文/空格路径，从非程序工作目录运行 EXE；
验证全部时长的模拟采集、保存回放、界面快捷键，以及合成合格/中断数据的离线质检。
输出目录必须是新的，避免旧结果被误认为本次结果。截图和 JSON 证据保存在 `outputs/`。

GitHub Actions 工作流 `.github/workflows/windows.yml` 执行同类检查并上传 ZIP 和验证证据。
该工作流需将分支推送到 GitHub 后才会运行。

## 5. 本次适配范围

- 新增 Windows 环境安装、源码启动、EXE/ZIP 打包及 CI。
- Windows 使用用户本地数据目录，避免 Program Files 或只读目录写入失败。
- JSON、配置和验证报告显式使用 UTF-8；支持中文/空格路径。
- 界面与波形字体增加 Windows 字体和系统字体回退。
- Windows 串口不传 POSIX `exclusive` 参数；打开失败释放句柄；保留 DTR/RTS 预设置与 STOP 同步。
- 新增滚动诊断日志，便于测试时提供异常信息。
- 保留 Linux 启动/打包脚本和 Linux 默认数据目录，不直接修改 `main` 分支。
