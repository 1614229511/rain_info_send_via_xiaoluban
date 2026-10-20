# 街道级即时降雨监控与起止通知

一个轻量、无第三方依赖的本地插件：每 5 分钟检查一次你所在街道/百米级网格的降水状态，
在**开始下雨**和**雨停**时通过小鲁班推送提醒。

- 数据源：彩云天气 `minutely`（公里级网格，未来两小时逐分钟）；token 无分钟级权限时自动降级到 `realtime` 实况，不中断监控
- 单位：`unit=metric:v2`（mm/h）；彩云的 `unit=metric` 返回 0~1 雷达强度，不能直接套 0.08 阈值
- 判定阈值：`0.08 mm/h`（可感知小雨）
- 防抖：连续 3 次检测无雨才判定雨停（5 分钟周期约 15 分钟）
- 定位：显式坐标 → Windows/macOS 原生定位 → 公网 IP 兜底
- 推送：调用同目录的 `push_client.py`，任何一轮只在小鲁班返回成功后才提交状态

## 工作流程

每个周期严格按以下顺序执行：

1. 读取 `config.json` 与 `runtime_state.json`
2. 位置解析（三级降级，见下文）
3. 请求彩云 `/weather` 综合接口（一次返回实况；有分钟级权限时同一响应还带 `minutely`）
4. 取当前第 1 分钟/实况降水强度（mm/h），状态机推导 + 防抖
5. 仅在状态发生有效跳变时调用 `push_client.py`
6. 原子回写 `runtime_state.json`

## 数据源与单位

| 数据 | 权限 | 用途 |
| --- | --- | --- |
| `/weather` 综合接口 | 基础套餐即可 | 每轮只发一次请求，返回 `realtime`，有权限时还返回 `minutely` |
| `result.realtime.precipitation.local.intensity` | 基础套餐 | 当前实况降水强度 |
| `result.minutely.precipitation_2h[0]` | 企业套餐（增值服务） | 未来两小时逐分钟；有权限时优先使用 |

彩云文档的关键点：

- **token 无分钟级权限时，整个 `result.minutely` 块不返回**（HTTP 仍为 200，只有 `result.primary`）；
- `unit=metric` 的降水是 0~1 的雷达强度，`unit=metric:v2` 才返回 mm/h。

因此插件统一请求 `unit=metric:v2`，`0.08 mm/h` 阈值可以直接比较。使用 `/weather`
综合接口而不是单独请求 `/minutely` 的原因是：**每个周期只发一次请求**，
基础 token 不会因为“先试 minutely、再试 realtime”而触发彩云限流。

- 有 `minutely` 权限：优先用逐分钟数据，`result.minutely.description` 作为“彩云预报”；
- 无 `minutely` 权限：自动用 `realtime`，并用 `result.forecast_keypoint` 补充预报描述，
  起止判断不受影响。

`realtime` 的描述会带上天气现象和最近降水带距离，例如：
`实况 雨；本地无降水；最近降水带约 189 米外（1.57 mm/h）`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `rain_monitor.py` | 主程序：定位、拉取、状态机、推送、持久化 |
| `state_machine.py` | 纯逻辑状态机与防抖（可单独测试） |
| `geo.py` | WGS84 → GCJ-02 坐标转换（中国大陆） |
| `net.py` | 代理策略：默认直连，可配置系统代理/手动代理与内网 bypass |
| `push_client.py` | 小鲁班推送客户端，可独立命令行使用 |
| `config.json` | 你的本地配置（含密钥，已被 `.gitignore` 忽略） |
| `config.example.json` | 配置模板 |
| `runtime_state.json` | 运行时状态（含坐标，已被忽略） |
| `helpers/geolocate_windows.ps1` | Windows 原生定位（WinRT Geolocator） |
| `helpers/geolocate_macos.swift` | macOS CoreLocation 兜底脚本 |
| `install_task.ps1` / `uninstall_task.ps1` | 安装/卸载 Windows 计划任务 |
| `tests/` | 单元测试 + 本地 mock 端到端测试 |

## 快速开始

### 1. 填写配置

```powershell
Copy-Item config.example.json config.json   # 首次使用
notepad config.json
```

至少填写三项：

```json
{
  "weather_token": "彩云天气 token",
  "receiver": "a00123456",
  "auth": "小鲁班 token"
}
```

`config.json` 是唯一需要填写凭据的地方，文件里的 `_填写说明` 也列出了待填项；
填入后可用 `py -3 rain_monitor.py --status` 查看是否还缺东西。

