package.path = "./?.lua;" .. package.path

local core = require "sms_center_core"
local passed = 0

local function equal(actual, expected, name)
    if actual ~= expected then
        error(name .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
    end
    passed = passed + 1
end

local fakeJson = {}
function fakeJson.encode(value)
    equal(value.msg_type, "text", "payload type")
    equal(value.content.text, "hello", "payload text")
    return "encoded"
end
function fakeJson.decode(value)
    if value == "new" then return {code = 0}, true end
    if value == "old" then return {StatusCode = 0}, true end
    if value == "denied" then return {code = 19001}, true end
    return nil, false, "bad json"
end

equal(core.trim("  abc \r\n"), "abc", "trim")
equal(core.makePayload(fakeJson, "hello"), "encoded", "payload encoded")

local formatted = core.formatMessage("测试板", "13800138000", "验证码 1234", "2026-07-23 02:00:00")
equal(formatted:find("【短信转发】", 1, true) ~= nil, true, "message title")
equal(formatted:find("发件人：13800138000", 1, true) ~= nil, true, "message phone")
equal(formatted:find("验证码 1234", 1, true) ~= nil, true, "message body")

local ok, code, reason = core.responseSucceeded(fakeJson, true, "200", "new")
equal(ok, true, "new response")
equal(code, 0, "new response code")
equal(reason, nil, "new response reason")

ok = core.responseSucceeded(fakeJson, true, "200", "old")
equal(ok, true, "legacy response")
ok, code, reason = core.responseSucceeded(fakeJson, true, "200", "denied")
equal(ok, false, "business error")
equal(code, 19001, "business error code")
equal(reason, "feishu", "business error reason")
ok, _, reason = core.responseSucceeded(fakeJson, true, "500", "new")
equal(ok, false, "http error")
equal(reason, "http", "http error reason")
ok, _, reason = core.responseSucceeded(fakeJson, false, "timeout", "")
equal(ok, false, "transport error")
equal(reason, "transport", "transport error reason")
ok, _, reason = core.responseSucceeded(fakeJson, true, "200", "invalid")
equal(ok, false, "invalid response")
equal(reason, "invalid_json", "invalid response reason")

equal(core.sameSms(
    {phone = "10086", datetime = "now", text = "hello"},
    {phone = "10086", datetime = "now", text = "hello"}
), true, "same sms")
equal(core.sameSms(
    {phone = "10086", datetime = "now", text = "hello"},
    {phone = "10010", datetime = "now", text = "hello"}
), false, "different sms")

print("PASS: " .. passed .. " assertions")
