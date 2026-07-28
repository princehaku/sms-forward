-- Shared, runtime-independent helpers for the SMS Center client.
local M = {}

local function valueOrUnknown(value)
    if value == nil or tostring(value) == "" then
        return "未知"
    end
    return tostring(value)
end

function M.trim(value)
    return (tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

function M.formatMessage(deviceName, phoneNumber, smsText, smsDatetime)
    return table.concat({
        "【短信转发】",
        "设备：" .. valueOrUnknown(deviceName),
        "发件人：" .. valueOrUnknown(phoneNumber),
        "时间：" .. valueOrUnknown(smsDatetime),
        "内容：",
        tostring(smsText or "")
    }, "\n")
end

function M.makePayload(jsonLib, text)
    return jsonLib.encode({
        msg_type = "text",
        content = {text = text}
    })
end

function M.responseSucceeded(jsonLib, result, statusCode, body)
    if not result then
        return false, nil, "transport"
    end

    local httpCode = tonumber(statusCode)
    if not httpCode or httpCode < 200 or httpCode >= 300 then
        return false, nil, "http"
    end

    local response, decoded = jsonLib.decode(body or "")
    if not decoded or type(response) ~= "table" then
        return false, nil, "invalid_json"
    end

    local businessCode = response.code
    if businessCode == nil then
        businessCode = response.StatusCode
    end

    if tonumber(businessCode) == 0 then
        return true, 0, nil
    end
    return false, businessCode, "feishu"
end

function M.sameSms(left, right)
    if type(left) ~= "table" or type(right) ~= "table" then
        return false
    end
    return tostring(left.phone or "") == tostring(right.phone or "")
        and tostring(left.datetime or "") == tostring(right.datetime or "")
        and tostring(left.text or "") == tostring(right.text or "")
end

return M
