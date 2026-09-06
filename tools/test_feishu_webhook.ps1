$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot 'device/config.lua'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'device/config.lua does not exist.'
}

$configText = Get-Content -LiteralPath $configPath -Raw
$tokenMatch = [regex]::Match($configText, 'feishu_bot_token\s*=\s*"([^"]*)"')
if (-not $tokenMatch.Success -or [string]::IsNullOrWhiteSpace($tokenMatch.Groups[1].Value)) {
    throw 'feishu_bot_token is empty. Fill it in device/config.lua first.'
}

$token = $tokenMatch.Groups[1].Value.Trim()
if ($token.Contains('/')) {
    throw 'Put only the value after /hook/ in feishu_bot_token.'
}

$uri = 'https://open.feishu.cn/open-apis/bot/v2/hook/' + $token
$payload = @{
    msg_type = 'text'
    content = @{
        text = "短信转发本机连通测试`n时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    }
} | ConvertTo-Json -Depth 4 -Compress

try {
    $response = Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json; charset=utf-8' -Body $payload -TimeoutSec 30
} catch {
    $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'network error' }
    throw "Feishu webhook request failed ($status). The token was not printed."
}

$businessCode = if ($null -ne $response.code) { $response.code } else { $response.StatusCode }
if ([int]$businessCode -ne 0) {
    throw "Feishu rejected the request (business code: $businessCode)."
}

Write-Host 'PASS: Feishu webhook accepted the local test message.'
