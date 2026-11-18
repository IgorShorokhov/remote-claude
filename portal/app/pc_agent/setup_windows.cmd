@echo off
rem Claude Portal setup for Windows. Double-click this file. It installs for your user only and needs no administrator rights.
title Claude Portal setup
set "CP_SELF=%~f0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:CP_SELF); $i=$t.IndexOf('#'+'PS-BEGIN'); Invoke-Expression ($t.Substring($i))"
echo.
pause
exit /b
#PS-BEGIN
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
$Portal = '@PORTAL@'
$AgentB64 = '@AGENT_B64@'
$Python = @{ AMD64 = @{ Url = '@PYTHON_AMD64_URL@'; Sha = '@PYTHON_AMD64_SHA@' }; ARM64 = @{ Url = '@PYTHON_ARM64_URL@'; Sha = '@PYTHON_ARM64_SHA@' } }
$Root = Join-Path $env:LOCALAPPDATA 'ClaudePortal'
Write-Host ''
Write-Host '  Claude Portal: connecting this computer'
Write-Host ('  Portal: ' + $Portal)
Write-Host ''
try {
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  $Agent = Join-Path $Root 'portal_agent.py'
  Get-Process pythonw, python -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Root) } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  [IO.File]::WriteAllBytes($Agent, [Convert]::FromBase64String($AgentB64))
  $Py = Join-Path $Root 'python\python.exe'
  if (-not (Test-Path $Py)) {
    $Arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $Arch = $env:PROCESSOR_ARCHITEW6432 }
    if (-not $Python.ContainsKey($Arch)) { throw ('Unsupported processor type: ' + $Arch) }
    $Info = $Python[$Arch]
    $Zip = Join-Path $Root 'python.zip'
    $Near = Join-Path (Split-Path -Parent $env:CP_SELF) (Split-Path -Leaf $Info.Url)
    if (Test-Path $Near) {
      Write-Host '  Using the Python file next to this script.'
      Copy-Item $Near $Zip -Force
    } else {
      Write-Host '  Downloading Python (about 11 MB)...'
      Invoke-WebRequest -UseBasicParsing -Uri $Info.Url -OutFile $Zip
    }
    if ((Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower() -ne $Info.Sha) {
      Remove-Item $Zip -Force
      throw 'The Python file is damaged (the checksum does not match).'
    }
    Expand-Archive -Path $Zip -DestinationPath (Join-Path $Root 'python') -Force
    Remove-Item $Zip -Force
  }
  & $Py $Agent setup --portal $Portal --label ('Windows ' + $env:COMPUTERNAME)
  if ($LASTEXITCODE -ne 0) { throw ('The setup stopped with exit code ' + $LASTEXITCODE + '.') }
  Write-Host ''
  Write-Host '  Done. You can close this window. The Claude Portal settings opened in your browser.'
  Write-Host '  Later: the Claude icon in the tray (next to the clock) or Claude Portal in the Start menu.'
} catch {
  Write-Host ''
  Write-Host ('  Setup failed: ' + $_.Exception.Message) -ForegroundColor Red
  Write-Host ('  Details are saved in ' + (Join-Path $Root 'activity.log'))
}
