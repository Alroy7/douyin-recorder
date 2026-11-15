# 环境与协作约定（所有成员必读）

## 工作目录
项目根目录：`D:\Code\DS-Workspace\douyin-recorder`
（文件沙箱只允许写这个目录下的文件；读目标程序可读 D 盘任意位置）

## 目标程序（逆向对象）
- 目录：`D:\抖音录制\抖音直播录制工具`
- 主程序：`抖音直播录制工具.exe`（45,973,504 字节，PE32 i386，12 个节，节名被混淆，含 .vmps0/.vmps1，疑似 VMProtect 保护，无版本信息）
- 守护：`抖音直播录制工具守护助手.exe`（20MB）
- 安装：`安装升级.exe`（24MB）
- 数据：`dyuser.json`（499KB，base64 密文，内含 620 个主播）
- 备份：`抖音主播数据备份\*.json`（同格式备份）
- 日志：`log\*.log`（GBK 编码，含中文提示，如「登录失败：1003，已过期，请购买」「退出成功：DYZBcAEy6dCCVWuWjkYV3G」）
- 自带播放器：`PotPlayer\`（含 cmdffmpeg.exe = 真 ffmpeg 2025-02-26 gyan build、mpv.exe、PotPlayer64.exe）

## 工具环境
- Python：`python` = 3.13.14（PATH 中可用），tkinter 可用。
- 额外库已解包到 `.pylibs`：pefile, capstone, requests, pycryptodome。
  用法：脚本开头 `sys.path.insert(0, r"D:\Code\DS-Workspace\douyin-recorder\.pylibs")`
- 没有 git / 7z / node / ffmpeg 命令（ffmpeg 用目标目录里的 cmdffmpeg.exe）。
- pwsh 工具实际是 Windows PowerShell 5.1，不存在 `pwsh` 命令；多行脚本写成 .ps1/.py 文件再执行。

## 网络（重要）
- **只有 Python 的 urllib/requests 能联网**；PowerShell 的 Invoke-WebRequest / curl / pip 全部 TLS 失败（不要用）。
- DNS 正常，TCP 正常，Python ssl 正常，证书校验可关闭。
- GitHub：`api.github.com` 可用（取文件用 `/repos/{owner}/{repo}/contents/{path}`，返回 base64）；raw.githubusercontent.com 常被重置，别依赖。
- 抖音：`https://live.douyin.com/`、`https://www.douyin.com/` 可访问。
- pip 装包会因临时目录权限失败；要装库用 `analysis/pip_fetch.py <pkg>...`（直接下 wheel 解包到 .pylibs）。

## 硬性约束
- 不要破解授权/绕过付费许可（不做注册机、不 patch license 校验）。只做互操作性分析 + 自研工具。
- 不要修改或删除 `D:\抖音录制` 下的任何文件，只读。可以运行目标 exe 采集行为，但别改其数据；需要复制出来再分析。
- 大文件（内存 dump、导出数据）放 `analysis/<组>/dumps/`，单文件 >50MB 先压缩或说明。
- 报告用中文写，路径写绝对路径。
- 完成后用 send_message 给 lead 发一条：做了什么、结论、产物路径、未决问题。
