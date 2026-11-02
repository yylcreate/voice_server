$ErrorActionPreference='Stop'
$voiceStatePath=Join-Path $PSScriptRoot '.runtime\service.json'
if (-not (Test-Path -LiteralPath $voiceStatePath)) { throw 'No recorded local service' }
$voiceState=Get-Content -LiteralPath $voiceStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
$voiceListener=Get-NetTCPConnection -LocalPort $voiceState.port -State Listen -ErrorAction SilentlyContinue
if (-not $voiceListener) { Write-Output 'Voice service is not listening'; exit 1 }
if ($voiceListener.LocalAddress -ne '127.0.0.1') { throw 'Unexpected listen address' }
$voiceProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($voiceListener.OwningProcess)"
if (($voiceProcess.ProcessId -ne $voiceState.pid -and $voiceProcess.ParentProcessId -ne $voiceState.pid) -or $voiceProcess.CommandLine -notlike '*-m uvicorn server:app_factory --factory --host 127.0.0.1*') { throw 'Unexpected port owner' }
$voiceHealth=Invoke-RestMethod -Uri "http://127.0.0.1:$($voiceState.port)/health" -TimeoutSec 5
$voiceMemory=Get-Process -Id $voiceProcess.ProcessId
[pscustomobject]@{ready=$voiceHealth.ready;active=$voiceHealth.active;protocol=$voiceHealth.protocol;externalTts=$voiceHealth.external_tts;pid=$voiceProcess.ProcessId;bind=$voiceListener.LocalAddress;port=$voiceState.port;workingSetMiB=[math]::Round($voiceMemory.WorkingSet64/1MB,1)} | ConvertTo-Json
