param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'Local Data Analysis Agent.lnk'

if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
    }
    Write-Output 'Automatic startup disabled. You can still use run.cmd.'
    exit 0
}

$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $projectRoot 'scripts\start_local.py'
if (!(Test-Path -LiteralPath $pythonWindowless) -or !(Test-Path -LiteralPath $launcher)) {
    throw 'Python environment or launcher is missing. See README.md.'
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindowless
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Start the local data analysis website after Windows sign-in'
$shortcut.Save()
Write-Output "Automatic startup enabled: $shortcutPath"
