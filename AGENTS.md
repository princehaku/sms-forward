# SMS Forward 项目维护说明

## 沟通规则

- 不要用“不是……而是……”造句。

## 项目与硬件

- 项目用途：使用 Air724UG 接收短信并按通道组转发到飞书或企业微信群，
  同时向服务端上报设备心跳；控制台可通过指定 Air724UG 下发短信。
- 主板型号：`GK21.5PTM_rev0.3`，板上模组为 `Air724UG-NFM`。
- 当前测试模组 IMEI：`861714054279207`。
- 当前 SIM 为中国电信卡。运营商归属应以 `AT+CIMI`、`AT+COPS?` 等实时查询结果为准。
- 飞书 App Secret、手机号等敏感信息不要写入本文档，也不要输出到日志或提交到版本库。

## 固件

- 当前已验证可用的底层固件：
  `LuatOS-Air_V4035_RDA8910_TTS_NOLVGL_FLOAT`
- 中国电信卡发送短信需要 VoLTE。使用
  `LuatOS-Air_V4035_RDA8910_TTS_NOVOLTE_FLOAT` 时，SIM 注册、联网、
  短信接收和 HTTPS 心跳可以正常工作，但下行短信会在 `AT+CMGS`
  后等待约 30 秒并返回 `ERROR`。
- 使用 VoLTE 固件后，`AT+CIREG?` 应显示 IMS 已注册；实测返回
  `+CIREG: 0, 1, 5`，下行短信返回 `+CMGS: 0` 和 `OK`。
- 模组刷机前使用过：
  `Luat_V3037_RDA8910_TTS_NOVOLTE_FLOAT`
- V4 固件线存在回退限制，不要尝试降回 V3037。
- 当前 SIM 注册、联网、短信收发、转发和 HTTPS 心跳均可在 V4035 上正常工作。
- 日常业务或指示灯调整只需下载 Lua 脚本，无需重复刷底层固件。
- 保持 `main.lua` 中的 `AT+RNDISCALL=0,1`，避免 USB 进入 RNDIS 模式后影响日志和下载。

## 板载指示灯

- 用户照片红圈中的指示灯在刷机前可以闪烁。
- 经逐脚扫描确认，该灯连接到 Air724UG：
  - 模组物理引脚：`53`
  - 复用功能：`SPI1_DIN / GPIO12`
  - Lua 引脚：`pio.P0_12`
- 正式配置使用：

  ```lua
  require "netLed"
  netLed.setup(true, pio.P0_12)
  ```

- GPIO12 属于 `V_GLOBAL_1V8` 电压域，无需为这盏灯调用
  `pmd.ldoset(..., pmd.LDO_VLCD)`。
- 已测试且未驱动该灯的候选包括：
  - 物理 49：`MODULE_STATUS / GPIO5 / pio.P0_5`
  - 物理 56：`LCD_RST / GPIO6 / pio.P0_6`
  - 物理 57：`NET_MODE / GPIO4 / pio.P0_4`
  - 物理 58：`NET_STATUS / GPIO1 / pio.P0_1`
  - 顶边物理 52、54、55 对应的 GPIO9、GPIO10、GPIO11
- 不要再次对上述引脚进行盲扫；板上“纸巾余量、机芯信号”等接口可能复用相邻 GPIO。

## 下载与验证

- Lua 语法检查：

  ```powershell
  .tmp\Luatools\_temp\tools\luac.exe -p .\main.lua
  ```

- 下载完成后检查 Luatools 日志同时出现：
  - `DownLoad Passed`
  - `下载成功`
- 重启后继续检查 trace：
  - SIM 已就绪并完成网络注册。
  - 出现 `IP_READY_IND`。
  - 出现 `smsCenter.device online ...`。
  - 下行短信成功时依次出现 `smsCenter.outbound accepted` 和
    `smsCenter.outbound sent`。
  - 不再出现 `smsCenter.ledScan`，扫描代码只允许临时诊断使用。
- COM 号会随重启变化，每次操作前重新枚举。曾观察到的正常端口类型包括
  AP Diag、CP Diag、Modem 和 AT。

## 修改原则

- 保留短信离线队列、心跳、通道组和下行短信逻辑，指示灯修改应局限在
  `main.lua` 的 `netLed` 配置。
- 下行短信使用 60 秒轮询，空响应保持为 `{"code":0}`。任务领取后不能自动
  重发；长时间无结果应标记为 `unknown`，由管理员确认后手动重试。
