param([string]$Target)

# Relaunch a .bat file elevated.
#
# Why this exists: "Start-Process -Verb RunAs" pointed at a .bat file
# silently does nothing on a number of Windows builds - the parent
# window just closes with no error. Elevating cmd.exe instead always
# works, because cmd.exe is a real executable and ShellExecute can
# elevate it.
#
# Kept ASCII-only on purpose: PowerShell 5.1 decodes .ps1 files without
# a BOM using the system ANSI code page, so non-ASCII text here would
# corrupt the script.

$ErrorActionPreference = 'Continue'

if (-not $Target) {
    Write-Host 'no target given'
    exit 2
}

$comspec = $env:ComSpec
if (-not $comspec) { $comspec = "$env:SystemRoot\System32\cmd.exe" }

try {
    Start-Process -FilePath $comspec -ArgumentList '/c', ('"' + $Target + '"') -Verb RunAs -ErrorAction Stop
} catch {
    Write-Host ('elevation failed: ' + $_.Exception.Message)
    exit 1
}
exit 0
