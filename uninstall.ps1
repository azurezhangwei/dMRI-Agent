[CmdletBinding()]
param([switch]$Purge, [switch]$Yes, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$arguments = @{ Uninstall = $true; NoRuntimeDownloads = $true }
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.dmri-agent-install') -PathType Leaf) {
    $arguments.InstallDir = $PSScriptRoot
}
if ($Purge) { $arguments.Purge = $true }
if ($Yes) { $arguments.Yes = $true }
if ($PythonPath) { $arguments.PythonPath = $PythonPath }
& (Join-Path $PSScriptRoot 'install.ps1') @arguments
