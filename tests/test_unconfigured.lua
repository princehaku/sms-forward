package.path = "./device/?.lua;" .. package.path

local smsLoaded = false
local callLoaded = false
local updateLoaded = false
local socketLoaded = false
local errors = {}

log = {
    info = function() end,
    warn = function() end,
    error = function(_, message) errors[#errors + 1] = message end
}

sys = {
    timerStart = function() error("timer must not start without center config") end,
    subscribe = function() error("events must not be subscribed without center config") end
}

package.preload["config"] = function()
    return {center_url = "", center_token = ""}
end
package.preload["sms"] = function()
    smsLoaded = true
    error("sms module must not load without center config")
end
package.preload["cc"] = function()
    callLoaded = true
    error("call module must not load without center config")
end
package.preload["update"] = function()
    updateLoaded = true
    error("update module must not load without center config")
end
package.preload["socket"] = function()
    socketLoaded = true
    error("socket module must not load without center config")
end

local app = require "sms_center"
assert(app.pendingCount() == 0, "unconfigured queue should be empty")
assert(smsLoaded == false, "sms module loaded before configuration")
assert(callLoaded == false, "call module loaded before configuration")
assert(updateLoaded == false, "update module loaded before configuration")
assert(socketLoaded == false, "socket module loaded before configuration")
assert(
    #errors == 1 and errors[1] == "center_url and center_token are required",
    "missing-center-config error not reported"
)

print("PASS: unconfigured startup leaves the SMS module disabled")
