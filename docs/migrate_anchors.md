# 主播列表迁移说明（原版 dyuser.json → 自研工具）

> 结论先说：原版 §dyuser.json§ 里的 620 个主播是**强加密**的，密钥在 VMProtect 壳里、且只在卡密登录成功后解密；本机卡密已过期 → **无法直接解密**（详见 §../docs/report.md§ §6、§analysis/dynamic/decrypted/DYUSER_ANALYSIS.md§）。
> 但我们从 Windows 自身的使用痕迹里恢复了 **394 个主播昵称**，可以借此把名单补回来。

## 1. 已恢复的资产

| 文件 | 内容 |
|---|---|
| `analysis/dynamic/decrypted/anchors_recovered.txt` | 394 个主播昵称 + 5 位短 ID，一行一个，可直接看 |
| `analysis/dynamic/decrypted/anchors_recovered.json` | 同上，JSON（短ID→昵称） |
| `analysis/dynamic/decrypted/anchor_name_to_suffix.json` | 反查（昵称→短ID） |
| `analysis/dynamic/decrypted/recording_files_observed.csv` | 25 个真实录制文件路径（含时间/清晰度/ts|mp4） |
| `analysis/dynamic/decrypted/ANCHORS_RECOVERED.md` | 说明与完整列表 |

## 2. 推荐迁移流程（约 10-20 分钟 / 100 个主播）

1. 打开 `anchors_recovered.txt`，把你要继续监控的主播昵称挑出来；
2. 在抖音里按昵称搜索主播 → 进入直播间/主页 → 复制链接；
3. 打开本工具 GUI → 「添加主播」→ 粘贴链接 → 名称可留空（工具会自动补全昵称）→ 确定；
4. 重复添加；也可以在命令行批量：

`§bat
:: 把链接一行一个放进 links.txt，然后：
for /f "delims=" %L in (links.txt) do python run.py --add "%L"
`§

5. 添加完成后，在 GUI「设置」里把 **输出目录** 改成原来的 `D:\抖音录制\直播`（如果还想沿用原目录结构），或保留默认 `records\`。

## 3. 输入格式支持（与原文一致）

1. 纯数字房间号：`76274427953`
2. 电脑直播间链接：`https://live.douyin.com/76274427953`
3. 手机分享短链：`https://v.douyin.com/iQLgKSj/`（或整段分享文案）
4. 电脑主播主页链接：`https://www.douyin.com/user/MS4wLjABAAAA...`

## 4. 如果以后恢复了卡密

若你续费/恢复卡密，可以让原版程序正常登录一次（登录成功后它会解密 dyuser.json 到内存），再用
`analysis/dynamic/decrypted/FRIDA_PLAN.md` 里的方案 hook `E2EECore.dll!e2ee_cryptoDecrypt` 导出明文 JSON（约 5 分钟）。
拿到明文后，可以按 JSON 里的 `web_rid/sec_uid` 批量添加（本工具的 §resolve_target§ 也接受 sec_uid 形式的主页链接）。
本任务没有、也不会做任何破解或绕过卡密的手段。

## 5. 其它建议

- 原版的录制文件命名是 `<昵称>_<YYYY年M月D日H时M分S秒>_原画M.ts`，本工具默认 `{anchor}_{date}_{time}`，可在 `config.json` 的 `filename_template` 改成你自己的模板；
- 原版输出目录结构是 `<昵称>（5位hex）`，本工具默认 `{anchor}/{date}`，可用 `output_template` 调整；
- 你原来的录制文件如果在 `D:\抖音录制\直播` 下，本工具不会动它们（只写自己的输出目录）。
