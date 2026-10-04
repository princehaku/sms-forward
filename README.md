# Air724UG 集中式短信转发

板载 Air724UG 负责接收短信、拒接呼入电话、持久化待发队列，并把设备状态、
短信和来电记录上报到中心：

`https://bytegallop.com/sms`

中心服务保存所有设备、短信、来电和转发记录，再按网页中配置的通道组转发到
飞书、企业微信群、手机号或通用 Webhook。控制台也可以创建下行短信任务，由
指定板子使用 SIM 卡发送。

## 目录结构

根目录只保留项目文档和仓库配置，代码按用途存放：

```text
device/       Air724UG 板端 Lua 代码和本地配置
server/       中心服务、控制台及服务端测试
tests/        板端 Lua 测试
tools/        维护和诊断脚本
```

首次配置时，将 `device/config.example.lua` 复制为 `device/config.lua` 并填写
本地配置；`config.lua` 已被 Git 忽略。

## 架构

1. 板子使用 IMEI 作为设备 ID，启动后调用 `/api/device/register`。
2. 板子每 60 秒调用 `/api/device/heartbeat`，上报网络、信号和待发数量。
   心跳同时携带业务脚本版本 `app_version` 和底层固件版本 `firmware`，控制台
   “设备”页面会持续显示最近一次上报值。
3. 收到短信后先写入 `/ldata/sms_center_outbox.json`。
4. 板子调用 `/api/messages` 上报短信；中心确认后才删除板端队列项。
5. 呼入电话触发后，板子立即挂断；断开后将来电记录写入同一持久化队列，并调用
   `/api/device/missed-call` 上报。
6. 中心按设备、通道组和关键词规则记录逐目标投递结果，符合条件的任务交给转发
   工作线程处理。
7. 板子每 60 秒轮询一次下行短信；空队列响应只有 `{"code":0}`。
8. 板子发送下行短信后，将结果写入
   `/ldata/sms_center_command_results.json`，中心确认后再删除结果记录。
9. 管理员通过 `https://bytegallop.com/sms/` 分页查看短信、来电和转发结果。

设备接口使用固定 Token `sms-sb`。板端同时在 JSON 的 `token` 字段和 `X-SMS-Token` 请求头中携带该值。

## 板端配置

本地 `device/config.lua`：

```lua
return {
    center_url = "https://bytegallop.com/sms",
    center_token = "sms-sb",
    device_name = "GK21.5PTM",
    phone_number = "",
    device_id = "",

    queue_file = "/ldata/sms_center_outbox.json",
    command_result_file = "/ldata/sms_center_command_results.json",
    heartbeat_interval_ms = 60000,
    command_poll_interval_ms = 60000,
    command_send_timeout_ms = 600000,
    network_watchdog_ms = 900000,
    enable_missed_call_forwarding = true,
    ota_enabled = false,
    ota_product_key = "",
    ota_startup_delay_ms = 120000,
    ota_check_interval_ms = 21600000,
    ota_busy_retry_ms = 60000,
    ota_watchdog_ms = 1800000
}
```

IMEI 会自动获取。模组的 `AT+CNUM` 经常无法返回 SIM 手机号，因此 `phone_number` 可以留空，再到中心控制台给设备补录号码和名称。

板端启动后持续等待数据网络；已经联网后若收到掉网事件，也会重新开始计时。连续
15 分钟没有可用数据 IP 时调用 `sys.restart("SMSCENTER_NETWORK_WATCHDOG")`，恢复
`IP_READY_IND` 后立即清零计时。`network_watchdog_ms` 缺省值为 `900000`。

## 自动升级

远程 OTA 当前默认禁用。`ota_enabled = false` 时，板端不会加载 Air724UG 的
`update` 库，也不会向合宙 IoT 平台发起升级检查。以后需要恢复时，将
`ota_enabled` 改为 `true`，并把平台分配的 Product Key 只写入本地 `device/config.lua`
的 `ota_product_key`；示例配置保持为空，密钥不能写入日志、文档或版本库。

- 开机联网后延迟检查，默认每 6 小时再检查一次。
- 有短信或来电待上报、下行短信正在发送、结果待确认时，检查顺延 60 秒。
- 下载完成后自动重启并启用新版本；无升级包时继续按周期检查。
- 平台项目名必须保持为 `SMSCENTER`，发布包版本应高于板端 `VERSION`。
- 合宙平台需要按设备 IMEI 分配升级包；脚本包可更新 Lua 业务代码，Core
  差分包可更新底层固件。

