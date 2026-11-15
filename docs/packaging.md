# 打包说明：免 Python 的 Windows x64 发行版

> 文档更新时间：2026-11-13
> 构建脚本：[build/build.ps1](../build/build.ps1)　打包配置：[build/DouyinRecorder.spec](../build/DouyinRecorder.spec)
> 本文档会随发行包一起复制到 `dist/DouyinRecorder/docs/packaging.md`。

---

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 打包工具 | PyInstaller **6.22.3**（要求 >= 6.11 才支持 Python 3.13），依赖解包在项目内 `.pylibs`，不改全局环境 |
| 打包方式 | **onedir**（目录形式，非 onefile）：启动快，87MB 的 ffmpeg 不需要每次解压 |
| 入口 | `run.py`，主程序 `DouyinRecorder.exe` 为窗口模式（`--windowed`）；另附控制台入口 `DouyinRecorder-CLI.exe` |
| 目标平台 | Windows 10 / 11 **x64**（PE32+ x86-64），自带 Python 3.13.14 运行时、tkinter/Tcl/Tk 与 VC 运行库 |
| 产物目录 | `dist/DouyinRecorder/`（约 1000 个文件，**115.4 MB**，其中 ffmpeg 83.6 MB） |
| 压缩包 | `dist/DouyinRecorder_win64.zip`（**45.6 MB**） |
| 免 Python | 是，双击即用，目标机器无需安装 Python / ffmpeg / VC++ 运行库 |
| 命令行 | `--check` / `--list` / `--add` / `--once` / `--cli`，已在打包产物上实测 |
| GUI | 窗口标题「抖音直播录制工具 · 本地版」（品牌「本地版 v2.0」），双击后约 **0.3~0.8 秒**出现主窗口，已实测截图 |
| 关键验证 | 打包后程序根目录 = **exe 所在目录**（不是 `_MEIPASS` / `_internal`），config/watchlist/records/logs/data/ffmpeg 全部落在 exe 目录 |

---

## 2. 构建步骤（可复现）

### 2.1 构建机环境

| 项目 | 版本 |
| --- | --- |
| 操作系统 | Windows 11（`Windows-11-10.0.26300-SP0`），x64 |
| Python | 3.13.14（`MSC v.1944 64 bit AMD64`，安装在 `C:\Users\Admin\AppData\Local\Programs\Python\Python313`） |
| PyInstaller | 6.22.3 |
| pyinstaller-hooks-contrib | 2026.10 |
| altgraph | 0.17.5 |
| packaging | 26.3 |
| pefile | 2024.8.26 |
| pywin32-ctypes | 0.2.3 |
| Pillow | 13.0.0（仅构建期画图标，不进入发行包） |
| ffmpeg | 2025-02-26-git-99e2af4e78-essentials_build-www.gyan.dev（项目 `bin/ffmpeg.exe`，87,697,408 字节） |

### 2.2 准备 PyInstaller 依赖（一次性，写项目内 `.pylibs`）

本机 `pip` 不可用（联网安装会因临时目录权限/TLS 失败），因此用项目自带的 `analysis/pip_fetch.py`
直接从 PyPI 下载 wheel 并解包到 `douyin-recorder/.pylibs`（**只影响本项目构建，不修改全局 Python**）：

```bat
cd /d D:\Code\DS-Workspace\douyin-recorder
python analysis\pip_fetch.py pyinstaller pyinstaller-hooks-contrib altgraph packaging pefile pywin32-ctypes
```

执行成功会看到 6 个 wheel 解包完成，`.pylibs/PyInstaller/__init__.py` 存在即可。

### 2.3 一键构建

