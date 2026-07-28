-- Central SMS client for Air724UG.
local core = require "sms_center_core"

local function resolveRuntimeModule(name, loaded)
    if type(loaded) == "table" then return loaded end
    return _G[name]
end

-- Literal requires let Luatools discover and package the dependencies.
local function loadSmsModule()
    return resolveRuntimeModule("sms", require "sms")
end

local function loadCallModule()
    return resolveRuntimeModule("cc", require "cc")
end

local function loadUpdateModule()
    return resolveRuntimeModule("update", require "update")
end

local function loadHttpModule()
    return resolveRuntimeModule("http", require "http")
end

local function loadWebsocketModule()
    return resolveRuntimeModule("websocket", require "websocket")
end

local function loadCommonModule()
    return resolveRuntimeModule("common", require "common")
end

local function loadMiscModule()
    return resolveRuntimeModule("misc", require "misc")
end

local function loadNetModule()
    return resolveRuntimeModule("net", require "net")
end

local function loadSocketModule()
    return resolveRuntimeModule("socket", require "socket")
end

local function loadJsonModule()
    if _G.json then return _G.json end
    if package and package.preload and package.preload.json then
        return package.preload.json("json")
    end
end

local configOk, config = pcall(require, "config")
local sms
local cc
local http
local websocket
local common
local misc
local net
local socket
local json

local queue = {}
local configReady = false
local sending = false
local sendTimerScheduled = false
local heartbeatTimerScheduled = false
local deviceRequestBusy = false
local registered = false
local commandPollTimerScheduled = false
local commandPollBusy = false
local commandSendBusy = false
local commandResultTimerScheduled = false
local commandResultBusy = false
local commandResults = {}
local wsClient
local wsConnected = false
local wsAuthenticated = false
local wsResultPending
local wsStatusTimerScheduled = false
local wsRetryWaitId = 0
local deviceId = ""
local heartbeatIntervalMs = 60000
local commandPollIntervalMs = 60000
local longConnectionEnabled = false
local longConnectionUrl = ""
local longConnectionStatusIntervalMs = 600000
local fallbackSyncIntervalMs = 120000
local longConnectionReconnectDelaysMs = {
    5000, 15000, 60000, 300000, 900000, 3600000
}
local centerUrl
local activeIncomingCall
local callSequence = 0
local otaUpdate
local otaEnabled = false
local otaProductKey = ""
local otaTimerScheduled = false
local otaCheckBusy = false
local otaGeneration = 0
local otaStartupDelayMs = 120000
local otaCheckIntervalMs = 21600000
local otaBusyRetryMs = 60000
local otaWatchdogMs = 1800000
local networkWatchdogMs = 900000
local networkReady = false
local networkWatchdogArmed = false
local networkWatchdogGeneration = 0
local trafficStatsEnabled = true
local trafficStatsIntervalSeconds = 60
local trafficSessionId = ""
local trafficSessionBytes = 0
local trySend
local sendHeartbeat
local pollOutboundSms
local tryReportCommandResult
local sendOutboundCommand
local runOtaCheck
local runLongConnection
local sendLongConnectionPayload
local sendLongConnectionStatus
local SMS_UTF8_PREFIX = "\2SMSCENTER_UTF8\2"

local function readAll(path)
    local file = io.open(path, "rb")
    if not file then return nil end
    local content = file:read("*a")
    file:close()
    return content
end

local function writeAll(path, content)
    local file = io.open(path, "wb")
    if not file then return false end
    local ok = file:write(content)
    file:close()
    return ok and true or false
end

local function persistQueue()
    if not configReady then return false end
    local encoded = json.encode(queue)
    if type(encoded) ~= "string" then
        log.error("smsCenter.queue", "encode failed")
        return false
    end
    if not writeAll(config.queue_file, encoded) then
        log.error("smsCenter.queue", "write failed")
        return false
    end
    return true
end

local function persistCommandResults()
    if not configReady then return false end
    local encoded = json.encode(commandResults)
    if type(encoded) ~= "string" then
        log.error("smsCenter.outbound", "result encode failed")
        return false
    end
    if not writeAll(config.command_result_file, encoded) then
        log.error("smsCenter.outbound", "result write failed")
        return false
    end
    return true
end

local function installSmsDecoderFallback()
    log.info(
        "smsCenter.decoder",
        "probe",
        type(common),
        type(common and common.ucs2beToGb2312),
        type(common and common.ucs2beToUtf8),
        tostring(common == _G.common)
    )
    if type(common) ~= "table"
        or type(common.ucs2beToGb2312) ~= "function"
        or type(common.ucs2beToUtf8) ~= "function"
        or common._smsCenterDecoderInstalled then
        log.warn("smsCenter.decoder", "not installed")
        return
    end
    local original = common.ucs2beToGb2312
    common.ucs2beToGb2312 = function(source)
        local ok, converted = pcall(original, source)
        if ok and type(converted) == "string" and converted ~= "" then
            return converted
        end
        local utf8Ok, utf8Text = pcall(common.ucs2beToUtf8, source)
        if utf8Ok and type(utf8Text) == "string" then
            log.info(
                "smsCenter.decoder",
                "ucs2 fallback",
                #(source or ""),
                #utf8Text
            )
            return SMS_UTF8_PREFIX .. utf8Text
        end
        return converted
    end
    common._smsCenterDecoderInstalled = true
    log.info("smsCenter.decoder", "installed")
