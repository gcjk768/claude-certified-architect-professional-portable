# Local run on Windows: loads bot.env then starts the bot (Ctrl+C to stop).
Get-Content "$PSScriptRoot\bot.env" | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
}
python "$PSScriptRoot\bot.py"
