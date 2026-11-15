# 抖音直播取流协议（实测结论 + 实现说明）

> 版本：v1.1.2（P1 性能改造 + 「无直播间」负缓存 + **解析缓存落盘**）· 实测时间：2026-11-13 19:19 – 11-14 00:55（Asia/Shanghai）
> 实现：`src/dy_api.py` · 证据：`analysis/probe/` · 性能对比：`analysis/probe/perf_compare.md` · 参考代码：`analysis/probe/refs/`

## 0. 一句话结论

**只需要一个 `ttwid` cookie，不需要 a_bogus / X-Bogus 签名。** 三条接口覆盖全部需求：

| 用途 | 接口 | 签名 | 实测 |
|---|---|---|---|
| 拿 cookie | `GET https://live.douyin.com/` 的 Set-Cookie | 不需要 | OK 200 |
| 查状态 + 取流（主接口） | `GET https://live.douyin.com/webcast/room/web/enter/?...&web_rid=<web_rid>` | **不需要** | OK status=2/4，flv/hls 全套 |
| 发现当前在播 | `GET https://live.douyin.com/webcast/feed/?...` | **不需要** | OK 每次 6~8 个在播房间 |
| 短链/主页 → web_rid | `GET https://webcast.amemv.com/webcast/room/reflow/info/?...` | **不需要** | OK 离线主播也能拿到数字 web_rid |

拉流地址（flv/hls）真实可播：用目标程序自带 `cmdffmpeg.exe` 实读 10 秒，返回码 0（见第 9 节）。

---

## 1. 完整链路图

```
                    ① GET https://live.douyin.com/  (桌面 Chrome UA)
                       └─ Set-Cookie: ttwid=...; UIFID_TEMP=...
                                   │
        ┌──────────────────────────┴───────────────────────────────────────────┐
        │ 输入 4 种形式（原版程序输入框也支持这 4 种）                          │
        └──────────────────────────┬───────────────────────────────────────────┘
                                   │
   1) 纯数字房间号         2) live.douyin.com/<web_rid>     3) v.douyin.com/xxxx   4) www.douyin.com/user/<sec_uid>
   web_rid 直接查          直接取路径里的 web_rid           302 跳转                 旧 user/info 接口
        │                        │                              │                     │
        │                        │                    webcast.amemv.com/douyin/        └─ 抖音号 unique_id
        │                        │                    webcast/reflow/<19位room_id>        （未签名可拿）
        │                        │                    ?...&sec_user_id=<sec_uid>                │
        │                        │                              │                    enter?web_rid=<抖音号>
        │                        │                    reflow/info(room_id, sec_uid)            │
        │                        │                    · 未签名可用                      reflow/info(room_id, sec_uid)
        │                        │                    · 返回 owner.web_rid（数字）      · 返回 owner.web_rid（数字）
        │                        │                              │                              │
        └────────────────────────┴──────────────┬───────────────┴──────────────────────────────┘
                                                ▼
                  GET https://live.douyin.com/webcast/room/web/enter/
                      ?aid=6383&app_name=douyin_web&live_id=1&device_platform=web
                      &enter_from=web_live&cookie_enabled=true&screen_width=1920
                      &screen_height=1080&browser_language=zh-CN&browser_platform=Win32
                      &browser_name=Chrome&browser_version=138.0.0.0&web_rid=<web_rid>
                  Header: User-Agent / Referer:https://live.douyin.com/ / Cookie:ttwid=...
                                                │
                                                ▼
                  data.data[0].status == 2  → 在播
                  data.data[0].stream_url.flv_pull_url      {FULL_HD1, HD1, SD1, SD2}
                  data.data[0].stream_url.hls_pull_url_map  同键
                  data.data[0].stream_url.live_core_sdk_data.pull_data.stream_data
                      → JSON 里的 data.origin.main.flv/hls = 原画(or4)
                                                │
                                                ▼
                  ffmpeg -i <flv 原画> -c copy ...（地址带 expire/sign，有时效）
```

---

## 2. cookie：ttwid

