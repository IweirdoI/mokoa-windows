# Windows 开发环境。默认装核心依赖并用 hash 向量做接线冒烟。
# 语义检索: .\scripts\setup_windows.ps1 -Profile full
# 麒麟真机不要运行本脚本，继续使用 requirements.txt 与 scripts/*.sh。

param(
    [ValidateSet("core", "full")]
    [string]$Profile = "core"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Resolve-Python312 {
    $candidates = @(
        @{ Cmd = "py"; Args = @("-3.12") },
        @{ Cmd = "python"; Args = @() }
    )
    foreach ($item in $candidates) {
        try {
            $version = & $item.Cmd @($item.Args + @("-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')"))
            if ($LASTEXITCODE -eq 0 -and $version -match "^3\.12\.") {
                return @{ Cmd = $item.Cmd; Args = $item.Args; Version = $version.Trim() }
            }
        } catch {
            continue
        }
    }
    throw "需要 Python 3.12。请安装后确认 py -3.12 或 python 可用。"
}

$python = Resolve-Python312
Write-Host "使用 Python $($python.Version)"

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $python.Cmd @($python.Args + @("-m", "venv", ".venv"))
    if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败" }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败" }

$requirements = if ($Profile -eq "full") { "requirements.txt" } else { "requirements-windows.txt" }
Write-Host "安装 $requirements"
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败: $requirements" }

$envExample = Join-Path $Root ".env.example"
$envFile = Join-Path $Root ".env"
if ((-not (Test-Path $envFile)) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Host "已从 .env.example 复制 .env。密钥留在本机，不要提交。"
}

. (Join-Path $PSScriptRoot "windows_env.ps1")
& $venvPython scripts\smoke_e2e.py
if ($LASTEXITCODE -ne 0) { throw "smoke_e2e 未通过" }

Write-Host ""
Write-Host "Windows 环境就绪（memory + hash，只验证接线）。"
Write-Host "启动 API: .\scripts\dev_windows.ps1"
Write-Host "BGE 语义检索: .\scripts\setup_windows.ps1 -Profile full，然后 .\scripts\dev_windows.ps1 -Embedding local"