end

local function loadQueue()
    local content = readAll(config.queue_file)
    if not content or content == "" then return end
    local decoded, ok, err = json.decode(content)
    if not ok or type(decoded) ~= "table" then
        log.error("smsCenter.queue", "invalid queue file", err or "")
        return
    end
    local discarded = 0
    for i = 1, #decoded do
        local item = decoded[i]
        local isSms = type(item) == "table"
            and (item.kind == nil or item.kind == "sms")
            and type(item.text) == "string"
            and item.text ~= ""
        local isMissedCall = type(item) == "table"
            and item.kind == "missed_call"
            and type(item.call_id) == "string"
            and item.call_id ~= ""
        if isSms or isMissedCall then
            item.kind = item.kind or "sms"
            item.attempts = tonumber(item.attempts) or 0
            queue[#queue + 1] = item
        else
            discarded = discarded + 1
        end
    end
    if discarded > 0 then
        log.warn("smsCenter.queue", "discarded invalid", discarded)
        persistQueue()
    end
    log.info("smsCenter.queue", "restored", #queue)
end

local function loadCommandResults()
    local content = readAll(config.command_result_file)
    if not content or content == "" then return end
    local decoded, ok, err = json.decode(content)
    if not ok or type(decoded) ~= "table" then
        log.error("smsCenter.outbound", "invalid result file", err or "")
        return
    end
    for i = 1, #decoded do
        local item = decoded[i]
        if type(item) == "table"
            and tonumber(item.id)
            and type(item.ok) == "boolean" then
            commandResults[#commandResults + 1] = item
        end
    end
    log.info("smsCenter.outbound", "results restored", #commandResults)
end

local function retryDelay(attempts)
    local delays = config.retry_delays_ms
    if type(delays) ~= "table" or #delays == 0 then return 60000 end
    local index = math.min(math.max(tonumber(attempts) or 1, 1), #delays)
    return tonumber(delays[index]) or 60000
end

local function refreshDeviceId()
    if deviceId ~= "" then return true end
    deviceId = core.trim(config.device_id)
    if deviceId == "" and misc and misc.getImei then
        deviceId = core.trim(misc.getImei())
    end
    return deviceId ~= ""
end

local function firmwareVersion()
    local version = core.trim(_G.CORE_VERSION)
    if version ~= "" then return version end
    if rtos and type(rtos.get_version) == "function" then
        local ok, runtimeVersion = pcall(rtos.get_version)
        if ok then return core.trim(runtimeVersion) end
    end
    return ""
end

local function onIpStatisReport(dataFlow)
    local bytes = tonumber(dataFlow)
    if not bytes or bytes <= 0 then return end
    trafficSessionBytes = trafficSessionBytes + math.floor(bytes)
end

local function volatileTrafficSessionId()
    local tick = 0
    if rtos and type(rtos.tick) == "function" then
        local ok, value = pcall(rtos.tick)
        if ok then tick = tonumber(value) or 0 end
    end
    return "volatile-" .. tostring(os.time()) .. "-" .. tostring(tick)
end

local function initializeTrafficStats()
    if not trafficStatsEnabled then return end
    local previous = tonumber(core.trim(readAll(config.traffic_session_file))) or 0
    local current = math.floor(previous) + 1
    if current > 2147483647 then current = 1 end
    if writeAll(config.traffic_session_file, tostring(current)) then
        trafficSessionId = tostring(current)
    else
        trafficSessionId = volatileTrafficSessionId()
        log.warn("smsCenter.traffic", "session file write failed")
    end
    sys.subscribe("LIB_IP_STATIS_RPT", onIpStatisReport)
    local ok, reason = pcall(socket.setIpStatis, trafficStatsIntervalSeconds)
    if not ok then
        log.warn("smsCenter.traffic", "statistics unavailable", tostring(reason))
    end
end

local function devicePayload()
    refreshDeviceId()
    local state = net and net.getState and net.getState() or ""
    local signal = net and net.getRssi and net.getRssi() or nil
    return {
        token = config.center_token,
        device_id = deviceId,
        name = config.device_name or "",
        phone_number = config.phone_number or "",
        firmware = firmwareVersion(),
        app_version = _G.VERSION or "",
        network = state or "",
        signal = signal,
        queue_count = #queue,
        traffic_session_id = trafficSessionId,
        traffic_session_bytes = trafficSessionBytes
    }
end

local function post(path, payload, callback)
    local body = json.encode(payload)
    if type(body) ~= "string" then
        callback(false, nil, {}, "", "encode failed")
        return
    end
    http.request(
        "POST",
        centerUrl .. path,
        {hostNameFlag = 1},
        {
            ["Content-Type"] = "application/json; charset=utf-8",
            ["Accept"] = "application/json",
            ["Connection"] = "close",
            ["X-SMS-Token"] = config.center_token
        },
        body,
        tonumber(config.http_timeout_ms) or 30000,
        callback
    )
end

local function responseOk(result, statusCode, body)
    return core.responseSucceeded(json, result, statusCode, body)
end

local function scheduleSend(delay)
    if not configReady or sendTimerScheduled then return end
    sendTimerScheduled = true
    sys.timerStart(function()
        sendTimerScheduled = false
        trySend()
    end, delay or 0)
end

local function scheduleHeartbeat(delay)
    if not configReady or heartbeatTimerScheduled then return end
    heartbeatTimerScheduled = true
    sys.timerStart(function()
        heartbeatTimerScheduled = false
        sendHeartbeat()
    end, delay or heartbeatIntervalMs)
end

local function scheduleCommandPoll(delay)
    if longConnectionEnabled
        or not configReady
        or commandPollTimerScheduled then
        return
    end
    commandPollTimerScheduled = true
    sys.timerStart(function()
        commandPollTimerScheduled = false
        pollOutboundSms()
    end, delay or commandPollIntervalMs)
end

local function scheduleCommandResult(delay)
    if not configReady or commandResultTimerScheduled then return end
    commandResultTimerScheduled = true
    sys.timerStart(function()
        commandResultTimerScheduled = false
        tryReportCommandResult()
    end, delay or 0)
end

local function scheduleOtaCheck(delay)
    if not configReady
        or not otaEnabled
        or otaProductKey == ""
        or otaTimerScheduled
        or otaCheckBusy then
        return
    end
    otaTimerScheduled = true
    sys.timerStart(function()
        otaTimerScheduled = false
        runOtaCheck()
    end, delay or otaCheckIntervalMs)
end

local function armNetworkWatchdog(reason)
    if not configReady or networkReady or networkWatchdogArmed then return end
    networkWatchdogArmed = true
    networkWatchdogGeneration = networkWatchdogGeneration + 1
    local generation = networkWatchdogGeneration
    log.warn(
        "smsCenter.network",
        "watchdog armed",
        tostring(reason or "unavailable"),
        math.floor(networkWatchdogMs / 1000)
    )
    sys.timerStart(function()
        if generation ~= networkWatchdogGeneration
            or networkReady
            or not networkWatchdogArmed then
            return
        end
        networkWatchdogArmed = false
        log.error(
            "smsCenter.network",
            "unavailable; restarting",
            math.floor(networkWatchdogMs / 1000)
        )
        sys.restart("SMSCENTER_NETWORK_WATCHDOG")
    end, networkWatchdogMs)
end

local function markNetworkReady()
    local recovered = not networkReady
    networkReady = true
    networkWatchdogArmed = false
    networkWatchdogGeneration = networkWatchdogGeneration + 1
    if recovered then log.info("smsCenter.network", "ready") end
end

local function markNetworkUnavailable(reason)
    if networkReady then
        log.warn("smsCenter.network", "unavailable", tostring(reason or "event"))
    end
    networkReady = false
    armNetworkWatchdog(reason)
end

local function onDeviceResponse(result, statusCode, _, body)
    deviceRequestBusy = false
    local ok, businessCode, reason = responseOk(result, statusCode, body)
    if ok then
        markNetworkReady()
        registered = true
        local decoded, decodedOk = json.decode(body or "")
        if decodedOk and type(decoded) == "table" then
            local seconds = tonumber(decoded.heartbeat_seconds)
            if seconds and seconds >= 15 then
                heartbeatIntervalMs = seconds * 1000
            end
        end
        log.info("smsCenter.device", "online", deviceId, "queue", #queue)
        scheduleSend(100)
        scheduleCommandResult(200)
        scheduleCommandPoll(1000)
        scheduleHeartbeat(heartbeatIntervalMs)
        return
    end
    if not result and tostring(statusCode or "") == "network not ready" then
        markNetworkUnavailable("http")
    end
    registered = false
    log.warn(
        "smsCenter.device",
        "request failed",
        tostring(statusCode or reason or businessCode or "transport")
    )
    scheduleHeartbeat(15000)
end

local function sendDeviceRequest(path, callback)
    if deviceRequestBusy then
        scheduleHeartbeat(5000)
        return
    end
    if not refreshDeviceId() then
        log.warn("smsCenter.device", "IMEI not ready")
        scheduleHeartbeat(5000)
        return
    end
    deviceRequestBusy = true
    post(path, devicePayload(), callback or onDeviceResponse)
end

local function onSyncResponse(result, statusCode, _, body)
    deviceRequestBusy = false
    local ok, businessCode, reason = responseOk(result, statusCode, body)
    if ok then
        markNetworkReady()
        registered = true
        local decoded, decodedOk = json.decode(body or "")
        if decodedOk and type(decoded) == "table" then
            local seconds = tonumber(decoded.sync_seconds)
            if seconds and seconds >= 300 then
                fallbackSyncIntervalMs = seconds * 1000
            end
            local command = decoded.command
            if type(command) == "table" and tonumber(command.id) then
                sendOutboundCommand(command)
            end
        end
        log.info("smsCenter.device", "sync", deviceId, "queue", #queue)
        scheduleSend(100)
        scheduleCommandResult(200)
        scheduleHeartbeat(fallbackSyncIntervalMs)
        return
    end
    if not result and tostring(statusCode or "") == "network not ready" then
        markNetworkUnavailable("http")
    end
    registered = false
    log.warn(
        "smsCenter.device",
        "sync failed",
        tostring(statusCode or reason or businessCode or "transport")
    )
    scheduleHeartbeat(60000)
end

sendHeartbeat = function()
    if longConnectionEnabled then
        if wsAuthenticated then return end
        if not networkReady then
            scheduleHeartbeat(60000)
            return
        end
        sendDeviceRequest("/api/device/sync", onSyncResponse)
        return
    end
    sendDeviceRequest(registered and "/api/device/heartbeat" or "/api/device/register")
end

local function enqueueCommandResult(commandId, success, errorMessage)
    commandResults[#commandResults + 1] = {
        id = tonumber(commandId),
        ok = success and true or false,
        error = success and "" or tostring(errorMessage or "send failed")
    }
    persistCommandResults()
    scheduleCommandResult(100)
end

sendOutboundCommand = function(command)
    if commandSendBusy then return false end
    local commandId = tonumber(command.id)
    local recipient = tostring(command.to or "")
    local text = tostring(command.text or "")
    if not commandId or recipient == "" or text == "" then
        if commandId then
            enqueueCommandResult(commandId, false, "invalid command")
        end
        return false
    end

    local converted, gb2312 = pcall(common.utf8ToGb2312, text)
    if not converted or type(gb2312) ~= "string" or gb2312 == "" then
        enqueueCommandResult(commandId, false, "text conversion failed")
        return false
    end

    commandSendBusy = true
    local callbackCalled = false
    local function completed(success)
        if callbackCalled then return end
        callbackCalled = true
        commandSendBusy = false
        enqueueCommandResult(
            commandId,
            success and true or false,
            success and "" or "modem send failed"
        )
        log.info(
            "smsCenter.outbound",
            success and "sent" or "failed",
            commandId
        )
        scheduleCommandPoll(commandPollIntervalMs)
    end

    local invoked, invokeError = pcall(sms.send, recipient, gb2312, completed)
    if not invoked then
        log.error("smsCenter.outbound", "send call failed", tostring(invokeError))
        completed(false)
    end
    sys.timerStart(
        function() completed(false) end,
        tonumber(config.command_send_timeout_ms) or 600000
    )
    if not callbackCalled then
        log.info("smsCenter.outbound", "accepted", commandId, recipient)
    end
    return true
end

local function onCommandPollResponse(result, statusCode, _, body)
    commandPollBusy = false
    local ok, businessCode, reason = responseOk(result, statusCode, body)
    if not ok then
        log.warn(
            "smsCenter.outbound",
            "poll failed",
            tostring(statusCode or reason or businessCode or "transport")
        )
        scheduleCommandPoll(commandPollIntervalMs)
        return
    end
    local decoded, decodedOk = json.decode(body or "")
    if decodedOk and type(decoded) == "table" and tonumber(decoded.id) then
        if not sendOutboundCommand(decoded) then
            scheduleCommandPoll(commandPollIntervalMs)
        end
        return
    end
    scheduleCommandPoll(commandPollIntervalMs)
end

pollOutboundSms = function()
    if commandPollBusy or commandSendBusy or not configReady then
        scheduleCommandPoll(commandPollIntervalMs)
        return
    end
    if not registered or not refreshDeviceId() then
        scheduleCommandPoll(commandPollIntervalMs)
        return
    end
    commandPollBusy = true
    post(
        "/api/device/outbound/poll",
        {device_id = deviceId},
        onCommandPollResponse
    )
end

local function onCommandResultResponse(result, statusCode, _, body)
    commandResultBusy = false
    local item = commandResults[1]
    if not item then return end
    local ok, businessCode, reason = responseOk(result, statusCode, body)
    if ok then
        table.remove(commandResults, 1)
        persistCommandResults()
        scheduleCommandResult(100)
        return
    end
    log.warn(
        "smsCenter.outbound",
        "result report failed",
        tostring(statusCode or reason or businessCode or "transport")
    )
    scheduleCommandResult(commandPollIntervalMs)
end

tryReportCommandResult = function()
    if commandResultBusy or #commandResults == 0 or not configReady then return end
    if not registered or not refreshDeviceId() then
        scheduleCommandResult(commandPollIntervalMs)
        return
    end
    local item = commandResults[1]
    if longConnectionEnabled and wsAuthenticated then
        if wsResultPending then return end
        wsResultPending = tonumber(item.id)
        local payload = {
            type = "result",
            id = item.id,
            ok = item.ok
        }
        if not item.ok then payload.error = item.error end
        if not sendLongConnectionPayload(payload) then
            wsResultPending = nil
            scheduleCommandResult(60000)
        end
        return
    end
    local payload = {
        device_id = deviceId,
        id = item.id,
        ok = item.ok
    }
    if not item.ok then payload.error = item.error end
    commandResultBusy = true
    post("/api/device/outbound/result", payload, onCommandResultResponse)
end

sendLongConnectionPayload = function(payload)
    local client = wsClient
    if not client
        or not wsConnected
        or type(client.send) ~= "function" then
        return false
    end
    local encoded = json.encode(payload)
    if type(encoded) ~= "string" then return false end
    -- Air724UG socket sends yield while waiting for the modem. Calling the
    -- low-level sendFrame through pcall cannot cross that Lua 5.1 coroutine
    -- boundary. The public send API queues the frame and wakes recvFrame so
    -- the socket operation stays in the WebSocket owner's task.
    client:send(encoded, true)
    return true
end

local function scheduleLongConnectionStatus(delay)
    if not configReady
        or not longConnectionEnabled
        or wsStatusTimerScheduled then
        return
    end
    wsStatusTimerScheduled = true
    sys.timerStart(function()
        wsStatusTimerScheduled = false
        sendLongConnectionStatus()
    end, delay or longConnectionStatusIntervalMs)
end

sendLongConnectionStatus = function()
    if not wsAuthenticated then return end
    local payload = devicePayload()
    payload.type = "status"
    payload.token = nil
    sendLongConnectionPayload(payload)
    scheduleLongConnectionStatus(longConnectionStatusIntervalMs)
end

local function handleLongConnectionMessage(message)
    local decoded, decodedOk = json.decode(message or "")
    if not decodedOk or type(decoded) ~= "table" then
        log.warn("smsCenter.ws", "invalid message")
        return
    end
    if decoded.type == "ready" and tonumber(decoded.code) == 0 then
        wsAuthenticated = true
        registered = true
        markNetworkReady()
        local statusSeconds = tonumber(decoded.status_seconds)
        if statusSeconds and statusSeconds >= 300 then
            longConnectionStatusIntervalMs = statusSeconds * 1000
        end
        local syncSeconds = tonumber(decoded.fallback_sync_seconds)
        if syncSeconds and syncSeconds >= 60 then
            fallbackSyncIntervalMs = syncSeconds * 1000
        end
        log.info("smsCenter.ws", "online", deviceId)
        scheduleLongConnectionStatus(longConnectionStatusIntervalMs)
        scheduleSend(100)
        scheduleCommandResult(200)
        return
    end
    if not wsAuthenticated then return end
    if decoded.type == "command" then
        sendOutboundCommand(decoded)
        return
    end
    if decoded.type == "result_ack"
        and tonumber(decoded.id) == tonumber(wsResultPending) then
        if tonumber(decoded.code) == 0 then
            local item = commandResults[1]
            if item and tonumber(item.id) == tonumber(decoded.id) then
                table.remove(commandResults, 1)
                persistCommandResults()
            end
            wsResultPending = nil
            scheduleCommandResult(100)
        else
            wsResultPending = nil
            log.warn("smsCenter.ws", "result rejected", tostring(decoded.code or ""))
            scheduleCommandResult(60000)
        end
        return
    end
    if decoded.type == "error" then
        log.warn("smsCenter.ws", "server error", tostring(decoded.code or ""))
    end
end

local function closeLongConnection()
    local client = wsClient
    wsConnected = false
    wsAuthenticated = false
    wsResultPending = nil
    wsClient = nil
    if client then pcall(client.close, client) end
end

local function longConnectionRetryDelay(attempt)
    local index = math.min(
        math.max(tonumber(attempt) or 1, 1),
        #longConnectionReconnectDelaysMs
    )
    return tonumber(longConnectionReconnectDelaysMs[index]) or 3600000
end

local function waitForLongConnectionRetry(delay)
    local elapsed = false
    wsRetryWaitId = wsRetryWaitId + 1
    local event = "SMSCENTER_WS_RETRY_" .. tostring(wsRetryWaitId)
    sys.timerStart(function()
        elapsed = true
        sys.publish(event)
    end, math.max(1000, tonumber(delay) or 1000))
    while not elapsed and configReady and longConnectionEnabled do
        sys.waitUntil(event)
    end
end

runLongConnection = function()
    local attempt = 1
    while configReady and longConnectionEnabled do
        if not networkReady then
            sys.wait(1000)
        else
            local client = websocket.new(longConnectionUrl, config.long_connection_cert)
            wsClient = client
            local connected = client and client:connect(
                tonumber(config.long_connection_connect_timeout_ms) or 30000
            )
            local authenticatedAtStart = false
            if connected then
                wsConnected = true
                local hello = devicePayload()
                hello.type = "hello"
                if sendLongConnectionPayload(hello) then
                    local authStartedAt = tonumber(os.time()) or 0
                    while wsConnected and client:online() do
                        local received, message = client:recv()
                        if received and message then
                            handleLongConnectionMessage(message)
                        elseif not received and message ~= "WEBSOCKET_OK" then
                            break
                        end
                        if wsAuthenticated then
                            authenticatedAtStart = true
                        elseif (tonumber(os.time()) or 0) - authStartedAt >= 60 then
                            log.warn("smsCenter.ws", "authentication timeout")
                            break
                        end
                    end
                end
            end
            closeLongConnection()
            scheduleHeartbeat(60000)
            if authenticatedAtStart then
                attempt = 1
            else
                attempt = attempt + 1
            end
            local delay = longConnectionRetryDelay(attempt)
            log.warn("smsCenter.ws", "offline; retry_ms", delay)
            waitForLongConnectionRetry(delay)
        end
    end
end

runOtaCheck = function()
    if not otaEnabled
        or otaProductKey == ""
        or not otaUpdate
        or otaCheckBusy then
        return
    end
    if not registered
        or sending
        or deviceRequestBusy
        or commandPollBusy
        or commandSendBusy
        or commandResultBusy
        or #queue > 0
        or #commandResults > 0 then
        scheduleOtaCheck(otaBusyRetryMs)
        return
    end

    otaCheckBusy = true
    otaGeneration = otaGeneration + 1
    local generation = otaGeneration
    log.info("smsCenter.ota", "checking")
    local ok, err = pcall(
        otaUpdate.request,
        function(success)
            if generation ~= otaGeneration then return end
            otaCheckBusy = false
            if success then
                log.info("smsCenter.ota", "downloaded; restarting")
                sys.restart("SMSCENTER_OTA_SUCCESS")
                return
            end
            log.info("smsCenter.ota", "no upgrade")
            scheduleOtaCheck(otaCheckIntervalMs)
        end,
        nil,
        nil,
        true
    )
    if not ok then
        otaCheckBusy = false
        log.error("smsCenter.ota", "request failed", tostring(err or ""))
        scheduleOtaCheck(otaBusyRetryMs)
        return
    end

    sys.timerStart(function()
        if generation ~= otaGeneration or not otaCheckBusy then return end
        otaCheckBusy = false
        otaGeneration = otaGeneration + 1
        log.warn("smsCenter.ota", "check timed out")
        scheduleOtaCheck(otaBusyRetryMs)
    end, otaWatchdogMs)
end

local function onMessageResponse(result, statusCode, _, body)
    sending = false
    local item = queue[1]
    if not item then return end
    local ok, businessCode, reason = responseOk(result, statusCode, body)
    if ok then
        table.remove(queue, 1)
        persistQueue()
        log.info("smsCenter.send", "accepted", "pending", #queue)
        scheduleSend(100)
        return
    end
    item.attempts = (tonumber(item.attempts) or 0) + 1
    persistQueue()
    local delay = retryDelay(item.attempts)
    log.warn(
        "smsCenter.send",
        tostring(statusCode or reason or businessCode or "transport"),
        "retry_ms",
        delay
    )
    scheduleSend(delay)
end

trySend = function()
    if sending or #queue == 0 or not configReady then return end
    if not refreshDeviceId() then
        scheduleSend(5000)
        return
    end
    if not registered then
        sendDeviceRequest("/api/device/register")
        scheduleSend(5000)
        return
    end

    local item = queue[1]
    local payload = devicePayload()
    local path
    if item.kind == "missed_call" then
        path = "/api/device/missed-call"
        payload.call_id = item.call_id
        payload.caller = item.phone
        payload.started_at = item.datetime
        payload.ended_at = item.ended_at
        payload.duration_seconds = item.duration_seconds or 0
    else
        path = "/api/messages"
        payload.sender = item.phone
        payload.body = item.text
        payload.sms_time = item.datetime
    end
    sending = true
    post(path, payload, onMessageResponse)
end

local function alreadyQueued(candidate)
    for i = 1, #queue do
        local item = queue[i]
        if candidate.kind == "missed_call" then
            if item.kind == "missed_call"
                and item.call_id == candidate.call_id then
                return true
            end
        elseif item.kind ~= "missed_call" and core.sameSms(item, candidate) then
            return true
        end
    end
    return false
end

local function enqueue(phone, text, datetime)
    if not configReady then
        log.error("smsCenter.sms", "configuration unavailable")
        return false
    end
    if #queue >= (tonumber(config.max_queue_size) or 200) then
        log.error("smsCenter.queue", "queue full", #queue)
        return false
    end
    local item = {
        kind = "sms",
        phone = tostring(phone or ""),
        text = tostring(text or ""),
        datetime = tostring(datetime or ""),
        attempts = 0
    }
    if alreadyQueued(item) then
        log.warn("smsCenter.sms", "duplicate ignored")
        return true
    end
    queue[#queue + 1] = item
    if not persistQueue() then
        table.remove(queue, #queue)
        log.error("smsCenter.sms", "queue persistence failed")
        return false
    end
    log.info("smsCenter.sms", "queued", "pending", #queue)
    scheduleSend(100)
    return true
end

local function enqueueMissedCall(phone, startedAt, endedAt, durationSeconds, callId)
    if not configReady or config.enable_missed_call_forwarding == false then
        return false
    end
    if #queue >= (tonumber(config.max_queue_size) or 200) then
        log.error("smsCenter.queue", "queue full", #queue)
        return false
    end
    local item = {
        kind = "missed_call",
        call_id = tostring(callId or ""),
        phone = tostring(phone or ""),
        datetime = tostring(startedAt or ""),
        ended_at = tostring(endedAt or ""),
        duration_seconds = tonumber(durationSeconds) or 0,
        attempts = 0
    }
    if item.call_id == "" then return false end
    if alreadyQueued(item) then return true end
    queue[#queue + 1] = item
    if not persistQueue() then
        table.remove(queue, #queue)
        log.error("smsCenter.call", "queue persistence failed")
        return false
    end
    log.info("smsCenter.call", "missed call queued", "pending", #queue)
    scheduleSend(100)
    return true
end

local function nowText()
    if os and type(os.date) == "function" then
        return os.date("%Y-%m-%d %H:%M:%S")
    end
    return ""
end

local function nowEpoch()
    if os and type(os.time) == "function" then
        return tonumber(os.time()) or 0
    end
    return 0
end

local function runtimeTick()
    if rtos and type(rtos.tick) == "function" then
        local ok, tick = pcall(rtos.tick)
        if ok then return tonumber(tick) or 0 end
    end
    return 0
end

local function onIncomingCall(phone)
    local caller = tostring(phone or "")
    local startedEpoch = nowEpoch()
    callSequence = callSequence + 1
    activeIncomingCall = {
        phone = caller,
        started_at = nowText(),
        started_epoch = startedEpoch,
        call_id = tostring(startedEpoch)
            .. "-" .. tostring(runtimeTick())
            .. "-" .. tostring(callSequence)
    }
    log.info("smsCenter.call", "incoming; hanging up")
    cc.hangUp(caller)
end

local function onConnectedCall(phone)
    if not activeIncomingCall then return end
    log.warn("smsCenter.call", "unexpected connection; hanging up")
    cc.hangUp(tostring(phone or activeIncomingCall.phone or ""))
end

local function onDisconnectedCall()
    local call = activeIncomingCall
    activeIncomingCall = nil
    if not call then return end
    local endedEpoch = nowEpoch()
    enqueueMissedCall(
        call.phone,
        call.started_at,
        nowText(),
        math.max(endedEpoch - call.started_epoch, 0),
        call.call_id
    )
end

local function onNewSmsLegacy(phone, data, datetime)
    local converted, text = pcall(common.gb2312ToUtf8, data or "")
    if not converted then
        log.error("smsCenter.sms", "decode failed")
        text = "[短信内容解码失败]"
    end
    enqueue(phone, text, datetime)
end

local function decodeSmsText(data)
    local source = tostring(data or "")
    if source:sub(1, #SMS_UTF8_PREFIX) == SMS_UTF8_PREFIX then
        return source:sub(#SMS_UTF8_PREFIX + 1)
    end
    local converted, text = pcall(common.gb2312ToUtf8, source)
    if converted and type(text) == "string" and (text ~= "" or source == "") then
        return text
    end
    -- The legacy converter returns nil (without throwing) for characters
    -- outside GB2312. Keep the original GBK/GB18030 bytes; the center accepts
    -- that wire format and normalizes it before storing the message.
    log.warn("smsCenter.sms", "decode fallback to source bytes")
    return source
end

local function onNewSmsSafe(phone, data, datetime)
    enqueue(phone, decodeSmsText(data), datetime)
end

local function validateConfig()
    if not configOk or type(config) ~= "table" then
        log.error("smsCenter.config", "config.lua missing or invalid")
        return false
    end
    local token = core.trim(config.center_token)
    local url = core.trim(config.center_url)
    if token == "" or url == "" then
        log.error("smsCenter.config", "center_url and center_token are required")
        return false
    end
    config.center_token = token
    centerUrl = url:gsub("/+$", "")
    config.queue_file = config.queue_file or "/ldata/sms_center_outbox.json"
    config.command_result_file =
        config.command_result_file or "/ldata/sms_center_command_results.json"
    config.traffic_session_file =
        config.traffic_session_file or "/ldata/sms_center_traffic_session.txt"
    heartbeatIntervalMs = tonumber(config.heartbeat_interval_ms) or 60000
    commandPollIntervalMs =
        tonumber(config.command_poll_interval_ms) or 60000
    if commandPollIntervalMs < 60000 then commandPollIntervalMs = 60000 end
    longConnectionEnabled = config.long_connection_enabled == true
    longConnectionUrl = core.trim(config.long_connection_url)
    if longConnectionUrl == "" then
        longConnectionUrl = centerUrl
            :gsub("^https://", "wss://")
            :gsub("^http://", "ws://")
            .. "/api/device/ws"
    end
    if longConnectionUrl:sub(1, 5) ~= "ws://"
        and longConnectionUrl:sub(1, 6) ~= "wss://" then
        log.error("smsCenter.config", "invalid long_connection_url")
        return false
    end
    longConnectionStatusIntervalMs = math.max(
        300000,
        tonumber(config.long_connection_status_interval_ms) or 600000
    )
    fallbackSyncIntervalMs = math.max(
        60000,
        tonumber(config.fallback_sync_interval_ms) or 120000
    )
    if type(config.long_connection_reconnect_delays_ms) == "table"
        and #config.long_connection_reconnect_delays_ms > 0 then
        longConnectionReconnectDelaysMs =
            config.long_connection_reconnect_delays_ms
    end
    otaEnabled = config.ota_enabled == true
    otaProductKey = otaEnabled and core.trim(config.ota_product_key) or ""
    otaStartupDelayMs =
        math.max(60000, tonumber(config.ota_startup_delay_ms) or 120000)
    otaCheckIntervalMs =
        math.max(3600000, tonumber(config.ota_check_interval_ms) or 21600000)
    otaBusyRetryMs =
        math.max(60000, tonumber(config.ota_busy_retry_ms) or 60000)
    otaWatchdogMs =
        math.max(600000, tonumber(config.ota_watchdog_ms) or 1800000)
    networkWatchdogMs =
        math.max(60000, tonumber(config.network_watchdog_ms) or 900000)
    trafficStatsEnabled = config.traffic_stats_enabled ~= false
    trafficStatsIntervalSeconds =
        math.max(60, tonumber(config.traffic_stats_interval_seconds) or 60)
    if otaEnabled and otaProductKey ~= "" then
        _G.PRODUCT_KEY = otaProductKey
    end
    return true
end

configReady = validateConfig()
if configReady then
    sms = loadSmsModule()
    cc = loadCallModule()
    http = loadHttpModule()
    if longConnectionEnabled then
        websocket = loadWebsocketModule()
    end
    common = loadCommonModule()
    misc = loadMiscModule()
    net = loadNetModule()
    socket = loadSocketModule()
    json = loadJsonModule()
    if otaEnabled and otaProductKey ~= "" then
        otaUpdate = loadUpdateModule()
    end
    installSmsDecoderFallback()
    loadQueue()
    loadCommandResults()
    initializeTrafficStats()
    sms.setNewSmsCb(onNewSmsSafe)
    sys.subscribe("CALL_INCOMING", onIncomingCall)
    sys.subscribe("CALL_CONNECTED", onConnectedCall)
    sys.subscribe("CALL_DISCONNECTED", onDisconnectedCall)
    sys.subscribe("IP_READY_IND", markNetworkReady)
    sys.subscribe("IP_ERROR_IND", function()
        markNetworkUnavailable("ip error")
    end)
    sys.subscribe("PDP_DEACT_IND", function()
        markNetworkUnavailable("pdp deactivated")
    end)
    sys.subscribe("NET_STATE_UNREGISTER", function()
        markNetworkUnavailable("network unregistered")
    end)
    sys.subscribe("NET_STATE_REGISTERED", function()
        registered = false
        scheduleHeartbeat(1000)
        scheduleSend(1500)
        scheduleCommandPoll(2000)
        scheduleCommandResult(2500)
    end)
    sys.subscribe("SMS_READY", function() scheduleHeartbeat(1000) end)
    if longConnectionEnabled then
        sys.taskInit(runLongConnection)
    end
    scheduleHeartbeat(tonumber(config.startup_delay_ms) or 12000)
    if otaEnabled then
        scheduleOtaCheck(otaStartupDelayMs)
    else
        log.info("smsCenter.ota", "disabled")
    end
    armNetworkWatchdog("startup")
    log.info("smsCenter", "ready", "pending", #queue)
end

return {
    enqueue = enqueue,
    enqueueMissedCall = enqueueMissedCall,
    decodeSmsText = decodeSmsText,
    pendingCount = function() return #queue end,
    pendingCommandResultCount = function() return #commandResults end,
    otaEnabled = function() return otaEnabled end,
    deviceId = function() return deviceId end
}