- 请求：`GET https://live.douyin.com/`，UA 用桌面 Chrome（`Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/138.0.0.0 Safari/537.36`）。
- 响应 `Set-Cookie` 下发 `ttwid`（形如 `1%7C3UUq...%7C1794569028%7C6e4d...`，URL 编码，%7C 即竖线，中间那段是签发时间戳）和 `UIFID_TEMP`。
- 后续所有接口带 `Cookie: ttwid=...` + `User-Agent` + `Referer: https://live.douyin.com/` 即可。
- 不需要登录账号、不需要 `msToken` / `__ac_signature` / `odin_tt` 等登录态 cookie。
- 实现：`src/dy_api.py` 内存缓存 30 分钟，落盘 `data/cookie.json`（含 `obtained_at`，文件兜底最长信任 12 小时）；请求失败/空响应时自动强制刷新 ttwid 再重试。
- `get_cookie()` / `python src/dy_api.py --cookie` 可拿当前 cookie 手工复用。

---

## 3. 接口明细

### 3.1 房间状态 + 取流（工具主接口，最重要）

```
GET https://live.douyin.com/webcast/room/web/enter/?<params>
params（实测缺项可能被风控，建议全带）:
  aid=6383
  app_name=douyin_web
  live_id=1
  device_platform=web
  language=zh-CN
  enter_from=web_live
  cookie_enabled=true
  screen_width=1920
  screen_height=1080
  browser_language=zh-CN
  browser_platform=Win32
  browser_name=Chrome
  browser_version=138.0.0.0
  web_rid=<web_rid>          ← 必填，且必须是 web_rid/抖音号；传 19 位 room_id 会 4001038/10011
headers: User-Agent, Referer: https://live.douyin.com/<web_rid>, Cookie: ttwid=...
签名: 不需要（实测）。ihmily 实现里加了 a_bogus，我们去掉后照样 200 + 完整数据。
```

响应关键字段（注意是 **data.data[0]**，不是 data 本身）：

| 字段 | 含义 |
|---|---|
| `status` | **2=直播中；4=未开播**（离线时 title/cover 仍在，没有 stream_url） |
| `status_str` | 同上，字符串 |
| `id_str` | 19 位直播间 room_id |
| `title` | 直播标题 |
| `cover.url_list[0]` | 封面 |
| `owner.nickname` / `data.user.nickname` | 主播昵称 |
| `owner.sec_uid` / `data.user.sec_uid` | 主播 sec_uid |
| `stream_url.flv_pull_url` | {FULL_HD1, HD1, SD1, SD2} → flv 地址 |
| `stream_url.hls_pull_url_map` | 同键 → m3u8 地址 |
| `stream_url.live_core_sdk_data.pull_data.stream_data` | JSON 字符串，`data.origin.main.flv/hls` = **原画**（unique_id 带 _or4） |
| `stream_url.default_resolution` | 默认档位（一般是 HD1） |

注意：enter 接口**不返回开播时间**（create_time/finish_time 均为空），所以此路径 `LiveStatus.started_at = 0`；reflow 路径有 create_time 可作为开播时间。

### 3.2 在播发现（feed）

```
GET https://live.douyin.com/webcast/feed/?aid=6383&app_name=douyin_web&live_id=1
    &device_platform=web&language=zh-CN&cookie_enabled=true&count=8
签名: 不需要。返回 data[]，每项 .data 就是一个 room 对象（结构同 3.1），
      status==2 即在播；owner.short_id 可直接当 web_rid 用（实测 enter 一致）。
```

用 `fetch_recommend(limit)` 封装，GUI「发现当前在播」用它。

### 3.3 reflow/info（短链/19 位 room_id → 数字 web_rid）

```
GET https://webcast.amemv.com/webcast/room/reflow/info/?<params>
params: verifyFp=verify_hwj52020_7szNlAB7_pxNY_48Vh_ALKF_GA1Uf3yteoOY
        type_id=0&live_id=1&room_id=<19位room_id>&sec_user_id=<sec_uid>
        version_code=99.99.99&app_id=1128
签名: 不需要（ihmily 实现里加 a_bogus / X-Bogus，实测去掉仍返回完整数据）。
返回: data.room（结构同 3.1）+ data.user；关键 **data.room.owner.web_rid = 数字 web_rid**。
      离线房间也会返回“最后一场”的 room 和 owner.web_rid。
      偶发 status_code=10033 forbidden / Server upgrading（服务端临时错误）→ 重试。
```

### 3.4 主播主页链接（只有 sec_uid）

