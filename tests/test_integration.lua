package.path = "./?.lua;" .. (package.path or "")
package.preload = package.preload or {}

local outboxPath = "tests/test_outbox.json"
local files = {}
local timers = {}
local subscriptions = {}
local smsCallback
local smsSendCallback
local smsSendNumber
local smsSendBody
local hungUpNumber
local otaRequests = {}
local restartReason
local ipStatisInterval
local httpCalls = {}
local httpResponses = {
    {true, "200", "success"},
    {false, "network not ready", ""},
    {true, "200", "success"},
    {true, "200", "command"},
    {true, "200", "success"}
}
local lastDevicePayload
local lastMessagePayload
local lastMissedCallPayload
local lastPollPayload
local lastResultPayload
local passed = 0

VERSION = "2.2.2"
rtos = {
    get_version = function()
        return "LuatOS-Air_V4035_RDA8910_TTS_NOLVGL_FLOAT"
    end
}

local function equal(actual, expected, name)
    if actual ~= expected then
        error(name .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
    end
    passed = passed + 1
end

local function popTimer(delay)
    for i = 1, #timers do
        if timers[i].delay == delay then
            return table.remove(timers, i)
        end
    end
    error("timer not found: " .. tostring(delay))
end

log = {
    info = function() end,
    warn = function() end,
    error = function() end
}

io = {}
function io.open(path, mode)
    if mode == "rb" and files[path] == nil then return nil end
    local buffer = mode == "rb" and files[path] or ""
    return {
        read = function(_, format)
            equal(format, "*a", "read all mode")
            return buffer
        end,
        write = function(_, content)
            files[path] = content
            buffer = content
            return true
        end,
        close = function() end
    }
end

sys = {}
function sys.timerStart(callback, delay)
    timers[#timers + 1] = {callback = callback, delay = delay}
end
function sys.subscribe(event, callback)
    subscriptions[event] = callback
end
function sys.restart(reason)
    restartReason = reason
end

package.preload["config"] = function()
    return {
        center_url = "https://bytegallop.com/sms/",
        center_token = "sms-sb",
        device_name = "测试设备",
        phone_number = "15300000000",
        queue_file = outboxPath,
        max_queue_size = 10,
        http_timeout_ms = 12345,
        startup_delay_ms = 12000,
        heartbeat_interval_ms = 60000,
        command_poll_interval_ms = 60000,
        long_connection_enabled = false,
        command_result_file = "tests/test_command_results.json",
        command_send_timeout_ms = 600000,
        network_watchdog_ms = 900000,
        enable_missed_call_forwarding = true,
        ota_enabled = false,
        ota_product_key = "test-product-key",
        ota_startup_delay_ms = 90000,
        ota_check_interval_ms = 3600000,
        ota_busy_retry_ms = 60000,
        ota_watchdog_ms = 600000,
        retry_delays_ms = {5000, 15000}
    }
end

package.preload["sms"] = function()
    return {
        setNewSmsCb = function(callback) smsCallback = callback end,
        send = function(number, body, callback)
            smsSendNumber = number
            smsSendBody = body
            smsSendCallback = callback
            return true
        end
    }
end
package.preload["cc"] = function()
    return {
        hangUp = function(number) hungUpNumber = number end
    }
end
package.preload["update"] = function()
    return {
        request = function(callback, url, period, redir)
            otaRequests[#otaRequests + 1] = {
                callback = callback,
                url = url,
                period = period,
                redir = redir
            }
        end
    }
end
package.preload["common"] = function()
    return {
        gb2312ToUtf8 = function(value)
            if value == "BAD_GB2312" then return nil end
            return "UTF8:" .. value
        end,
        utf8ToGb2312 = function(value)
            return "GB:" .. value
        end
    }
end
package.preload["misc"] = function()
    return {getImei = function() return "861700000000001" end}
end
package.preload["net"] = function()
    return {
        getState = function() return "REGISTERED" end,
        getRssi = function() return 18 end
    }
end
package.preload["socket"] = function()
    return {
        setIpStatis = function(interval) ipStatisInterval = interval end
    }
end
package.preload["json"] = function()
    return {
        encode = function(value)
            if value.token then
                if value.call_id then
                    lastMissedCallPayload = value
                    return "MISSED_CALL"
                end
                if value.body then
                    lastMessagePayload = value
                    return "MESSAGE"
                end
                lastDevicePayload = value
                return "DEVICE"
            end
            if value.device_id and value.id and value.ok ~= nil then
                lastResultPayload = value
                return "RESULT"
            end
            if value.device_id then
                lastPollPayload = value
                return "POLL"
            end
            return "QUEUE:" .. tostring(#value)
        end,
        decode = function(value)
            if value == "success" then
                return {code = 0, heartbeat_seconds = 30}, true
            end
            if value == "command" then
                return {
                    code = 0,
                    id = 91,
                    to = "13900139000",
                    text = "控制台下行"
                }, true
            end
            return nil, false, "invalid"
        end
    }
end
package.preload["http"] = function()
    return {
        request = function(method, url, cert, headers, body, timeout, callback)
            httpCalls[#httpCalls + 1] = {
                method = method,
                url = url,
                cert = cert,
                headers = headers,
                body = body,
                timeout = timeout
            }
            local response = table.remove(httpResponses, 1)
            callback(response[1], response[2], {}, response[3])
        end
    }
end

local app = require "sms_center"
equal(type(smsCallback), "function", "sms callback registered")
equal(app.decodeSmsText("normal"), "UTF8:normal", "normal SMS decoding")
equal(app.decodeSmsText("BAD_GB2312"), "BAD_GB2312", "failed conversion fallback")
equal(app.decodeSmsText("\2SMSCENTER_UTF8\2direct"), "direct", "UTF8 fallback marker")
equal(type(subscriptions.NET_STATE_REGISTERED), "function", "network callback registered")
equal(type(subscriptions.SMS_READY), "function", "sms ready callback registered")
equal(type(subscriptions.CALL_INCOMING), "function", "incoming call callback registered")
equal(type(subscriptions.CALL_CONNECTED), "function", "connected call callback registered")
equal(type(subscriptions.CALL_DISCONNECTED), "function", "disconnected call callback registered")
equal(type(subscriptions.IP_READY_IND), "function", "IP ready callback registered")
equal(type(subscriptions.IP_ERROR_IND), "function", "IP error callback registered")
equal(type(subscriptions.PDP_DEACT_IND), "function", "PDP callback registered")
equal(type(subscriptions.NET_STATE_UNREGISTER), "function", "network loss callback registered")
equal(type(subscriptions.LIB_IP_STATIS_RPT), "function", "traffic callback registered")
equal(ipStatisInterval, 60, "traffic statistics interval")
equal(app.otaEnabled(), false, "OTA disabled")
equal(_G.PRODUCT_KEY, nil, "OTA product key not installed")
equal(#timers, 2, "startup timers")
equal(timers[1].delay, 12000, "startup delay")
equal(timers[2].delay, 900000, "network watchdog delay")

local startupWatchdog = popTimer(900000)
subscriptions.IP_READY_IND()
startupWatchdog.callback()
equal(restartReason, nil, "recovery cancels startup watchdog")
subscriptions.IP_ERROR_IND()
local armedTimerCount = #timers
subscriptions.PDP_DEACT_IND()
equal(#timers, armedTimerCount, "duplicate loss event keeps original watchdog")
popTimer(900000).callback()
equal(restartReason, "SMSCENTER_NETWORK_WATCHDOG", "network watchdog restart")
restartReason = nil
subscriptions.IP_READY_IND()
subscriptions.LIB_IP_STATIS_RPT(1536)

smsCallback("13800138000", "验证码 1234", "2026-07-23 03:00:00")
equal(app.pendingCount(), 1, "sms queued")
smsCallback("13800138000", "验证码 1234", "2026-07-23 03:00:00")
equal(app.pendingCount(), 1, "duplicate ignored")

popTimer(12000).callback()
equal(#httpCalls, 1, "register request")
equal(httpCalls[1].url, "https://bytegallop.com/sms/api/device/register", "register url")
equal(lastDevicePayload.device_id, "861700000000001", "IMEI device id")
equal(lastDevicePayload.signal, 18, "signal included")
equal(lastDevicePayload.traffic_session_id, "1", "traffic session included")
equal(lastDevicePayload.traffic_session_bytes, 1536, "traffic bytes included")
equal(lastDevicePayload.app_version, "2.2.2", "application version included")
equal(
    lastDevicePayload.firmware,
    "LuatOS-Air_V4035_RDA8910_TTS_NOLVGL_FLOAT",
    "firmware version included"
)

popTimer(100).callback()
equal(#httpCalls, 2, "first message attempt")
equal(app.pendingCount(), 1, "failed item retained")
equal(httpCalls[2].url, "https://bytegallop.com/sms/api/messages", "message url")
equal(httpCalls[2].headers["X-SMS-Token"], "sms-sb", "token header")
equal(httpCalls[2].timeout, 12345, "http timeout")
equal(lastMessagePayload.sender, "13800138000", "sender uploaded")
equal(lastMessagePayload.body, "UTF8:验证码 1234", "converted body uploaded")
equal(lastMessagePayload.queue_count, 1, "queue count uploaded")

popTimer(200).callback()

popTimer(5000).callback()
equal(#httpCalls, 3, "retry request")
equal(app.pendingCount(), 0, "successful item removed")
equal(app.deviceId(), "861700000000001", "device id exposed")
popTimer(100).callback()

popTimer(1000).callback()
equal(#httpCalls, 4, "outbound poll request")
equal(httpCalls[4].url, "https://bytegallop.com/sms/api/device/outbound/poll", "poll url")
equal(lastPollPayload.device_id, "861700000000001", "minimal poll device id")
equal(smsSendNumber, "13900139000", "outbound recipient")
equal(smsSendBody, "GB:控制台下行", "outbound text converted")
equal(type(smsSendCallback), "function", "outbound callback installed")

smsSendCallback(true, smsSendNumber, smsSendBody)
equal(app.pendingCommandResultCount(), 1, "outbound result persisted")
popTimer(100).callback()
equal(#httpCalls, 5, "outbound result report")
equal(httpCalls[5].url, "https://bytegallop.com/sms/api/device/outbound/result", "result url")
equal(lastResultPayload.id, 91, "result command id")
equal(lastResultPayload.ok, true, "result success")
equal(app.pendingCommandResultCount(), 0, "reported result removed")
popTimer(100).callback()

subscriptions.CALL_INCOMING("13100000000")
equal(hungUpNumber, "13100000000", "incoming call rejected")
equal(app.pendingCount(), 0, "call waits for disconnect before queueing")
subscriptions.CALL_DISCONNECTED("NO CARRIER")
equal(app.pendingCount(), 1, "missed call queued")
httpResponses[#httpResponses + 1] = {true, "200", "success"}
popTimer(100).callback()
equal(#httpCalls, 6, "missed call report request")
equal(
    httpCalls[6].url,
    "https://bytegallop.com/sms/api/device/missed-call",
    "missed call report url"
)
equal(lastMissedCallPayload.caller, "13100000000", "caller uploaded")
equal(type(lastMissedCallPayload.call_id), "string", "call id uploaded")
equal(lastMissedCallPayload.duration_seconds >= 0, true, "ring duration uploaded")
equal(app.pendingCount(), 0, "reported missed call removed")

subscriptions.CALL_INCOMING("13200000000")
subscriptions.CALL_CONNECTED("13200000000")
equal(hungUpNumber, "13200000000", "unexpected connection rejected again")
subscriptions.CALL_DISCONNECTED("NO CARRIER")
equal(app.pendingCount(), 1, "incoming call remains reportable after connection race")
httpResponses[#httpResponses + 1] = {true, "200", "success"}
popTimer(100).callback()
equal(app.pendingCount(), 0, "connection race call report removed after success")

equal(#otaRequests, 0, "OTA request disabled")
equal(restartReason, nil, "OTA cannot restart device")

local file = assert(io.open(outboxPath, "rb"))
equal(file:read("*a"), "QUEUE:0", "empty queue persisted")
file:close()

local resultFile = assert(io.open("tests/test_command_results.json", "rb"))
equal(resultFile:read("*a"), "QUEUE:0", "empty result queue persisted")
resultFile:close()

print("PASS: " .. passed .. " integration assertions")