```powershell
cd D:\Code\DS-Workspace\douyin-recorder
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

可选参数：

| 参数 | 作用 |
| --- | --- |
| `-SkipZip` | 只打包目录，不生成 zip（调试时快一点） |
| `-NoSmokeTest` | 跳过构建后的 `--check` 冒烟测试 |
| `-Python <路径>` | 指定 Python 解释器（默认 `python`） |

脚本会自动：检查 `.pylibs` 依赖与 PyInstaller 版本 → 生成 `config.json` 模板和图标 → 清理旧 `dist/`、`build/work/` →
调用 `python -m PyInstaller` 以 `build/DouyinRecorder.spec` 打包 → 用 CLI 版本跑 `--check` 冒烟测试 →
清理测试残留 → `Compress-Archive` 生成 zip，最后打印体积汇总。

### 2.4 手动构建（等价）

```bat
cd /d D:\Code\DS-Workspace\douyin-recorder
python build\make_assets.py
set PYTHONPATH=%CD%\.pylibs
set DYR_PROJECT_ROOT=%CD%
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work build\DouyinRecorder.spec
```

> 注意：`build/build.ps1` 保存为 **UTF-8 with BOM**。Windows PowerShell 5.1 对无 BOM 的 UTF-8 脚本会按
> ANSI/GBK 解析，脚本里的中文会让引号错位、直接语法报错。修改脚本后请保持 BOM。

---

## 3. 打包设计要点

### 3.1 onedir + `contents_directory="."`（重点）

选择 onedir 的原因：启动快（onefile 每次启动都要把 ~100MB 解压到临时目录，冷启动数秒），
并且 ffmpeg 这种 87MB 的二进制放在外部目录里，替换/升级都方便。

更关键的是**配置路径**：`src/config.py` 用 `Path(__file__).resolve().parent.parent` 计算项目根目录，
`src/ffmpeg_util.py`、`src/dy_api.py`、`src/mock_api.py` 同理。PyInstaller 6 默认 onedir 布局会把运行时文件
放在 `_internal/` 子目录，此时 `__file__` 指向 `...\DouyinRecorder\_internal\src\config.py`，
程序根目录会被错误解析成 `_internal`，后果是：

- `config.json`、`watchlist.json` 生成在 `_internal/`（用户找不到），
- `records/`、`logs/`、`data/` 落在 `_internal/`，
- `bin/ffmpeg.exe` 查找不到（它被放在 `_internal/bin/` 或解压根目录之外）。

解决办法是在 `build/DouyinRecorder.spec` 里给两个 EXE 都加 `contents_directory="."`（等价于 CLI 的
`--contents-directory .`），恢复 PyInstaller 5 及以前的“运行时文件与 exe 平铺”布局：

```
dist\DouyinRecorder\
├─ DouyinRecorder.exe        ← 双击这个
├─ DouyinRecorder-CLI.exe    ← 命令行用这个
├─ config.json               ← 可编辑配置（相对路径）
├─ bin\ffmpeg.exe           ← 自带 ffmpeg
├─ assets\                  ← 示例文件
├─ python313.dll / *.pyd / *.dll / _tcl_data / _tk_data / base_library.zip ...
└─ （首次运行后生成）watchlist.json / records/ / logs/ / data/
```

这样打包后程序内所有路径都指向 **exe 所在目录**，实现了绿色版：整个文件夹随便移动、拷到别的机器都可用。
（第 6 节实测 `--check` 打印的“项目根目录/配置文件/输出目录/ffmpeg 解析”都指向 `dist\DouyinRecorder\`。）

### 3.2 双入口：GUI + CLI

| exe | 子系统 | 用途 |
| --- | --- | --- |
| `DouyinRecorder.exe` | Windows GUI（`console=False`） | 双击启动 tkinter 图形界面，不弹黑框 |
| `DouyinRecorder-CLI.exe` | 控制台（`console=True`） | `--check/--once/--list/--add/--cli`，适合命令行与 Windows 计划任务 |

两者由同一份代码、同一次 Analysis 构建，只是 bootloader 子系统不同；`DouyinRecorder.exe` 从命令行启动时也能输出
（stdout 有效时），但 PowerShell/cmd 对 GUI 子系统程序默认**不等待**，拿不到退出码，所以自动化脚本请用 CLI 版本。

### 3.3 随包数据与运行时目录

| 内容 | 来源 | 打包位置 |
| --- | --- | --- |
| ffmpeg | `bin/ffmpeg.exe` | `bin/ffmpeg.exe`（exe 同级） |
| 示例文件 | `assets/` | `assets/` |
| 配置模板 | `build/make_assets.py` 由 `src/config.py` 的 `DEFAULTS` 生成 | 根目录 `config.json`（`output_dir`/`log_dir` 为相对路径） |
| 本文档 | `docs/packaging.md`（构建时若存在则收集） | `docs/packaging.md` |

`watchlist.json`、`records/`、`logs/`、`data/` 不预先打包，由程序首次运行/首次添加主播时在 exe 目录创建。
程序自动创建目录，所以**整个目录必须在可写位置**（例如 `D:\Tools\DouyinRecorder\`，不要放 `C:\Program Files`）。

### 3.4 窗口模式的 stdout 兜底（PyInstaller 坑）

PyInstaller 的窗口模式程序如果没有控制台（双击启动），`sys.stdout/sys.stderr` 会被置为 `None`，
此时任何 `print()` 都会抛 `AttributeError: NoneType object has no attribute write`。
`build/rthooks/rthook_stdout.py` 作为运行时钩子做三层兜底：复用继承的标准句柄（管道/重定向）→ `AttachConsole(-1)` + `CONOUT$` →
丢弃输出的空流，保证 GUI 不会被打印语句打挂，同时命令行启动窗口版时仍能看到输出。

### 3.5 图标

`build/make_assets.py` 用 Pillow 现场画一个简单图标（深色圆角底 + 红色录制圆 + 播放三角，256/128/64/48/32/24/16 多尺寸），
保存为 `build/app.ico` 供 PyInstaller 嵌入。**没有**复制逆向目标（原商业软件）的图标，避免版权/商标问题。

---

## 4. 产物结构与体积

### 4.1 目录结构（`dist/DouyinRecorder/`）

```
DouyinRecorder\
├─ DouyinRecorder.exe            2.71 MB   GUI 主程序（--windowed）
├─ DouyinRecorder-CLI.exe        2.71 MB   控制台入口（--console）
├─ config.json                   1.6 KB    配置模板（output_dir=records、log_dir=logs 相对路径）
├─ bin\ffmpeg.exe               83.6 MB   自带 ffmpeg（可直接替换）
├─ assets\                                  示例分享文案 / assets/README.md
├─ python313.dll                 5.85 MB   Python 3.13 运行时
├─ base_library.zip              1.34 MB   Python 标准库
├─ *.pyd / *.dll                           扩展模块与 VC 运行库（_socket、_ssl、_sqlite3、libssl、ucrtbase、VCRUNTIME140 等）
├─ tcl86t.dll / tk86t.dll                  Tcl/Tk 运行时
├─ tcl8\ / _tcl_data\ / _tk_data\        tkinter 所需 Tcl/Tk 数据（约 4 MB，832 + 89 个文件）
└─ docs\packaging.md                       本文档（可选）
```

### 4.2 体积

| 项目 | 大小 | 说明 |
| --- | --- | --- |
| `dist/DouyinRecorder/` 目录 | **115.4 MB**（120,996,183 字节） | 约 1000 个文件 |
| `dist/DouyinRecorder_win64.zip` | **45.6 MB** | 整体压缩，ffmpeg 可压到约一半 |
| 其中 ffmpeg | 83.6 MB | 占目录体积 72%；如果只是自用、机器已有 ffmpeg，可删除 `bin/ffmpeg.exe` 并在 config 里指定 `ffmpeg_path` |
| Python + 标准库 | 约 10 MB | `python313.dll`、`*.pyd`、`base_library.zip` |
| tkinter / Tcl/Tk | 约 7 MB | 保证 GUI 免安装可用 |
| 两个 exe | 各 2.71 MB | 各自内嵌 PYZ（同一份代码，方便独立运行） |

---

## 5. 使用说明（Windows 10 / 11 x64）

### 5.1 第一次运行

1. 把 `DouyinRecorder_win64.zip` 解压到**可写目录**，例如 `D:\Tools\DouyinRecorder\`；
2. 双击 `DouyinRecorder.exe`：首次可能被 SmartScreen / 杀软拦一下（见 5.2、5.3），放行后进入图形界面；
3. 在左侧「监控」页点「+ 添加」（或「批量导入」），粘贴直播间链接/房间号/分享文案，名称可留空自动补全；
4. 点「开始监控」（或对单个主播「批量录制」）。

首次运行/添加主播后，exe 目录会出现：

- `watchlist.json` 主播列表；
- `records\` 录像输出（默认 `records/{anchor}/{date}/主播_日期_时间.flv`，录完默认 remux 成 `.mp4`）；
- `logs\` 日志（UTF-8，按天/按 5MB 轮转，默认保留 30 天）；
- `data\` 协议层 cookie 缓存与历史数据库（可删，会自动重建）。

### 5.2 SmartScreen 提示（未签名程序的正常现象）

双击后可能出现蓝色窗口「Windows 已保护你的电脑」（SmartScreen），因为 exe **没有代码签名证书**：

- 点「更多信息」→「仍要运行」；
- 若想彻底消除提示，只能使用受信任的代码签名证书签名（个人自用可忽略）；
- 自签名证书签名后，仍需在每台机器导入证书才不报警，不推荐。

### 5.3 杀毒软件误报

PyInstaller 打包的 exe 常被杀软（尤其 360/火绒/Defender 启发式）误报为“可疑程序/木马”，属于 bootloader 特征导致的常见误报：

- 把整个 `DouyinRecorder` 目录加入杀软白名单/排除目录；
- 若已被隔离，请先从隔离区恢复并信任；
- 可用 `CertUtil -hashfile DouyinRecorder.exe SHA256` 记录哈希，提交给杀软厂商做误报申诉。

### 5.4 命令行 / 计划任务

PowerShell / cmd 里请使用 **CLI 版本**（GUI 版是 Windows 子系统程序，PowerShell 不会等它结束，也拿不到退出码）：

```bat
cd /d D:\Tools\DouyinRecorder
DouyinRecorder-CLI.exe --check          :: 环境自检（Python/ffmpeg/目录/协议层/tkinter）
DouyinRecorder-CLI.exe --list           :: 列出主播
DouyinRecorder-CLI.exe --add 76274427953 --name 某主播
DouyinRecorder-CLI.exe --remove 76274427953
DouyinRecorder-CLI.exe --once           :: 只查一次直播状态（不录制）
DouyinRecorder-CLI.exe --cli            :: 无 GUI 常驻监控（Ctrl+C 退出）
```

Windows 计划任务示例（每 10 分钟查一次状态）：

```bat
schtasks /Create /TN "DouyinRecorder-Once" /SC MINUTE /MO 10 /TR "D:\Tools\DouyinRecorder\DouyinRecorder-CLI.exe --once"
```

如果一定要用 GUI 版 exe 跑命令行：`Start-Process -Wait -FilePath .\DouyinRecorder.exe -ArgumentList --check`。

### 5.5 替换 ffmpeg

两种方式：

1. **直接替换文件**：把新的 `ffmpeg.exe` 覆盖 `bin\ffmpeg.exe`（保持文件名不变），无需改配置；
2. **指定路径**：编辑 `config.json`，把 `ffmpeg_path` 设为绝对路径（例如 `D:\Tools\ffmpeg\bin\ffmpeg.exe`），留空则自动查找。

替换后运行 `DouyinRecorder-CLI.exe --check`，末尾会打印解析到的 ffmpeg 路径与版本。录制只用到 `-c copy`、
`-f segment`、concat remux，gyan.dev / BtbN 等 Windows build 都支持。

### 5.6 迁移 / 备份

- 整个 `DouyinRecorder` 目录拷走即可（配置用相对路径，换盘符/换机器都能用）；
- 需要保留的只有：`config.json`、`watchlist.json`、`records\`（录像）、可选 `data\`；
- 不要只拷贝 exe，`python313.dll`、`_tcl_data` 等运行时文件缺一不可。

---

## 6. 实测记录（本机打包产物，2026-11-13）

完整原始记录见仓库内 `build/tmp/verify-report.md`，GUI 截图见 `build/tmp/gui-printwindow.png`。

### 6.1 构建结果

```
==> 4/6 冒烟测试（--check）
===== 环境自检 =====
Python      : 3.13.14 (D:\Code\DS-Workspace\douyin-recorder\dist\DouyinRecorder\DouyinRecorder-CLI.exe)
操作系统    : Windows-11-10.0.26300-SP0
项目根目录  : D:\Code\DS-Workspace\douyin-recorder\dist\DouyinRecorder
配置文件    : ...\dist\DouyinRecorder\config.json
主播列表    : ...\dist\DouyinRecorder\watchlist.json（0 个主播）
输出目录    : ...\dist\DouyinRecorder\records（磁盘剩余 1422.8 GB）
日志目录    : ...\dist\DouyinRecorder\logs（保留 30 天）
ffmpeg 配置 : （自动：项目 bin -> 原版目录 -> PATH -> 常见目录 -> 注册表）
ffmpeg 解析 : ...\dist\DouyinRecorder\bin\ffmpeg.exe
              ffmpeg version 2025-02-26-git-99e2af4e78-essentials_build-www.gyan.dev
