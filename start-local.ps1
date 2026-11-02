param([int]$Port = 8793, [ValidateRange(1,8)][int]$AudioThreads = 1, [string]$ConfigPath = $env:XIAOYA_VOICE_CONFIG)
$ErrorActionPreference = 'Stop'
$voiceServiceRoot = $PSScriptRoot
# 原工作区内嵌路径改为独立项目的显式私有配置或.runtime默认位置。--yyl
# $voiceWorkspace = [System.IO.Path]::GetFullPath((Join-Path $voiceServiceRoot '..\..'))
$voicePython = Join-Path $voiceServiceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $voicePython)) { throw 'Run local environment preparation first.' }
# $voiceConfig = Join-Path $voiceWorkspace 'ysim\runtime\xiaoya-voice.json'
$voiceConfig = if ($ConfigPath) { [System.IO.Path]::GetFullPath($ConfigPath) } else { Join-Path $voiceServiceRoot '.runtime\config.json' }
if (-not (Test-Path -LiteralPath $voiceConfig)) { throw 'Provide -ConfigPath or prepare the private .runtime/config.json first.' }
$voiceRuntime = Join-Path $voiceServiceRoot '.runtime'
New-Item -ItemType Directory -Path $voiceRuntime -Force | Out-Null
$voiceStatePath = Join-Path $voiceRuntime 'service.json'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'The voice port is already in use; inspect status before starting.' }
if (Test-Path -LiteralPath $voiceStatePath) {
    $voicePrior = Get-Content -LiteralPath $voiceStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (Get-Process -Id $voicePrior.pid -ErrorAction SilentlyContinue) { throw 'The recorded voice process is still running; verify it before restarting.' }
}
$env:XIAOYA_VOICE_CONFIG = $voiceConfig
# 保留模型线程/音色参数，局部限制响度滤镜线程以便与下一段推理共用CPU。--yyl
$env:XIAOYA_VOICE_FFMPEG_THREADS = [string]$AudioThreads
$voiceProcess = Start-Process -FilePath $voicePython -ArgumentList @('-m','uvicorn','server:app_factory','--factory','--host','127.0.0.1','--port',"$Port",'--no-access-log') -WorkingDirectory $voiceServiceRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $voiceRuntime 'stdout.log') -RedirectStandardError (Join-Path $voiceRuntime 'stderr.log')
@{ pid=$voiceProcess.Id; port=$Port; bind='127.0.0.1'; startedAt=[DateTime]::UtcNow.ToString('o'); executable=$voicePython } | ConvertTo-Json | Set-Content -LiteralPath $voiceStatePath -Encoding UTF8
Write-Output ('Voice service starting, PID ' + $voiceProcess.Id + ', loopback port ' + $Port)
