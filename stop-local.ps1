$ErrorActionPreference='Stop'
$voiceStatePath=Join-Path $PSScriptRoot '.runtime\service.json'
$voiceState=Get-Content -LiteralPath $voiceStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
$voiceLauncher=Get-CimInstance Win32_Process -Filter "ProcessId=$($voiceState.pid)"
$voiceExpected=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($voiceLauncher -and ($voiceLauncher.ExecutablePath -ne $voiceExpected -or $voiceLauncher.CommandLine -notlike '*-m uvicorn server:app_factory --factory --host 127.0.0.1*')) { throw 'Recorded PID is owned by another process' }
$voiceListener=Get-NetTCPConnection -LocalPort $voiceState.port -State Listen -ErrorAction SilentlyContinue
if ($voiceListener) {
    if ($voiceListener.LocalAddress -ne '127.0.0.1') { throw 'Unexpected listen address' }
    $voiceServer=Get-CimInstance Win32_Process -Filter "ProcessId=$($voiceListener.OwningProcess)"
    if (($voiceServer.ProcessId -ne $voiceState.pid -and $voiceServer.ParentProcessId -ne $voiceState.pid) -or $voiceServer.CommandLine -notlike '*-m uvicorn server:app_factory --factory --host 127.0.0.1*') { throw 'Unexpected voice port owner' }
    Stop-Process -Id $voiceServer.ProcessId
}
if ($voiceLauncher -and (Get-Process -Id $voiceLauncher.ProcessId -ErrorAction SilentlyContinue)) { Stop-Process -Id $voiceLauncher.ProcessId }
Write-Output 'Recorded local voice service stopped'
