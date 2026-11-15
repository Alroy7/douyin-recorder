# 把项目上传到你自己的 Git 仓库

本仓库已经准备好 `.gitignore`：**只上传源码/文档/测试，不含**打包产物、ffmpeg、个人主播列表、配置、日志、录像、逆向分析产物。
当前机器**没有安装 Git**（检测不到 `git` 命令），需要先装一次。

## 一、安装 Git（Windows）

任选一种：

1) 命令行（推荐，Win10/11 自带 winget）：

```powershell
winget install --id Git.Git -e --source winget
```

2) 官网下载安装：https://git-scm.com/download/win （一路下一步即可）

安装完**重新打开一个终端**，执行 `git --version` 能看到版本号即可。

## 二、新建远程仓库

- GitHub：右上角 + → New repository → 名称如 `douyin-recorder` → **不要**勾选 Add README/.gitignore/License（保持空仓库）→ Create。
- 或 Gitee：新建仓库 → 同样保持空仓库。

> 建议设为 **Private（私有）**：文档里包含对商业软件的分析记录与直播协议信息，公开需自行评估风险。

## 三、初始化并推送（在当前项目目录执行）

```powershell
cd D:\Code\DS-Workspace\douyin-recorder

git init -b main
git config user.name  "你的名字"
git config user.email "你的邮箱"

git add -A
git commit -m "chore: 初始化仓库（v2.5.2 源码）"

# GitHub（把 <用户名> 换成你的）
git remote add origin https://github.com/<用户名>/douyin-recorder.git
# 或 Gitee：
# git remote add origin https://gitee.com/<用户名>/douyin-recorder.git

git push -u origin main
```

也可以直接运行仓库里准备好的脚本（它会做 init/commit，并打印 push 命令）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\init_git.ps1 -Name "你的名字" -Email "你的邮箱" -Remote "https://github.com/<用户名>/douyin-recorder.git"
```

## 四、认证方式

- **HTTPS**：GitHub 不再支持账号密码，需要 Personal Access Token（Settings → Developer settings → Personal access tokens → 勾选 repo 权限），push 时用户名填 GitHub 用户名、密码填 token。
- **SSH（推荐长期使用）**：`ssh-keygen -t ed25519 -C "你的邮箱"`，把 `~/.ssh/id_ed25519.pub` 内容加到 GitHub → Settings → SSH keys；然后 remote 用 `git@github.com:<用户名>/douyin-recorder.git`。

## 五、关于 exe 安装包

`.gitignore` 已排除 `dist/`、`bin/ffmpeg.exe`、`.pylibs/` 等大文件（GitHub 单文件 100MB 限制）。
如果要发布可执行版：在仓库页面 **Releases → Draft a new release**，把 `dist/DouyinRecorder_win64.zip`（46MB）作为附件上传即可。

## 六、如果不想装 Git

可以用 GitHub 网页版上传：把 `dist/DouyinRecorder_source.zip` 解压后，在网页仓库页面 **Add file → Upload files** 拖入整个文件夹（或直接在网页上传源码文件）。适合一次性的上传。

## 七、后续更新

```powershell
git add -A
git commit -m "说明本次改动"
git push
```

## 已排除的内容（如需调整请编辑 .gitignore）

| 排除项 | 原因 |
| --- | --- |
| `dist/`、`build/work/`、`build/stage/` | 打包产物，体积大且可重新生成 |
| `bin/ffmpeg.exe` | 83MB 二进制，可自行下载 |
| `.pylibs/`、`.wheels/` | 本地第三方依赖 |
| `config.json`、`watchlist.json` | 个人配置/主播列表（含本机路径） |
| `data/`、`logs/`、`records/` | 运行记录、日志、录像 |
| `tests/tmp/`、`tests/soak/logs*/`、`tests/soak/out*/` | 测试产物 |
| `analysis/` | 逆向分析过程产物（抓包/反编译/个人样本），默认不上传；如需要可删除 .gitignore 中 `analysis/` 一行 |