```
GET https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid=<sec_uid>
签名: 不需要。返回 user_info.unique_id（抖音号）。
兜底: GET https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=<sec_uid>&...
      该接口实测**不带 a_bogus 也返回 user 数据**（uid / unique_id / nickname）。

然后（关键实测发现）：
  enter 接口的 web_rid 参数**接受抖音号 unique_id 作为别名**：
    enter?web_rid=kingmengwang  → 王濛的房间（status=4，离线也返回最后一场 room）
    enter?web_rid=Fantis2020    → 范泰西的房间（status=2，在播）
  → 用 unique_id 先 enter 拿到 19 位 room_id，再 reflow(room_id, sec_uid) 得到数字 web_rid。
```

### 3.5 备用：check_user_live_status

```
GET https://live.douyin.com/webcast/distribution/check_user_live_status/
    ?user_ids=<数字uid>&aid=6383&distribution_scenes=254&channel=test
签名: 不需要。在播返回 user_live[{live_status:1, room_id_str:...}]；
      user_ids 要用**数字 uid**（sec_uid 传进去返回空）。离线时 user_live=[]（拿不到 room_id）。
→ 因此它不能替代 3.4 的离线路径，仅作为“主播是否在播”的快速探测备选。
```

---

## 4. 输入形式与解析规则（resolve_target）

原版程序输入框支持 4 种（见 Lead 内存 dump），全部覆盖：

| 输入 | 解析结果 | 后续 |
|---|---|---|
| `76274427953`（纯数字房间号） | web_rid=76274427953 | enter |
| `https://live.douyin.com/76274427953` | web_rid（取路径） | enter |
| `https://v.douyin.com/iQLgKSj/` | 302 → reflow → web_rid=466747346712 | enter |
| `https://www.douyin.com/user/<sec_uid>` | user/info → unique_id → enter → reflow → web_rid=369324308707 | enter |
| 分享文案（夹带链接/房间号：xxx） | 正则抠 URL / 房间号 | 同上 |

其他细节：
- `v.douyin.com` 短链 302 落到 `webcast.amemv.com/douyin/webcast/reflow/<19位room_id>?...&sec_user_id=<sec_uid>`，需要再调 3.3 换 web_rid。
- 纯数字 16~20 位视为 19 位 room_id（单靠它无法查询，必须配合 sec_uid，报错文案会提示）。
- 主页 sec_uid → 数字 web_rid 映射缓存 **12 小时**（`SEC_UID_CACHE_TTL`；web_rid 基本终身不变），长期轮询同一批主播时只多付一次解析开销；
  若某次因 reflow 临时失败（10033）退回抖音号别名，只缓存 **10 分钟**（`SEC_UID_ALIAS_TTL`）后重试拿数字 web_rid。
- **「无直播间」负缓存（v1.1.1）**：若接口明确返回了账号资料、但该账号没有抖音号/解析不出直播间
  （从未开播/已注销/私密等，实测占真实 1360 主播抽样的约 12%），则把这条「解析失败」结果缓存
  **10 分钟**（`NEGATIVE_CACHE_TTL`，可改常量）：期间对该主页链接的重复查询直接返回**完全相同**的 error，
  **不再发 3~6 次解析请求**；一旦解析到直播间（或缓存过期后重试成功）负缓存立即失效。
  网络/风控等「不确定」失败**不会**写入负缓存（10s 后重试仍会重新请求）。
- **解析缓存落盘（v1.1.2）**：sec_uid → web_rid 的正缓存写入 `data/resolve_cache.json`，
  重启后首次使用时自动加载（默认上限 12h；环境变量 `DYREC_RESOLVE_CACHE` 可覆盖路径），
  写入去抖 ≤10s、tmp+os.replace 原子写，失败只 warning；负缓存不落盘。详见 §10「解析缓存落盘」。

---

## 5. 签名结论（a_bogus / X-Bogus）

**结论：本项目全部链路不需要签名。**

实测（2026-11-13）：
- `webcast/room/web/enter/`：不带 a_bogus → 200 + 完整 stream_url。
- `webcast/feed/`：不带签名 → 200 + 在播列表。
- `webcast/room/reflow/info/`：不带签名 → 200 + owner.web_rid。
- 对照实验：分区列表接口 `/webcast/web/partition/detail/room/v2/` 不带签名返回 200+0 字节 / 另一个版本 HTTP 444；
  带上用 ihmily 算法算的 a_bogus 返回 1.1MB 数据（`analysis/probe/partition_v2_signed.json`），
  说明签名算法本身有效，但**我们用的三个接口不校验它**。
