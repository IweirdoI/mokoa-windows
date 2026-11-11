# 当前 PowerShell 会话的 Windows 开发环境。不写注册表，不影响麒麟。
# 用法: . .\scripts\windows_env.ps1

$Root = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $Root
if (-not $env:OS_AGENT_MEMORY_BACKEND) {
    $env:OS_AGENT_MEMORY_BACKEND = "memory"
}
if (-not $env:OS_AGENT_EMBEDDING_BACKEND) {
    $env:OS_AGENT_EMBEDDING_BACKEND = "hash"
}