协议层      : dy_api
tkinter     : 可用

===== 构建完成 =====
文件数量 : 1001
目录体积 : 115.4 MB
GUI 主程序: 2.71 MB
CLI 程序 : 2.71 MB
ffmpeg   : 83.6 MB
压缩包   : ...\dist\DouyinRecorder_win64.zip (45.6 MB)
```

**重点验证通过**：`项目根目录`、`配置文件`、`输出目录`、`ffmpeg 解析` 全部是 `dist\DouyinRecorder\`，
说明 `contents_directory="."` 生效，程序没有把 `_MEIPASS`/`_internal` 当成项目根。

### 6.2 命令行实测（打包后的 exe，真实运行）

| 命令 | 退出码 | 耗时 | 结果 |
| --- | --- | --- | --- |
| `DouyinRecorder-CLI.exe --version` | 0 | 0.10~0.13 s | 打印 `抖音直播录制工具（本地自用版）v1.0.0` |
| `DouyinRecorder-CLI.exe --check` | 0 | 0.37~0.42 s | 全部检查通过：ffmpeg 解析到自带 `bin\ffmpeg.exe`、协议层 `dy_api`、tkinter 可用 |
| `DouyinRecorder-CLI.exe --list` | 0 | 0.16~0.18 s | 空列表正常提示 |
| `DouyinRecorder.exe --check`（窗口版） | 0 | 0.37~0.40 s | 输出与 CLI 版一致（stdout 经管道捕获正常） |
| `DouyinRecorder-CLI.exe --add 39008801136` | 0 | 0.20 s | 添加成功，写入 `watchlist.json` |
| `DouyinRecorder-CLI.exe --once`（真实抖音接口） | 0 | 0.84~1.92 s | 见 6.3（网络波动） |
| `DouyinRecorder.exe --once`（窗口版） | 0 | 0.84 s | 同 CLI 版 |

### 6.3 真实接口 `--once` 输出

```
协议层: dy_api
正在查询 1 个主播的直播状态...
  [直播中] As one（三角洲行动）（房间号 39008801136）标题: 最后三场，播完退网
      可录制流: 有