- 彩云 token：<https://dashboard.caiyunapp.com/> 注册后创建
- 小鲁班 `auth`：给小鲁班发送「获取发送token」
- `receiver`：你的 uid，格式如 `a00123456`

> 想要稳定的街道级精度，强烈建议同时填写 `latitude` / `longitude`（见「定位策略」）。
> 修改配置后无需重装计划任务，下一轮会自动读取。

### 2. 验证推送链路

```powershell
py -3 rain_monitor.py --test-push
```

收到测试消息即说明 `receiver` / `auth` / 网络都正常。

### 3. 先干跑一轮

```powershell
py -3 rain_monitor.py --once --dry-run --verbose
```

`--dry-run` 会真实拉取和评估，但不推送、不写状态，适合确认定位和接口是否正常。

### 4. 安装每 5 分钟的计划任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install_task.ps1 -RunNow
```

任务以当前登录用户身份运行，仅在用户登录时执行，使用 `pythonw.exe`，不会弹出黑窗口。
日志写入 `logs\monitor.log`（2 MB 轮转，保留 3 份）。

```powershell
# 查看任务
schtasks /Query /TN "RainMonitor" /V /FO LIST

# 卸载
powershell -NoProfile -ExecutionPolicy Bypass -File uninstall_task.ps1
```

## 配置项

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `weather_token` | 空 | 彩云天气 token，必填 |
| `latitude` / `longitude` | `null` | 显式坐标；填了就跳过系统/IP 定位，最高精度 |
| `coordinate_system` | `gcj02` | config 坐标的坐标系：`gcj02`（高德/腾讯）或 `wgs84`（GPS/Google） |
| `receiver` | 空 | 小鲁班接收人 uid，必填 |
| `auth` | 空 | 小鲁班认证 token，必填 |
| `sender` | 空 | 可选发送人标识 |
| `interval_minutes` | `5` | 检测周期（分钟），建议 5 |
| `rain_threshold` | `0.08` | 有效降雨阈值（mm/h） |
| `dry_confirm_count` | `3` | 连续多少次无雨才判雨停 |
| `min_push_interval_minutes` | `30` | 两次「开始下雨」通知的最小间隔，`0` 关闭 |
| `request_timeout_seconds` | `10` | 单次 HTTP 超时 |
| `location_cache_minutes` | `60` | 系统/IP 定位缓存时长，`0` 表示不缓存 |
| `api_base` | `https://api.caiyunapp.com` | 彩云根地址（调试可指向 mock） |
| `push_url` | `http://xiaoluban.rnd.huawei.com:80/` | 小鲁班推送地址 |
| `proxy` | `none` | 代理模式：`none` 直连（默认）/ `system` 系统代理 / `http://host:port` |
| `proxy_bypass` | `localhost,127.0.0.1,*.huawei.com` | 命中这些域名时强制直连 |
| `log_level` | `INFO` | 日志级别 |

环境变量可覆盖敏感项（优先级高于 `config.json`）：

| 环境变量 | 覆盖项 |
| --- | --- |
| `CAIYUN_TOKEN` / `CAIYUN_WEATHER_TOKEN` | `weather_token` |
| `XIAOLUBAN_RECEIVER` | `receiver` |
| `XIAOLUBAN_AUTH` | `auth` |
| `XIAOLUBAN_SENDER` | `sender` |
| `RAIN_LATITUDE` / `RAIN_LAT` | `latitude` |
| `RAIN_LONGITUDE` / `RAIN_LON` | `longitude` |
| `CAIYUN_API_BASE` | `api_base` |
| `XIAOLUBAN_URL` | `push_url` |

## 定位策略（三级降级）

1. **显式配置（最高精度）**
   `config.json` 中 `latitude` / `longitude` 同时为有效数值时直接使用。
   这是唯一能稳定保证街道/百米级精度的方式。

   **坐标系很重要**：彩云在中国大陆要求 GCJ-02，而 Windows/macOS 原生定位和
   IP 归属地返回 WGS84。两者在杭州一带相差约 **500 米**，不转换会明显偏楼。
   插件会自动把系统/IP 坐标转成 GCJ-02；`config.json` 里的坐标默认按 GCJ-02 解释，
   如果你填的是 GPS/Google 的 WGS84 坐标，请设置 `"coordinate_system": "wgs84"`。
   香港/澳门/台湾等地区如坐标有偏差，建议直接配置坐标。

