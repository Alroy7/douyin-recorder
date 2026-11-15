# Lead 实测结论：抖音直播取流（2026-11-13 19:20 左右，真实在线房间）

## 结论：不需要 a_bogus / X-Bogus 签名，只需要 ttwid cookie

### 1. cookie
GET `https://live.douyin.com/`（UA 用桌面 Chrome），响应 Set-Cookie 里含 `ttwid`（另有 UIFID_TEMP）。后续请求带上 ttwid + UA + Referer: https://live.douyin.com/ 即可。

### 2. 在播房间列表（未签名可用）
`GET https://live.douyin.com/webcast/feed/?aid=6383&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&cookie_enabled=true&count=6`
- 返回 `data[].data`，`status==2` 表示在播；
- 字段：`id_str`(19 位 room_id)、`title`、`user_count`、`owner.short_id`、`owner.nickname`、`stream_url`；
- **stream_url 直接带可拉流地址**：`stream_url.flv_pull_url` = {FULL_HD1, HD1, SD1, SD2}，`stream_url.hls_pull_url_map` 同键。

### 3. 按 web_rid 查房间状态（未签名可用，工具主用接口）
`GET https://live.douyin.com/webcast/room/web/enter/?aid=6383&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live&cookie_enabled=true&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=138.0.0.0&web_rid=<web_rid>`
- `data.data[0].status == 2` 在播（4=未开播），`status 2` 时 `data.data[0].stream_url` 有 flv/hls 地址；
- 同时返回 `title`、`owner.nickname`、`owner.avatar_thumb`、`similar_rooms`。
- **重要**：`web_rid` == feed 里的 `owner.short_id`（实测 54702621639 等三个房间 id_str 完全一致）。
- 传 19 位 room_id 或用 `room_id=` 参数都失败（status_code 4001038/10011），必须用 web_rid。

### 4. ffmpeg 可读
`cmdffmpeg.exe -v error -rw_timeout 15000000 -i <flv_pull_url[FULL_HD1]> -t 8 -f null -` → 返回码 0（HLS 也可用）。

### 5. 工具设计含义
- 监控已知主播：watchlist 存 web_rid → 轮询 enter 接口 → status==2 时取 stream_url 录制。
- 分享链接解析：短链 `https://v.douyin.com/xxx` 302 后 location 里是 `https://live.douyin.com/<web_rid>`；纯数字直接用。
- 「推荐在播」功能：可直接用 `/webcast/feed/` 接口（未签名）做发现/测试用。
- 拉流地址有时效（URL 带签名参数），每次开播/重连都要重新调 enter 获取，不能缓存太久。

## 实测证据文件
- analysis/lead/web_feed.json（在播列表原始响应）
- analysis/lead/feed_room_full.json（单个在播房间完整字段）
- analysis/lead/ffmpeg_verify.json（ffmpeg 读流结果 rc=0）
- analysis/lead/get_live_url.py（最小可跑实现，队友可直接复用逻辑）
- analysis/lead/enter_seed.json（未开播房间 enter 响应，status=4）