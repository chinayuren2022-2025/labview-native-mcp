param(
  [string]$InstallDir = "$env:LOCALAPPDATA\LabVIEWNativeMCP",
  [string]$Python = "",
  [string]$LabVIEWHome = "$env:LABVIEW_HOME",
  [string]$LabVIEWVersion = "$env:LABVIEW_VERSION",
  [switch]$SkipProbe
)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
  & $Executable @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}
if (-not $Python) {
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) {
    $candidate = & $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0) { $Python = "$candidate".Trim() }
  }
  if (-not $Python) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path $candidate) { $Python = $candidate }
  }
}
if (-not $Python -or -not (Test-Path $Python) -or $Python -match 'WindowsApps') {
  throw 'Python 3.12 x64 not found. Install from python.org, or pass -Python with its actual python.exe path.'
}
Invoke-Checked $Python @('-c', 'import sys,struct; assert sys.version_info[:2]==(3,12), "Python 3.12 required"; assert struct.calcsize("P")==8, "64-bit Python required"')
if ($LabVIEWHome) { $env:LABVIEW_HOME = $LabVIEWHome }
if ($LabVIEWVersion) { $env:LABVIEW_VERSION = $LabVIEWVersion }
$source = $PSScriptRoot
$target = [IO.Path]::GetFullPath($InstallDir)
if ($source.TrimEnd('\') -eq $target.TrimEnd('\')) { throw 'InstallDir must differ from the source checkout.' }
New-Item -ItemType Directory -Force $target | Out-Null
# Copy only release inputs; do not import previous workers, user VIs or credentials.
foreach ($file in @('README.md','LICENSE','requirements.txt')) { Copy-Item (Join-Path $source $file) $target -Force }
New-Item -ItemType Directory -Force (Join-Path $target 'tools'), (Join-Path $target 'work\lvscript') | Out-Null
Copy-Item (Join-Path $source 'tools\*.py') (Join-Path $target 'tools') -Force
Copy-Item (Join-Path $source 'tools\*.json') (Join-Path $target 'tools') -Force
Copy-Item (Join-Path $source 'work\lvscript\*') (Join-Path $target 'work\lvscript') -Recurse -Force
$venv = Join-Path $target '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { Invoke-Checked $Python @('-m','venv',$venv) }
$runtime = Join-Path $venv 'Scripts\python.exe'
Invoke-Checked $runtime @('-m','pip','install','-r',(Join-Path $target 'requirements.txt'))
$discovery = & $runtime -c 'import json, sys; sys.path.insert(0, sys.argv[1]); from lvscript import _discover_labview, _installation_identity; print(json.dumps(_installation_identity(_discover_labview())))' (Join-Path $target 'tools')
if ($LASTEXITCODE -ne 0) { throw 'LabVIEW installation discovery failed; see diagnostics above.' }
$selected = ("$discovery".Trim() | ConvertFrom-Json)
$LabVIEWHome = [string]$selected.home
$LabVIEWVersion = [string]$selected.year
$serverName = "labview-native-$LabVIEWVersion"
$env:LABVIEW_HOME = $LabVIEWHome
$env:LABVIEW_VERSION = $LabVIEWVersion
# SkipProbe skips both COM-dependent steps, useful for offline packaging only.
if (-not $SkipProbe) {
  Invoke-Checked $runtime @((Join-Path $target 'tools\warmup.py'))
  Invoke-Checked $runtime @((Join-Path $target 'tools\install_probe.py'))
}
$config = @{ mcpServers = @{ $serverName = @{
  command = $runtime.Replace('\','/')
  args = @((Join-Path $target 'tools\labview_mcp.py').Replace('\','/'))
  env = @{ LABVIEW_HOME = $LabVIEWHome.Replace('\','/'); LABVIEW_VERSION = $LabVIEWVersion }
} } } | ConvertTo-Json -Depth 6
$configPath = Join-Path $target 'mcp-config.json'
[IO.File]::WriteAllText($configPath, $config, (New-Object Text.UTF8Encoding($false)))
Write-Output "Installed: $target"
Write-Output "Selected LabVIEW: $LabVIEWVersion ($($selected.bitness)-bit) at $LabVIEWHome"
Write-Output "MCP server name: $serverName"
Write-Output "MCP configuration fragment: $configPath"
Write-Output $config