2. **系统原生定位（高精度，但依赖权限与 Wi-Fi）**
   - Windows：调用 `Windows.Devices.Geolocation.Geolocator`，基于周围 Wi-Fi 定位。
     需要开启「设置 → 隐私和安全性 → 定位」，并允许桌面应用访问位置。
   - macOS：优先使用 `CoreLocationCLI`（`brew install corelocationcli`），
     否则尝试用 `swift` 运行内置脚本。命令行工具没有 Info.plist，系统可能不弹授权框。

3. **公网 IP 兜底（粗精度）**
   请求 `http://ip-api.com/json?lang=zh-CN`，取 `lat` / `lon`。
   精度通常只到城市级；如果使用 VPN/代理，结果可能完全错误。

> 实测提示：本机 Windows 定位可返回约 200 米精度的坐标，但偶尔会跳变；
> 若你要求长期稳定的街道级判定，请填写显式坐标。

位置缓存：系统/IP 定位结果会写入 `runtime_state.json` 的 `location` 字段，
默认缓存 60 分钟（IP 结果最多缓存 30 分钟），避免每 5 分钟触发一次定位。
缓存的是已经转换好的 GCJ-02 坐标，不会重复偏移。

## 状态机与防抖

状态只包含 `DRY`（无雨）和 `RAINING`（降雨中）。

| 当前状态 | 条件 | 结果 |
| --- | --- | --- |
| `DRY` | `intensity >= 0.08` | 切到 `RAINING`，推送「开始下雨」 |
| `RAINING` | `intensity >= 0.08` | 保持，`dry_counter` 清零 |
| `RAINING` | `intensity < 0.08` | `dry_counter + 1` |
| `RAINING` | `dry_counter >= 3` | 切到 `DRY`，推送「雨停了」，计数清零 |

一次典型的雨停防抖（5 分钟周期）：

| 时间 | 强度 | dry_counter | 状态/动作 |
| --- | --- | --- | --- |
| 20:00 | 0.50 | 0 | DRY → RAINING，推送开始 |
| 20:05 | 0.30 | 0 | RAINING，无推送 |
| 20:10 | 0.02 | 1 | RAINING，无推送 |
| 20:15 | 0.01 | 2 | RAINING，无推送 |
| 20:20 | 0.00 | 3 → 0 | RAINING → DRY，推送雨停 |

冷却规则：

- 「开始下雨」通知受 `min_push_interval_minutes` 限制，避免短时误报刷屏；
- 若本次开始通知因冷却被抑制，则雨停时也**不**推送，避免「没收到开始却收到结束」；
- 雨停通知不受冷却限制（只要开始通知成功发出过）。

推送采用「先成功、后提交」：如果 `push_client.py` 失败，本轮状态回滚，
下一轮仍会检测到同一次跃迁并重试，不会把事件吞掉。

## 运行方式