## 下载到 Air724UG

Luatools 项目从 `device/` 目录添加以下文件；已有项目需将旧根目录路径更新为新路径：

- `device/main.lua`
- `device/sms_center.lua`
- `device/sms_center_core.lua`
- `device/config.lua`

下载到模组时保留文件名，将这四个文件放在同一个脚本目录中，模块加载名保持不变。
`device/config.example.lua` 仅供本地配置参考，无需下载。

在仓库根目录执行板端语法检查和测试：

```powershell
Get-ChildItem device/*.lua, tests/*.lua | ForEach-Object {
    & .tmp/Luatools/_temp/tools/luac.exe -p $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Lua syntax check failed: $($_.Name)" }
}
Get-ChildItem tests/test_*.lua | ForEach-Object {
    lua $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Lua test failed: $($_.Name)" }
}
```

运行测试需要本机 Lua 解释器；测试路径以仓库根目录为基准。

启用“添加默认扩展库”和 USB trace，下载脚本后重启模块。日志出现以下内容说明运行正常：

```text
smsCenter.device online
smsCenter.send accepted
smsCenter.outbound accepted
smsCenter.outbound sent
```

旧版 `script_LuaTask_V2.4.4` 的短信库会先把 UCS2 正文转换成 GB2312，遇到扩展字符时可能得到空字符串。首次使用或重新安装 Luatools 后，先执行兼容补丁：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\patch_air724_sms_decoder.ps1
```

补丁在 GB2312 转换为空时回退到 UTF-8，并同时覆盖普通短信和长短信合并路径。补丁执行后再在 Luatools 中下载脚本。调试时若通过 AT 口临时使用 `AT+CMGF=1` 发送文本短信，发送结束后要恢复 `AT+CMGF=0` 和 `AT+CSCS="UCS2"`，否则旧版接收库无法解析后续 PDU。

## 中心服务

服务端代码位于 `server/`，使用 Flask、SQLite、Uvicorn 和单独的转发工作线程。容器只监听服务器本机 `127.0.0.1:8787`，公网入口由 Nginx 的 `/sms/` 路由提供。

中心数据保存在 SQLite：

- 使用 `server/compose.yml` 部署时，容器内路径为 `/data/sms-center.db`，
  对应宿主机 `server/data/sms-center.db`。
- 直接运行 `server/app.py` 时，默认路径也是 `server/data/sms-center.db`；
  可通过 `DATABASE_PATH` 环境变量覆盖。
- 数据库保存设备、短信、来电、Chrome Web Push 订阅与推送结果、转发目标、
  通道组、短信转发模板、逐条转发记录和下行短信任务。
  成功转发时间保存在 `deliveries.delivered_at`；控制台发送记录保存在
  `outbound_sms`。
- 板端尚未被中心确认的短信另存于 `/ldata/sms_center_outbox.json`，中心确认
  接收后才会从板端队列删除。
- 板端未成功回报的下行结果保存在
  `/ldata/sms_center_command_results.json`，重启后继续回报。

本地测试：

```powershell
$env:PYTHONPATH = ".tmp\server_pydeps"
python -m unittest discover -s server\tests -v
```

部署：

```bash
cd /root/apps/sms-center
docker compose up -d --build
```

控制台管理 Token 默认也为 `sms-sb`，登录后保存在当前浏览器的 `localStorage`。

### Chrome 消息通知

管理员登录控制台后，在“总览”的“Chrome 消息通知”中选择“新短信”、
“未接来电”，再点击“在此 Chrome 开启通知”。Chrome 授权成功后会立即收到
一条测试通知；后续即使控制台页面关闭，Service Worker 仍可显示系统通知。
每个 Chrome 配置文件需要单独订阅，可在同一区域发送测试通知或关闭当前浏览器
的订阅。

Web Push 使用持久化 VAPID 密钥。Docker 部署默认在数据卷中自动创建
`/data/vapid_private.pem`，不得删除或加入版本库；更换该密钥后，现有 Chrome
需要重新订阅。可通过 `VAPID_SUBJECT` 设置 VAPID 联系地址。Chrome 通知要求
通过 HTTPS 访问控制台。

## MCP

公网 Streamable HTTP 地址：

```text
https://bytegallop.com/sms/mcp/
```

MCP 使用独立的 Bearer Token，不能使用设备 Token 或控制台管理 Token。管理员登录控制台后，在“MCP”页面填写名称和有效期并创建 Token。完整 Token 会保存在服务端，并持续显示在管理表格中；可在同一页面复制 Token、查看最后使用时间和过期时间，也可以撤销或永久删除。永久删除会同时清除完整 Token、SHA-256 哈希和使用时间记录。所有现有和新建 Token 的权限统一为 `read,send`。

客户端请求头：

```text
Authorization: Bearer <MCP Token>
```

当前提供七个查询工具：

- `get_system_status`：服务统计和设备在线状态
- `list_devices`：设备、SIM 手机号和最近心跳
- `search_messages`：查询接收短信
- `list_missed_calls`：查询自动挂断的呼入电话
- `list_forwarding_records`：查询转发记录，包含关键词命中状态和匹配详情
- `list_outbound_sms`：查询控制台下行短信状态
- `get_routing_summary`：查询通道组及短信关键词、目标元数据和直连路由

另提供一个会产生真实下行任务的工具：

- `send_sms`：选择 Air724UG 设备、收件号码和正文后加入发送队列。调用时必须
  提供稳定且唯一的 `request_id`；相同参数重试会返回原任务，复用同一
  `request_id` 发送不同内容会被拒绝。短信由设备异步发送，并可能产生运营商费用。

还提供 `sms-center://status` 和 `sms-center://devices` 两个只读资源。MCP 不提供
重试、删除或修改配置的工具，路由摘要也不会返回 Webhook URL、请求头或应用密钥。

