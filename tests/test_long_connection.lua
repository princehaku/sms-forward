package.path = "./?.lua;" .. (package.path or "")
package.preload = package.preload or {}

local files = {}
local timers = {}
local subscriptions = {}
local task
local httpCalls = 0
local sentFrames = {}
local smsSendCallback
local recvStep = 0
local passed = 0

VERSION = "2.2.3"
rtos = {get_version = function() return "V4035" end}

local function equal(actual, expected, name)
    if actual ~= expected then
        error(name .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
    end
    passed = passed + 1
end

local function popTimer(delay)
    for i = 1, #timers do
        if timers[i].delay == delay then return table.remove(timers, i) end
    end
    error("timer not found: " .. tostring(delay))
end

log = {info = function() end, warn = function() end, error = function() end}
io = {}
function io.open(path, mode)
    if mode == "rb" and files[path] == nil then return nil end
    local buffer = mode == "rb" and files[path] or ""
    return {
        read = function() return buffer end,
        write = function(_, value) files[path] = value return true end,
        close = function() end
    }
end

sys = {}
function sys.timerStart(callback, delay)
    timers[#timers + 1] = {callback = callback, delay = delay}
end
function sys.subscribe(event, callback) subscriptions[event] = callback end
function sys.taskInit(callback) task = callback end
function sys.wait() end
function sys.restart() end

package.preload.config = function()
    return {
        center_url = "https://example.test/sms",
        center_token = "secret-device-token",
        device_id = "device-ws-1",
        queue_file = "tests/ws-outbox.json",
        command_result_file = "tests/ws-results.json",
        startup_delay_ms = 12000,
        network_watchdog_ms = 900000,
        long_connection_enabled = true,
        long_connection_status_interval_ms = 21600000,
        fallback_sync_interval_ms = 120000
    }
end
package.preload.sms = function()
    return {
        setNewSmsCb = function() end,
        send = function(_, _, callback) smsSendCallback = callback return true end
    }
end
package.preload.cc = function() return {hangUp = function() end} end
package.preload.http = function()
    return {request = function() httpCalls = httpCalls + 1 end}
end
package.preload.common = function()
    return {
        utf8ToGb2312 = function(value) return value end,
        gb2312ToUtf8 = function(value) return value end
    }
end
package.preload.misc = function()
    return {getImei = function() return "device-ws-1" end}
end
package.preload.net = function()
    return {getState = function() return "REGISTERED" end, getRssi = function() return 20 end}
end
package.preload.socket = function()
    return {setIpStatis = function() end}
end
package.preload.update = function() return {} end
package.preload.json = function()
    return {
        encode = function(value)
            if value.type == "hello" then
                equal(value.token, "secret-device-token", "hello authenticates in payload")
                return "HELLO"
            end
            if value.type == "result" then return "RESULT" end
            return "QUEUE:" .. tostring(#value)
        end,
        decode = function(value)
            if value == "READY" then
                return {
                    type = "ready",
                    code = 0,
                    status_seconds = 21600,
                    fallback_sync_seconds = 120
                }, true
            end
            if value == "COMMAND" then
                return {type = "command", id = 41, to = "13900139000", text = "test"}, true
            end
            if value == "RESULT_ACK" then
                return {type = "result_ack", code = 0, id = 41}, true
            end
            return nil, false
        end
    }
end

local fakeClient = {connected = false}
function fakeClient:connect()
    self.connected = true
    return true
end
function fakeClient:online() return self.connected end
function fakeClient:send(payload, text)
    equal(text, true, "text websocket message")
    sentFrames[#sentFrames + 1] = payload
    -- Match the Air724UG websocket library: queued sends return nil.
end
function fakeClient:sendFrame()
    error("low-level sendFrame must stay inside the WebSocket task")
end
function fakeClient:recv()
    recvStep = recvStep + 1
    if recvStep == 1 then return true, "READY" end
    if recvStep == 2 then return true, "COMMAND" end
    if recvStep == 3 then
        smsSendCallback(true)
        return false, "WEBSOCKET_OK"
    end
    if recvStep == 4 then
        popTimer(200).callback()
        return false, "WEBSOCKET_OK"
    end
    if recvStep == 5 then return true, "RESULT_ACK" end
    error("STOP_TEST_TASK")
end
function fakeClient:close() self.connected = false end

package.preload.websocket = function()
    return {new = function(url)
        equal(url, "wss://example.test/sms/api/device/ws", "derived WSS URL")
        return fakeClient
    end}
end

local app = require "sms_center"
equal(type(task), "function", "long connection task started")
popTimer(12000).callback()
equal(httpCalls, 0, "HTTP fallback waits for packet data")
subscriptions.IP_READY_IND()
local ran, taskError = pcall(task)
equal(ran, false, "test task stopped")
equal(tostring(taskError):find("STOP_TEST_TASK", 1, true) ~= nil, true, "test stop observed")
equal(sentFrames[1], "HELLO", "hello sent without HTTP")
equal(sentFrames[2], "RESULT", "command result sent over WSS")
equal(app.pendingCommandResultCount(), 0, "result removed after server acknowledgement")
equal(httpCalls, 0, "no background HTTP request while WSS is active")

popTimer(60000).callback()
equal(httpCalls, 0, "legacy heartbeat suppressed while WSS is authenticated")

print("PASS: " .. passed .. " long-connection assertions")