```powershell
# 单轮（计划任务使用）
py -3 rain_monitor.py --once

# 常驻循环（前台/终端，按固定间隔执行）
py -3 rain_monitor.py --loop

# 查看状态（不联网）
py -3 rain_monitor.py --status

# 发送测试消息
py -3 rain_monitor.py --test-push

# 干跑（不推送、不写状态）
py -3 rain_monitor.py --once --dry-run --verbose

# 临时使用其他配置/状态/日志目录
py -3 rain_monitor.py --once --config D:\rain\config.json --state D:\rain\state.json --log-dir D:\rain\logs
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功（包括「无状态跳变」和「锁冲突跳过」） |
| `1` | 运行期错误（网络、接口、定位、推送失败） |
| `2` | 配置缺失或非法 |

macOS / Linux 可用 cron 或 launchd 调度同一入口（注意 macOS 的命令行定位权限限制）：

```cron
*/5 * * * * cd /path/to/rain_info_send_via_xiaoluban && /usr/bin/python3 rain_monitor.py --once
```

## 系统代理与计划任务

Windows 上 Python 的 `urllib` 默认会读取注册表里的系统代理。计划任务在代理客户端
未授权的上下文运行时，可能出现“拒绝套接字创建 / socket access permission”错误。
这不是小鲁班或彩云的问题，而是请求被转发到了代理。

插件默认 `"proxy": "none"`，即**显式直连并忽略系统代理和 `HTTP_PROXY` 环境变量**，
因此开启 Windows 系统代理不再影响定时任务，也不需要为了定时任务关闭代理。

如果公司网络必须通过代理访问外网，可以改成：

```json
"proxy": "system",
"proxy_bypass": ["localhost", "127.0.0.1", "*.huawei.com"]
```

或手动指定：

```json
"proxy": "http://127.0.0.1:7890",
"proxy_bypass": ["localhost", "127.0.0.1", "*.huawei.com"]
```

规则：

- `proxy_bypass` 命中的域名强制直连，小鲁班内网域名应放在这里；
- `system` 读取注册表/环境变量里的 ProxyServer，不支持 PAC 脚本，PAC 环境请填手动地址；
- 仅支持 HTTP/HTTPS 代理，不支持 SOCKS5/NTLM；
- `push_client.py` 也支持 `--proxy` / `--proxy-bypass`。

## 测试

```powershell
py -3 -m unittest discover -s tests -v
```

- `tests/test_state_machine.py`：阈值、防抖、冷却、状态文件兼容、回滚
- `tests/test_providers.py`：`/weather`、minutely/realtime 解析、单位与强度分级
- `tests/test_geo.py`：WGS84 → GCJ-02 转换与配置坐标系
- `tests/test_net.py`：代理模式、直连 bypass、默认忽略系统代理
- `tests/test_e2e_mock.py`：本地 mock 彩云 + mock 小鲁班，走完整子进程链路
  （包括经纬度顺序、无 minutely 时用 realtime、设置 HTTP_PROXY 后仍直连、推送失败回滚重试、`--status` 无需凭据）

## 常见问题

**日志出现「彩云响应无 minutely 块」**
彩云账号只有基础套餐，`minutely` 属于企业增值服务。插件使用 `/weather` 综合接口，
同一响应里没有 `minutely` 时会自动使用 `realtime`，起止提醒不受影响，无需额外配置。
若需要未来两小时逐分钟描述，在彩云控制台开通分钟级降水权限即可，代码会自动优先使用。

**计划任务报「拒绝套接字创建 / socket access permission」**
通常是 Windows 系统代理导致的。插件默认 `proxy: none` 直连，已忽略系统代理；
若仍失败，检查 `config.json` 是否被改成 `system`，或代理软件是否做了全局透明代理/防火墙拦截。
可先用 `py -3 rain_monitor.py --once --verbose` 复现。

**彩云返回 `HTTP 429` / `Rate limit exceeded`**
触发了彩云限流。本轮会直接跳过、不写状态，下一个 5 分钟周期再试；
不要在同一分钟内反复手动运行。

**彩云返回 `token is invalid`**
token 未填写或已失效，检查 `weather_token` / `CAIYUN_TOKEN`。

**定位结果在城市之间跳变 / 明显不对**
系统定位可能受 Wi-Fi、VPN、代理影响。填写 `latitude` / `longitude` 即可锁定位置。

**Windows 定位不可用**
打开「设置 → 隐私和安全性 → 定位」，开启定位服务与「让桌面应用访问你的位置」，
再执行 `helpers\geolocate_windows.ps1` 单独验证。

**计划任务 Last Result 为 2 或 1**
`2` 通常是配置缺失（先跑 `--test-push`）；`1` 查看 `logs\monitor.log`。
注意任务使用 `pythonw.exe`，控制台输出不会出现，只能看日志。

**Windows PowerShell 5.1 里日志中文乱码**
日志是 UTF-8。用 `Get-Content logs\monitor.log -Encoding UTF8`，
或使用 PowerShell 7 / VS Code 打开。

**修改了配置需要重装任务吗？**
不需要。每一轮都会重新读取 `config.json`。

**如何避免任务重叠执行？**
程序使用 `logs\.monitor.lock` 排他锁；上一轮超过 15 分钟仍未结束会被视为陈旧锁。

## 安全与隐私

- `config.json` 含彩云 token 和小鲁班 token，已加入 `.gitignore`，不要提交。
- `.gitignore` 现在忽略所有 `config*.json`，只保留无密钥的 `config.example.json`；
  但仍要检查 `git status`，不要把本地配置副本提交上去。
- **如果 token 曾经被提交到 git**：仅删除文件不会清除历史，请立即到彩云/小鲁班
  控制台轮换 token，并用 `git filter-repo` 或 BFG 清理历史后强制推送；公开仓库尤其要尽快处理。
- `runtime_state.json` 含最近坐标，也已忽略。
- 日志不会记录 token；请求异常信息会把 token 替换为 `***`。
- Level 3 会把你的公网 IP 发送给 `ip-api.com`；不希望这样请配置显式坐标或关闭系统定位。
- 所有数据只在本机与小鲁班/彩云之间传输，不经过其他服务器。
