param(
    [string]$LuatoolsRoot = (Join-Path $PSScriptRoot '..\.tmp\Luatools'),
    [string]$SmsLibraryPath = ''
)

$ErrorActionPreference = 'Stop'

if (-not $SmsLibraryPath) {
    $resourceRoot = Join-Path $LuatoolsRoot 'resource\8910_script'
    if (-not (Test-Path -LiteralPath $resourceRoot)) {
        throw "Luatools 8910 script resource directory was not found: $resourceRoot"
    }

    $candidate = Get-ChildItem -LiteralPath $resourceRoot -Recurse -Filter 'sms.lua' |
        Where-Object { $_.FullName -match 'script_LuaTask_[^\\]+\\lib\\sms\.lua$' } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw "No legacy Air724 sms.lua was found under: $resourceRoot"
    }
    $SmsLibraryPath = $candidate.FullName
}

$SmsLibraryPath = (Resolve-Path -LiteralPath $SmsLibraryPath).Path
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$content = [System.IO.File]::ReadAllText($SmsLibraryPath, $utf8NoBom)
$marker = 'data = "\2SMSCENTER_UTF8\2"'

if ([regex]::Matches($content, [regex]::Escape($marker)).Count -ge 2) {
    Write-Host "Air724 SMS UTF-8 fallback is already installed: $SmsLibraryPath"
    exit 0
}

$pattern = '(?m)^([ \t]*)data = common\.ucs2beToGb2312\(data:fromHex\(\)\)\r?$'
$matches = [regex]::Matches($content, $pattern)
if ($matches.Count -ne 2) {
    throw "Expected 2 legacy decoder calls, found $($matches.Count): $SmsLibraryPath"
}

$newline = if ($content.Contains("`r`n")) { "`r`n" } else { "`n" }
$patched = [regex]::Replace(
    $content,
    $pattern,
    {
        param($match)
        $indent = $match.Groups[1].Value
        return (
            "${indent}local source = data:fromHex()" + $newline +
            "${indent}data = common.ucs2beToGb2312(source)" + $newline +
            "${indent}if not data or data == `"`" then" + $newline +
            "${indent}    data = `"\2SMSCENTER_UTF8\2`" .. (common.ucs2beToUtf8(source) or `"`")" + $newline +
            "${indent}end"
        )
    }
)

[System.IO.File]::WriteAllText($SmsLibraryPath, $patched, $utf8NoBom)
Write-Host "Installed Air724 SMS UTF-8 fallback: $SmsLibraryPath"