- `sms_forward` 转发目标通过现有 `outbound_sms` 队列发送，使用
  `source_delivery_id` 保证同一转发记录只生成一个下行任务。模组结果必须同步回
  原转发记录。
- 短信转发默认使用收到原短信的设备；配置 `sender_device_id` 后可改用指定设备。
  目标号码不得匹配任何已登记板载 SIM，避免循环转发。
- 短信转发和飞书短信通知模板保存在 `sms_templates`，支持 `{{ori}}`、`{{sms}}`、
  `{{receiver}}`、`{{phone}}`、`{{time}}` 和 `{{device}}`。其中
  `{{phone}}` 在手机号转发时表示实际发送设备 SIM，在飞书通知时表示接收原短信的
  设备 SIM。`sms_forward`、`feishu_app` 和 `feishu_webhook` 未指定
  `template_id` 时使用当前默认模板；手机号转发渲染后仍需遵守 1000 字下行限制。
- 自动短信转发失败或结果未知后不得自动重发。删除关联的待发送任务时，应把原
  转发记录标记为失败。
- 飞书群回复 SMS 支持每 10 秒读取群历史消息，并可同时接收 `feishu_app` 的
  `im.message.receive_v1` 事件；事件推送必须通过 Verification Token 校验来源。
  首次轮询只建立已读游标，不追发旧回复。仅当群成员回复已关联的机器人短信消息
  时入队；普通群消息、私聊、非文本和机器人消息必须忽略，消息 ID 必须去重。
- 飞书回复生成的 SMS 使用原短信接收设备和原发送号码。收到回复后先在原飞书
  消息下回复“已读，正在处理。”并添加 `OK` 表情 Reaction；两项确认成功后才
  允许生成 SMS 下行任务。最终发送状态通过后台通知任务回复到原飞书线程；
  最终通知失败不得影响 SMS 下行状态。
- 板端下行结果持久化到 `/ldata/sms_center_command_results.json`，中心确认
  收到结果后才能删除。
- 本机不支持接听电话。板端收到 `CALL_INCOMING` 后立即调用 `cc.hangUp`，
  `CALL_DISCONNECTED` 后把 `missed_call` 事件写入现有持久化队列，再上报
  `/api/device/missed-call`。中心以 `device_id + call_id` 去重。
- 短信和来电在控制台分开显示，转发共用通道组。飞书群回复 SMS 只允许关联
  `event_type='sms'` 的投递，来电通知不能触发回复短信。
- 不要提交 `config.lua`、Luatools 日志、诊断转储或任何密钥。
- 修改后至少执行 Lua 语法检查；涉及模组运行行为时，还要完成下载和联网心跳验证。

## 自动升级

- 自动升级使用 Air724UG 自带的 `update` 库和合宙 IoT 平台。Product Key 只允许
  保存在被忽略的 `config.lua`，禁止写入日志、文档和版本库。
- `_G.PROJECT` 保持为 `SMSCENTER`；每次发布都严格递增 `_G.VERSION`，不要发布
  低于板端版本的升级包。
- 开机延迟检查，默认每 6 小时检查一次。短信、来电、下行任务或结果队列繁忙时
  顺延检查，不能打断业务队列。
- `update.request` 下载成功后调用 `sys.restart`。升级失败或没有新版本时保留
  现有业务版本并继续按周期检查。
- 合宙平台可以发布 Lua 脚本全量升级包和 Core 差分包；设备 IMEI、项目名、版本
  和底层固件必须与平台升级配置匹配。

## MCP

- 公开入口为 `https://bytegallop.com/sms/mcp/`，传输方式为 Streamable HTTP。
- MCP 提供设备、接收短信、来电记录、转发记录、下行状态、路由摘要查询和
  `send_sms` 真实短信发送工具。发送必须提供稳定的 `request_id`，服务端用它
  保证客户端重试时只生成一个下行任务。不要添加重试、删除、Token 管理或配置
  修改工具。
- MCP Token 与设备 Token、控制台管理 Token 分开管理，所有现有和新建 Token
  权限统一为 `read,send`。
- 服务端同时保存完整 MCP Token 和 SHA-256 哈希；管理员登录控制台后可持续
  查看并复制全文。不要把完整 Token 输出到日志、飞书消息、文档或版本库。
- 控制台负责创建、列出、撤销和永久删除 MCP Token。永久删除时完整 Token、
  SHA-256 哈希和使用记录一并清除。部署验证使用临时 Token，握手和工具调用
  完成后立即永久删除。