## 转发通道

控制台支持五类目标：

- `feishu_app`：填写 `app_id`、`app_secret`、`receive_id` 和 `receive_id_type`；
  `template_id` 可指定短信通知模板，省略时跟随默认模板
- `feishu_webhook`：填写飞书群自定义机器人完整 URL；`template_id` 的规则与
  `feishu_app` 相同
- `wecom_webhook`：填写企业微信群机器人的完整 Webhook URL
- `sms_forward`：填写目标号码 `recipient`；`sender_device_id` 留空时使用收到
  原短信的板子，填写设备 ID 时改由指定板子发送；`template_id` 可指定短信
  转发模板，省略时跟随默认模板
- `webhook`：填写任意 HTTPS URL，可附带请求头

通道组可以包含多个设备和多个转发目标，因此多个手机号可以共用同一套转发方式。
每条短信或来电对每个目标都有独立的投递状态、次数、转发时间和错误信息。

### 通道组短信关键词

在通道组中填写短信关键词，每行一个。中心按原短信正文进行字面包含匹配，
英文字母忽略大小写；命中任意一个关键词即可通过该通道组转发。关键词留空时
沿用全部短信转发，已有通道组升级后默认留空。来电通知沿用通道组的设备和目标
配置，不参与短信关键词匹配。
最多支持 50 个去重后的关键词，每个最多 100 字；匹配采用普通文字，不支持正则。
接口的 `keywords` 为字符串数组，每个元素只允许单行文字。

同一目标关联多个通道组时，只要任一组命中或未设置关键词，就会生成一条投递
任务；同一短信不会因多个组命中而重复发送。旧版直连路由独立生效，因此同时
配置直连路由时，即使相关通道组的关键词均未命中，也可以经直连路由转发。
转发记录中的 `direct_route` 标记会显示该途径。

未命中的短信仍会归档，对应目标的转发记录为 `filtered`，不会创建手机号下行
任务或进行网络投递，也不能通过失败重试发送。记录保留接收时的通道组名称、
关键词和实际命中词快照，之后修改或删除通道组不会改写历史，也不会自动补发
历史短信。更新通道组接口省略 `keywords` 时保留原配置，传入空列表即可清空
过滤规则。

转发记录的 `keyword_status` 表示关键词匹配结果：`matched` 为命中，`unmatched`
为未命中，`unrestricted` 为存在不限关键词的通道组或直连路由，`not_applicable`
为来电通知，`legacy`
为升级前未记录匹配结果的历史投递。`keyword_matches` 提供各通道组的配置和
匹配详情；实际是否投递同时取决于转发状态及是否存在直连路由。

## 来电转发

板子不提供电话接听能力。`CALL_INCOMING` 到达后会立即调用 `cc.hangUp`，等
`CALL_DISCONNECTED` 到达后生成一条来电记录。来电事件和短信在控制台分开显示，
转发时共用现有通道组：