- 已移植纯 Python a_bogus（出处 ihmily/DouyinLiveRecorder, MIT）到 `analysis/probe/abogus_ihmily.py`，验证可发出签名请求，作为风控升级时的后备，未进主流程。

若未来 enter/feed 开始校验签名：把 `analysis/probe/abogus_ihmily.py` 的 `ab_sign(query, ua)` 结果作为
`&a_bogus=<urlencode(sig)>` 拼到 URL 上即可（注意签名对象是**完整 query string**，且 UA 必须与实际请求一致）。

---

## 6. 状态码 / 错误码表

| 现象 | 来源 | 含义 / 处理 |
|---|---|---|
| status=2 | enter / feed / reflow | 直播中，取流 |
| status=4 | enter / reflow | 未开播（title/cover 仍在，无 stream_url） |
| status_code=0 | enter/feed/reflow | 请求成功（业务错误看 status / data） |
| status_code=4001038 | enter | web_rid 非法/房间不存在（如传 19 位 room_id 或乱码数字）→ 不可重试，报错给用户 |
| status_code=10011 | reflow | 参数错误（room_id 无效/缺失） |
| status_code=10033 | reflow | 服务端临时错误 Server upgrading → 退避重试（我们 retries=2） |
| status_code=10038 | reflow | 房间不存在/已删除 → 回落 enter 用抖音号别名 |
| HTTP 444 Access Denied | 部分 webcast 接口 | 未签名 + 未带 ttwid，被边缘节点拒绝 |
| HTTP 200 + 空 body | 风控 | 刷新 ttwid 重试一次；仍空则返回 LiveStatus(error=...)，不抛异常 |
| HTTP 200 + HTML（非 JSON） | 风控/跳登录 | 同上 |

`src/dy_api.py` 对外只暴露 `LiveStatus.error`：
error 为空串表示查询成功（is_live 才代表是否在播）；非空表示网络/风控/房间错误，**从不抛异常**。

---

## 7. cookie 生命周期与失效

- 实测 `ttwid` 值里带签发时间（如 ...%7C1794569028%7C...），属于长期匿名访客票据，未登录场景下未观察到快速过期；
  但风控策略会随 IP/频率变化，不能假设永久有效。
- 策略：内存 30 分钟（`COOKIE_TTL`）→ 磁盘 `data/cookie.json` 兜底 12 小时（`COOKIE_FILE_TTL`）；
  请求异常/空响应时强制重新 GET 首页刷新并重试。
- 换 IP/长时间停跑后，直接删除 `data/cookie.json` 或调 `get_cookie(force=True)` 即可。

## 8. 风控表现与对策

| 表现 | 对策（已实现） |
|---|---|
| 200 + 空响应 / HTML | 刷新 ttwid，退避 0.25~0.6s 重试 1~2 次，仍失败返回 error |
| 请求过密被限 | 单查询 1~2 个 HTTP 请求；解析/重试用整体超时预算截断（默认 12~15s） |
| cookie 失效 | 自动刷新（见第 7 节） |
| reflow 10033 | 重试 2 次 |
| 轮询频率 | 建议 ≥30~60s/房间（原版程序也是轮询），并发线程数 ≤ 房间数，避免突刺 |
| 拉流地址过期 | 地址带 expire/sign，**每次开播/断流重连都必须重新 get_live_status 取新地址**，不要长期缓存 |
| HTTP 444（新建连接被拒） | 临时反滥用状态（压测高峰出现过，复用连接不受影响）；刷新 ttwid 重试一次，持续 444 时按轮询间隔退避，连接复用可显著降低触发概率 |

---

## 9. 实测证据（2026-11-13，本机真实网络）