共 1 个主播，1 个正在直播
```

说明打包后的网络（urllib + ssl）、协议层、真实取流地址解析都正常（能拿到带签名的 flv 地址）。

### 6.4 自带 ffmpeg 真实执行（mock 端到端）

用内置 mock 协议层跑一次（mock 会用 ffmpeg 生成测试流并通过本机 HTTP-FLV 提供直播源）：

```
DouyinRecorder-CLI.exe --mock --once
[INFO] dyrec.mock_api: 生成 mock 测试流（120 秒）: ...\dist\DouyinRecorder\tests\tmp\mock\mock_live_120s.flv
[INFO] dyrec.mock_api: mock 测试流就绪: ...（19.2 MB）
[INFO] dyrec.mock_api: mock HTTP-FLV 服务已启动: http://127.0.0.1:<随机端口>/live.flv
  [直播中] 冒烟测试（房间号 39008801136）标题: Mock 测试直播（本地测试源）
      可录制流: 有
```

这验证了打包后的 ffmpeg 不仅能 `-version`，还能被程序实际拉起、编码生成文件（录制链路依赖的进程调用/中文路径参数都没问题）。

### 6.5 GUI 实测

- 双击 `DouyinRecorder.exe`：窗口标题 `抖音直播录制工具 · 本地版`（左上角品牌「抖音录制 / 本地版 v2.0」），进程正常创建主窗口；
- 从进程启动到主窗口可见：**0.32~0.78 秒**（本机 SSD 热启动，两次实测）；
- 界面：左侧导航「监控 / 录制记录 / 运行日志 / 设置 / 关于」，顶部统计「直播中 / 录制中 / 排队 / 错误 / 离线」，
  工具栏「立即检查 / 批量录制 / 批量设置 / 隐藏详情」，中间为主播列表，右侧为「主播详情」面板，
  底部状态栏显示「监控未启动」与「协议层: dy_api」，输出目录同样指向 exe 目录；
- 发送 `WM_CLOSE` 关闭窗口后进程退出码 0（无崩溃、无残留进程）。

GUI 截图（仓库内）：`build/tmp/gui-printwindow.png`。

### 6.6 绿色版可搬运性

| 测试 | 结果 |
| --- | --- |
| 把 zip 解压到全新目录运行 `--check` | 通过，程序根目录 = 解压目录（不依赖构建机绝对路径） |
| 解压到含中文和空格的路径（`build\tmp\中文 路径测试\`） | 通过，路径解析、ffmpeg 查找、`--version` 均正常 |
| 从其它工作目录启动（cwd 不是 exe 目录） | 通过，路径仍以 exe 目录为准 |
| 删除 `config.json` 后启动 | 自动重建，且 output_dir/log_dir 指向 exe 目录下的 `records\`、`logs\` |

### 6.7 启动耗时

| 场景 | 耗时 |
| --- | --- |
| `--version`（最小启动，3 次） | 0.103 / 0.108 / 0.110 秒（另一次 0.112 秒） |
| `--check`（含 ffmpeg -version 探测，3 次） | 0.388 / 0.392 / 0.398 秒（另一次 0.400 / 0.417 秒） |
| `--once`（含真实网络查询） | 0.84~1.92 秒（受网络波动影响） |
| GUI 双击 → 主窗口可见 | 0.32~0.78 秒 |

> 均为本机（Windows 11 + SSD）热启动数据；首次运行因杀软扫描、磁盘缓存未命中可能到 1~3 秒。onedir 比 onefile 明显快
> （onefile 每次都要解压约 100MB 到临时目录）。

---

## 7. 已知限制与常见问题

### 7.1 限制

1. **未签名**：exe 无代码签名 → SmartScreen 提示、部分杀软误报（见 5.2/5.3）。
2. **仅 Windows x64**：PE32+ x86-64，不支持 32 位 Windows，不支持 ARM64 原生（ARM64 上可用 x64 模拟运行，但未实测）。
3. **GUI 需要桌面会话**：tkinter 窗口需要交互式桌面；Windows 服务、无桌面的远程会话里请用 `DouyinRecorder-CLI.exe --cli`。
4. **目录必须在可写位置**：配置/日志/录像都写在 exe 目录；放 `Program Files` 会因权限失败（除非给目录写权限）。
5. **GUI 版 exe 不适合脚本**：Windows GUI 子系统程序在 PowerShell/cmd 里不会被等待，拿不到输出和退出码；脚本请用 CLI 版。
6. **未在 Windows 10 实机验证**：本机只有 Windows 11（10.0.26300）。技术栈（Python 3.13 + PyInstaller 6）官方支持 Windows 10 x64，
   预期可用，但未做 Win10 实机回归。
7. **`--version` 输出编码**：`--version` 在 `setup_console()` 之前打印，管道捕获时是 GBK（控制台显示正常）；
   `--check/--once/--list` 已统一 UTF-8，不影响使用。

### 7.2 打包/运行常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 构建报 `缺少 .pylibs/PyInstaller` | 先执行 2.2 的 `pip_fetch.py`；注意 `.pylibs` 在项目根，不要删 |
| 构建报 PyInstaller 版本过低 | Python 3.13 需要 PyInstaller >= 6.11；重新执行 2.2 更新到 6.22.3 |
| `build.ps1` 语法错误、中文乱码 | 脚本必须保存为 UTF-8 with BOM（PS 5.1 限制），见 2.4 注释 |
| 双击没反应/一闪而过 | 先 `DouyinRecorder-CLI.exe --check` 看报错；确认目录可写、Windows Defender 未隔离文件 |
| 提示找不到 ffmpeg | 确认 `bin\ffmpeg.exe` 未被删；或 config.json 的 `ffmpeg_path` 填绝对路径 |
| 移动目录后输出路径不对 | 程序只在 config.json 缺失时重建（重建用 exe 目录绝对路径）；可删除 config.json 重新生成，或手动把 `output_dir`/`log_dir` 改成 `records`/`logs` 相对路径 |
| 打包后路径跑到 `_internal` | 说明 `contents_directory="."` 丢了；检查 spec 中两个 EXE 都设置了该参数 |
| 窗口版从命令行启动看不到输出 | 无控制台时输出被丢弃（GUI 正常）；命令行请用 CLI 版 |

### 7.3 后备方案（如果 PyInstaller 方案在你的机器上不可用）

1. **目录 + 启动.bat（嵌入式 Python）**：下载 Windows embeddable Python 3.13 x64，解压到 `runtime\`，把 `src/`、`run.py`、
   `config.json`、`bin/ffmpeg.exe` 放进去，写 `启动.bat` 调用 `runtime\pythonw.exe run.py`。体积约 15MB（不含 ffmpeg），
   免安装，但首次要自己准备 embeddable 包，且不如单目录干净；
2. **onedir 改为“目录 + 启动.bat”**：保留 PyInstaller 产物的 `_internal` 布局，在根目录放 `启动.bat` 调用 `runtime` 里的 python 或
   `start "" DouyinRecorder.exe`，只改启动方式，不改变路径解析问题；
3. **不打包，直接分发源码 + Python 嵌入包**：最稳，适合只在少数几台机器使用；
4. **Onefile 模式**（`--onefile`）：单 exe 约 100MB，每次启动解压到临时目录，冷启动 3~10 秒且 -once 之类的短命令体验差，
   不推荐；若必须单文件，需注意 `_MEIPASS` 路径问题需要额外的路径重写。

---

## 8. 验收清单（构建后自检）

```bat
cd /d <解压目录>
DouyinRecorder-CLI.exe --version        :: 应输出 v1.0.0
DouyinRecorder-CLI.exe --check          :: 退出码 0；项目根目录 = 解压目录；ffmpeg 解析到 bin\ffmpeg.exe；tkinter 可用
DouyinRecorder-CLI.exe --list           :: 列出主播（首次为空）
DouyinRecorder-CLI.exe --once           :: 真实接口查询一次（需要能上网）
DouyinRecorder.exe                      :: 双击打开 GUI（人工确认窗口与界面正常）
```

对应自动化脚本：`build/tmp/verify_frozen.py`（本仓库内，开发用；不属于发行包）。

---

## 9. 复现环境备忘

- 构建机：Windows 11 x64（10.0.26300），Python 3.13.14，PyInstaller 6.22.3 + pyinstaller-hooks-contrib 2026.10；
- 依赖全部在 `douyin-recorder/.pylibs`（构建期使用，**不要**提交进发行包）；
- 构建产物：`dist/DouyinRecorder/` 与 `dist/DouyinRecorder_win64.zip`；中间文件在 `build/work/`、`build/stage/`、`build/app.ico`；
- 打包脚本与配置均在 `build/`：`build.ps1`、`DouyinRecorder.spec`、`rthooks/rthook_stdout.py`、`make_assets.py`；
- 本文档与 `_internal` 无关：发行包采用旧式平铺布局，`docs\` 与 exe 同级。