- 飞书、企业微信和群 Webhook 收到“未接来电”通知。
- 手机号目标收到一条固定格式的提示短信，包含接收卡、来电号码和中心入库时间。
- 通用 Webhook 的事件名为 `call.missed`，并携带呼入、挂断时间与响铃秒数。

来电记录先写入 `/ldata/sms_center_outbox.json`。断网、中心不可达或设备重启后会
继续上报；中心以 `call_id` 去重，已经接收的同一次来电不会重复生成投递任务。

顶层“短信模板”页面提供短信转发和飞书短信通知模板管理，可新增、编辑、设为默认和
删除未被引用的模板。默认模板保持原有格式，支持以下占位符：

- `{{ori}}`：原短信发送号码
- `{{sms}}`：原短信正文
- `{{receiver}}`：接收原短信的板载 SIM 手机号
- `{{phone}}`：手机号转发时表示实际发送设备 SIM；飞书通知时表示接收原短信的
  设备 SIM
- `{{time}}`：原短信时间
- `{{device}}`：接收设备名称

手机号目标的模板渲染结果会作为新的下行短信进入现有 60 秒轮询队列；飞书目标会
把渲染结果直接作为群消息正文。短信转发记录会等待 Air724UG 的真实发送结果，模组返回成功
后才标记为完成；失败或结果未知时不会自动重发，需要管理员确认后手动操作。
目标号码不能与任何已登记板载 SIM 相同，避免短信回到中心后循环转发。短信发送
会产生运营商费用，长内容可能拆分为多条计费。

## 飞书群回复短信

`feishu_app` 可以接收群成员对机器人短信转发消息的文本回复，并把回复内容加入
现有真实 SMS 下行队列。系统通过飞书消息的 `parent_id`、`root_id` 和发送接口
返回的 `message_id` 关联原短信，因此普通群消息、私聊消息、非文本消息和机器人
自己的消息都不会触发 SMS。同一个飞书事件只会生成一个下行任务。

服务端默认每 10 秒通过“获取会话历史消息”接口读取一次目标群，轮询仅处理首次
建立游标后的新消息，因此重启或升级不会追发旧回复。飞书 App 需要拥有
`im:message`、`im:message:readonly` 或历史消息读取权限之一。

事件推送是可选的低延迟通道，配置步骤：

1. 在目标的配置 JSON 中增加 `verification_token`，其值来自飞书开发者后台
   “事件与回调 > 加密策略”页面；当前回调实现要求 Encrypt Key 保持未启用。
2. 在飞书事件订阅中填写请求地址
   `https://bytegallop.com/sms/api/integrations/feishu/events`。
3. 订阅 `im.message.receive_v1`，确保机器人在目标群内，并发布应用版本。

轮询和事件推送共享消息 ID 去重，二者同时启用也只会产生一条 SMS。群回复默认
使用接收原短信的板子，收件号码为原短信发送号码。收到群回复后，服务端会先在
群成员的回复消息下回复“已读，正在处理。”并添加 `OK` 表情；两项确认成功后才
生成 SMS 下行任务。最终成功、失败或状态未知结果会用文字回复到飞书消息线程。
失败和状态未知任务不会自动重发。

## 控制台发送短信

“发短信”页面选择发送设备、填写收件手机号和内容后，会创建一条
`pending` 任务。板子领取后状态变为 `sending`，Air724UG 回报成功后变为
`sent`。

- `failed` 表示模组明确回报发送失败。
- `unknown` 表示板子领取任务后长时间没有回报结果。系统不会自动重发，
  防止收件人收到重复短信。
- `failed` 和 `unknown` 可以在控制台手动重新入队；操作前应先向收件人核实。
- 服务器无法直接连接移动网络后的板子，因此从入队到领取最多约 60 秒。

### 设备转发开关与电话模板

设备列表支持分别开关短信转发和电话通知转发。关闭后继续归档，新事件不再生成转发任务；已入队任务继续处理。已有设备升级后默认保持开启，接收目标沿用通道组配置。

“转发模板”页面分别提供短信模板和电话转发模板。电话模板为全局模板，用于飞书、企业微信群及短信来电通知，通用 Webhook 继续使用结构化事件。支持现有六种占位符，来电的 `{{time}}` 使用入库时间；短信通知仍遵守 1000 字限制。