| 证据 | 内容 | 文件 |
|---|---|---|
| FLV/HLS 可读 | 房间 76274427953（涛仔，room_id 7707592311743925019，status=2）：cmdffmpeg.exe -v error -rw_timeout 15000000 -i <flv FULL_HD1> -t 10 -f null - → **rc=0**（7.8s）；同命令 HLS FULL_HD1 → **rc=0**（10.7s） | `analysis/probe/ffmpeg_verify.json` |
| 短链端到端 | https://v.douyin.com/iQLgKSj/ → reflow → web_rid=466747346712（范泰西，在播）→ enter → 原画 flv → ffmpeg **rc=0**（8.8s） | `analysis/probe/shortlink_e2e_verify.json` |
| dy_api 全链路 | web_rid / URL / 短链 / 主播主页 4 种输入全部跑通；flv 原画 ffmpeg rc=0（8.1s）、hls 原画 rc=0（10.6s） | `analysis/probe/dy_api_selftest_report.json` |
| 主页链接（离线） | 王濛主页（sec_uid）→ unique_id=kingmengwang → enter → reflow → **web_rid=369324308707**，status=4、title=京东运动「濛」力来袭 | `analysis/probe/sec_uid_chain.json` |
| 主页链接（在播） | 范泰西主页 → web_rid=466747346712，status=2；喜剧电影笑不停主页 → web_rid=745964462470，status=4 | `analysis/probe/sec_uid_chain.json` |
| 在播发现 | feed 一次返回 6 个 status=2 房间，全部带 flv/hls 全档位 | `analysis/probe/feed_now.json` |
| 错误码 | 不存在 web_rid → status_code=4001038；reflow 缺 room_id → status_code=10011 | `analysis/probe/enter_edge_cases.json` |

复现命令（在项目根目录）：

```
python src/dy_api.py --selftest                     # 自动挑一个在播房间
python src/dy_api.py --selftest 76274427953
python src/dy_api.py --selftest https://live.douyin.com/76274427953
python src/dy_api.py --selftest https://v.douyin.com/iQLgKSj/
python src/dy_api.py --selftest https://www.douyin.com/user/<sec_uid>
python src/dy_api.py --recommend 10                 # 发现当前在播
python src/dy_api.py --qualities 76274427953        # 清晰度列表（GUI 画质选择/排错）
python src/dy_api.py --stats                        # 本进程 HTTP 统计（请求/连接/流量/缓存）
python src/dy_api.py --check                        # 查看解析缓存文件与条目数（落盘缓存诊断）
python analysis/probe/test_dy_api.py                # 集成自测（含 ffmpeg 实读）
python analysis/probe/perf_bench.py                 # P1 性能对比（生成 perf_compare.md）
```

---

## 10. src/dy_api.py 使用说明

### 函数清单（契约）

| 函数 | 说明 |
|---|---|
| `LiveStatus` | dataclass：is_live / room_id(=web_rid) / title / anchor_name / cover_url / started_at / streams / raw / error |
| `resolve_target(text, timeout=None) -> dict` | 解析 4 种输入 → {web_rid, sec_uid, url, room_id, unique_id, source, error}，永不抛异常 |
| `get_live_status(target, cookie=None, timeout=15) -> LiveStatus` | 查状态+取流；失败返回 error |
| `choose_stream(status, prefer='flv', quality='origin') -> str or None` | 选可录制地址；quality 支持 origin / best / uhd / hd / sd / ld / ao / FULL_HD1... |
| `get_qualities(status, prefer='flv') -> list` | **v1.1 新增**：清晰度列表 [{name,sdk_key,resolution,bitrate,fps,url}]，按画质降序（GUI 展示/选择） |
| `get_cookie(force=False) -> dict` | 当前 cookie（ttwid） |
| `fetch_recommend(limit=10, timeout=15) -> list[LiveStatus]` | 发现当前在播（GUI 用） |
| `get_cookie_error() -> str` | 最近一次取 cookie 失败原因 |
| `get_http_stats() / reset_http_stats() / close_connections()` | HTTP 统计（请求/建连/线上字节/解压字节/错误/negative_cache_hits/**cache_disk_hits/cache_disk_saved**）、清零、关闭当前线程连接 |
| `flush_resolve_cache()` | **v1.1.2 新增**：立即把解析缓存原子落盘（测试/退出前可调），返回 bool 不抛异常 |
| `get_resolve_cache_info()` | **v1.1.2 新增**：缓存文件路径/条目数/有效条目/过期条目/内存条目（`--check` 用） |

### streams / raw 结构

```python
status.streams = {
    "flv": [url_origin, url_uhd, url_hd, url_sd, url_ld, url_md, ...],   # 按质量从高到低
    "hls": [url_origin, url_uhd, ...],
    "origin": {"flv": "...", "hls": "...", "codec": "264", "sdk_params": {...}},
}
# v1.1 瘦身：raw 不再保留原始大 JSON / 全量 URL dict，URL 只在 streams 里存一份
status.raw = {
    "source": "enter" | "reflow" | "feed",
    "status": 2,                        # 2=在播 4=未开播
    "web_rid": "...", "room_id": "<19位>", "sec_uid": "...",
    "default_resolution": "HD1",
    "flv_pull_url_keys": ["FULL_HD1", "HD1", "SD1", "SD2"],   # 老档位键（只留键，不含 URL）
    "hls_pull_url_keys": ["FULL_HD1", "HD1", "SD1", "SD2"],
    "qualities": [                      # 精简元数据；flv_idx/hls_idx 是 streams 列表下标
        {"name": "蓝光", "sdk_key": "uhd", "resolution": "1440x1080", "bitrate": 14766000,
         "fps": 60, "flv_idx": 1, "hls_idx": 1, "map_key": "FULL_HD1", "score": 22964083200000},
        ...
    ],
}
```

档位实测备注：
- 不同房间（甚至同一房间不同场）`uhd/origin/SD1/SD2` 的语义并不一致：实测某房间 SD1 实际是 _ld 码流、SD2 实际是 _sd 码流。
- 所以「最高画质」不要按名字硬编码：用 `get_qualities(status)`（按 score=分辨率面积×码率 降序）或
  `choose_stream(status, quality='best')`（实时按元数据排序；缺失时回退 origin→FULL_HD1→HD1→SD1→SD2→hls）。
- `choose_stream` 的 quality 参数：`origin`（默认，原画流）、`best`、`uhd/hd/sd/ld/ao`、
  老档位键 `FULL_HD1/HD1/SD1/SD2`、中文名（蓝光/超清/高清/标清）都支持；找不到时按顺序回退，不会返回空。
- **P0 兼容性变化**：`raw['flv_pull_url']` / `raw['hls_pull_url_map']`（P0 曾提供的全量 dict）在 v1.1 中改为
  `raw['flv_pull_url_keys']` / `raw['hls_pull_url_keys']`（只留键）。需要「按档位取 URL」请用
  `choose_stream(status, quality='FULL_HD1')` 或 `get_qualities(status)`。

### 超时 / 重试 / 并发（v1.1）

- **连接复用**：每线程一组 `http.client.HTTPSConnection`（按 scheme/host/port 缓存，`threading.local`），
  TLS 上下文复用；网络异常/超时自动丢弃连接并重试 1 次；`resp.will_close` 时主动丢弃。
- **gzip**：请求 `Accept-Encoding: gzip, deflate`，按 `Content-Encoding` 用 zlib 解压（br 不请求）。
- **超时**：建连/TLS 5s、读取 10s；`get_live_status` 整体仍是 12~15s 预算（解析/enter/reflow 共享 deadline）。
- **响应上限**：单响应 2MB（流式读取超限即中止 → 返回 `LiveStatus(error=...)`，绝不抛异常）。
- 每次 HTTP 请求最多 2~3 次尝试（风控/空响应/444 时刷新 ttwid）；短链解析最多跟 30x 跳转。
- **并发**：每线程独立连接；cookie / sec_uid 映射缓存加锁；12 线程 1000 次混合轮询实测 0 非预期错误。
- 只依赖标准库（urllib/http.client/ssl/http.cookiejar/zlib/json/threading），**不依赖 .pylibs**。

### 性能（v1.1，1000+ 房间长期挂机）

| 指标 | v1.0 | v1.1 | 说明 |
|---|---|---|---|
| 在播房间单次线上流量 | 617 KB | 108 KB | gzip 实测 6.9x（解压后 765KB） |
| 离线房间单次线上流量 | 424 KB | 62 KB | gzip 6.6x |
| 1000 次混合轮询总流量（12 并发） | 378 MB | 67 MB | **-82%** |
| 1000 次总耗时 / 吞吐 | 90.5s / 11.1 req/s | 72.5s / 13.8 req/s | +25% |
| 新建连接数 | ~1035（每请求一次 TLS） | **12**（每线程 1 条） | 轮询不再制造 TLS 握手 |
| LiveStatus 内存（JSON 代理值） | 13.3 KB | 6.8 KB | **-49%**（raw 瘦身/URL 去重） |

完整对比（含 444 风控观察、复现脚本）：`analysis/probe/perf_compare.md`、`analysis/probe/perf_bench.py`。
测试环境：本机直连，12 线程，负载 = 3 在播 + 1 离线 + 1 非法 混合轮询 1000 次。

### 「无直播间」负缓存（v1.1.1）

真实场景：1360 个主播主页链接里有相当一部分账号（从未开播 / 已注销 / 私密 / 没有抖音号）无法解析出直播间。
旧逻辑每轮都要重新走「user/info → 兜底 profile」2~4 个请求，长期挂机时纯浪费且增加风控暴露。
v1.1.1 增加负缓存：

| 项 | 说明 |
|---|---|
| 触发条件 | 接口**明确**返回了账号资料，但拿不到抖音号/直播间（`definitive=True`）；网络失败/风控不算 |
| 缓存内容 | 该 sec_uid 的原始 error 字符串（对外 error 文案与旧版完全一致） |
| TTL | `NEGATIVE_CACHE_TTL = 600` 秒（可改常量） |
| 命中效果 | 不再发任何解析请求，直接返回相同 `LiveStatus(error=...)`；`get_http_stats()['negative_cache_hits']` +1 |
| 失效 | 解析成功（拿到直播间）立即删除；TTL 到期后自动重试；线程安全（`threading.RLock`） |
| 实测 | 真实失败主页链接：第 1 次 4 个请求 / 1.77s → 第 2、3 次 **0 请求 / 0.000s**、error 完全一致；正常主页链接仍按 12h 正缓存（6 请求 → 0） |

证据与复现：`analysis/probe/neg_cache_probe.py`（真实 200/60 条抽样）、
`analysis/probe/test_negative_cache.py`（本地单测 19 项全 PASS）、`analysis/probe/neg_cache_real.py`（真实网络复现）。

### 解析缓存落盘（v1.1.2）

问题：12h 正缓存 / 10min 负缓存只在内存，**工具重启就丢**。实测 1000+ 主播每次重启都要重新冷启动 3–15 分钟
（每条约 5 次解析请求）。v1.1.2 把「已解析出 web_rid」的正缓存落盘，重启后直接复用。

| 项 | 说明 |
|---|---|
| 文件 | `data/resolve_cache.json`（环境变量 `DYREC_RESOLVE_CACHE` 可覆盖，测试用） |
| 结构 | `{version, updated_at, entries: {sec_uid: {web_rid, ts, kind, unique_id?, room_id?, nickname?}}}` |
| 加载 | 首次使用时懒加载；数字 web_rid 条目 TTL **12h**（`SEC_UID_CACHE_TTL`），别名条目 10min；过期/非法条目丢弃；损坏文件容错（只 warning，不阻塞） |
| 负缓存 | **不落盘**（重启后重新检查一次失败账号，也能在账号恢复开播时尽快发现）；内存 10min |
| 写入 | 有新增/变更才写；**去抖 ≤10s 一次**（`RESOLVE_CACHE_SAVE_INTERVAL`），`tmp + os.replace` 原子写；失败只 warning 并保留 dirty 重试，不影响录制 |
| 退出 | `atexit` 把未落盘的 dirty 数据写掉 |
| 统计 | `get_http_stats()['cache_disk_hits']`（命中来自磁盘缓存次数）、`['cache_disk_saved']`（累计写入条目次数） |
| 诊断 | `python src/dy_api.py --check` 打印缓存文件、条目数、有效/过期条目、内存条目、env 覆盖等 |

真实收益（analysis/probe/cache_restart_bench.py，12 条真实主播主页链接、两个独立进程）：

| 场景 | 耗时 | 解析请求 | 磁盘缓存命中 |
|---|---|---|---|
| 冷启动（无缓存文件） | 23.07s | 70 | 0 |
| 第二次启动（加载 resolve_cache.json） | **1.85s** | **4** | 11 |

即 12 条链接提速 **12.5x**；按 1000+ 主播规模估算，重启冷启动从 3–15 分钟降到秒级（仅剩失败账号重新解析）。
复现：`python analysis/probe/cache_restart_bench.py`；单测：`python tests/test_cache_persist.py`（9 项全 PASS，
覆盖读写/TTL/损坏容错/去抖/失败容错/新进程重启命中/env 覆盖/--check 字段）。

### 使用示例

```python
import sys; sys.path.insert(0, "src")
import dy_api

st = dy_api.get_live_status("https://www.douyin.com/user/<sec_uid>", timeout=15)
if st.error:
    print("查询失败:", st.error)
elif st.is_live:
    url = dy_api.choose_stream(st, prefer="flv", quality="origin")  # 原画 flv
    print(st.title, st.anchor_name, st.started_at, url)
    # 交给 recorder-dev: ffmpeg -i url -c copy out.flv
    # 断流/重连时重新 get_live_status 取新地址（地址带时效签名）
else:
    print("未开播", st.title)
```

---

## 11. 如何更新（接口失效排查手册）

1. **先确认 cookie 还能拿**：`python src/dy_api.py --cookie` → 无 ttwid 说明首页被风控/需要换 UA。
2. **用最小脚本复现**（不要动 src/dy_api.py，先写 analysis/probe/probe_xx.py）：
   - 拿一个当前在播房间：`python src/dy_api.py --recommend 3`；
   - 手工 GET enter 接口，看是否 200/空/HTML/4001038。
3. **判断是否新增签名要求**：若 enter/feed 从 200 变成 200+空/444，先试 `analysis/probe/abogus_ihmily.py` 的 ab_sign 拼 a_bogus；
   仍不行再去 GitHub 拉最新参考实现（codeload.github.com 本机可用，api.github.com 可用但 60 次/小时，raw.githubusercontent.com 常被重置）。
4. **前端版本号/参数变化**：browser_version 跟着 Chrome 实际版本改；新增必填参数从 live.douyin.com 页面 JS 的请求里看。
5. **接口字段变化**：重点看 data.data[0].stream_url 是否还有 flv_pull_url / live_core_sdk_data；
   若 live_core_sdk_data.pull_data.stream_data 结构变化，只需改 `_extract_streams()`。
6. 改完跑 `python analysis/probe/test_dy_api.py`（含 ffmpeg 实读）确认端到端。

---

## 12. 局限与未决

1. **主播无抖音号**或接口取不到 unique_id 时，主页链接无法解析出 web_rid（会返回明确 error）。
2. reflow 偶发 10033（服务端升级/风控）时，主页链接会退回抖音号别名（如 qiaoer_0102）。
   enter 接口对别名可用、能持续轮询状态，但**别名不是规范数字 web_rid**；等 reflow 恢复后缓存会自动更新。
   实测 4 个主播中 3 个能拿到数字 web_rid（王濛 369324308707、范泰西 466747346712、喜剧电影笑不停 745964462470），
   1 个（中元七七）当时遇到 10033 走了别名兜底。
3. enter 接口不返回开播时间：started_at 只有在 reflow 路径（create_time）才有值，enter 路径为 0。
4. HTTP 接口签名/风控由抖音侧随时可能调整，本文件记录的是 2026-11-13 的实测状态。
5. 未做登录态（不登录、不模拟登录、不要求用户账号密码）。
6. v1.1 起 `LiveStatus.raw` 做了瘦身（不再有 flv_pull_url/hls_pull_url_map 全量 dict，qualities 用下标引用 streams）；
   上层若需要「按档位取 URL」请用 `choose_stream(status, quality='HD1')` 或 `get_qualities(status)`。
7. 444 属于临时风控（非接口变更）：新建连接可能被拒，已有 keep-alive 连接通常可用；长时间挂机请保持连接复用、`check_interval>=30s`。
8. 解析缓存落盘只保存**正缓存**（sec_uid→web_rid）：负缓存不落盘，重启后「无直播间」账号会再解析一次（约 3~6 请求）；
   如需重启后也快速跳过失败账号，可另加短期负缓存落盘（当前刻意不做，保证账号恢复开播能尽快被发现）。

## 13. 参考实现出处与许可

仅摘录协议相关代码做互操作研究与算法对照，未整体复制；文件在 analysis/probe/refs/（详见该目录 README）：

| 仓库 | 许可 | 用途 |
|---|---|---|
| [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | MIT | douyin enter/feed 逻辑、纯 Python a_bogus（src/ab_sign.py）、短链解析（src/room.py） |
| [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) | AGPL-3.0 | 房间信息 / 弹幕 protobuf、__ac_signature |
| [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | Apache-2.0 | a_bogus / X-Bogus 最全 Python 实现、直播接口封装 |
