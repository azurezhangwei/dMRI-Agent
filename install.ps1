# Native Windows bootstrap. Installs no global Python, Node.js, npm or shell profile.
[CmdletBinding()]
param(
    [string]$Slicer,
    [string]$Workspace,
    [string]$InstallDir,
    [string]$PythonPath,
    [int]$BackendPort = 8765,
    [switch]$NoSlicerRegister,
    [switch]$NoRuntimeDownloads,
    [switch]$ForceManagedPython,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$Yes,
    [switch]$Check,
    [switch]$PlatformInfo
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Use install.sh on macOS or Linux.' }
if ([Environment]::Is64BitProcess -eq $false -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'This installer requires x64 Windows and 64-bit PowerShell.'
}
if ($BackendPort -lt 1 -or $BackendPort -gt 65535) { throw 'Invalid backend port.' }
if ($PlatformInfo) { Write-Output 'windows:x86_64'; exit 0 }
$AgentRoot = Join-Path $env:LOCALAPPDATA 'dMRI-Agent'
$RuntimeRoot = Join-Path $AgentRoot 'runtime'
$PythonRoot = Join-Path $RuntimeRoot 'python'
$SelectedPython = $null
$SavedPythonUtf8 = $env:PYTHONUTF8
$SavedLocalAppData = $env:LOCALAPPDATA
# Resolve directory handles before passing paths to uv. MSIX hosts may redirect
# LocalAppData while an ordinary Slicer process sees only the physical location.
if (-not ('DmriAgentFinalPath' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class DmriAgentFinalPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint size, uint flags);
    public static string DirectoryPath(string path) {
        using (var handle = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            var buffer = new StringBuilder(32768);
            uint size = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
            if (size == 0 || size >= buffer.Capacity) throw new Win32Exception(Marshal.GetLastWin32Error());
            string result = buffer.ToString();
            if (result.StartsWith(@"\\?\UNC\")) return @"\\" + result.Substring(8);
            return result.StartsWith(@"\\?\") ? result.Substring(4) : result;
        }
    }
}
'@
}
function Test-AgentPython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if ($Candidate -like '*\Microsoft\WindowsApps\*') { return $false }
    try {
        & $Candidate -c 'import ssl,struct,sys,venv;sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize(chr(80))==8 else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}
function Get-AgentPython([string]$Candidate) {
    if (-not (Test-AgentPython $Candidate)) { return $null }
    $encoded = & $Candidate -c 'import base64,os,sys;print(base64.b64encode(os.path.realpath(sys.executable).encode()).decode())'
    if ($LASTEXITCODE -ne 0) { return $null }
    $resolved = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
    if (-not (Test-AgentPython $resolved)) { return $null }
    return [string]$resolved
}
function Protect-AgentDirectory([string]$Directory) {
    if (Test-Path -LiteralPath $Directory) {
        $item = Get-Item -LiteralPath $Directory -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Runtime directories cannot be reparse points.' }
    } else { New-Item -ItemType Directory -Path $Directory -Force | Out-Null }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $Directory
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne $sid.Value -and $ownerSid -ne 'S-1-5-32-544') { throw 'Runtime directory is owned by another account.' }
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
    foreach ($principal in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}
if ($PythonPath) {
    $PythonPath = [IO.Path]::GetFullPath($PythonPath)
    $SelectedPython = Get-AgentPython $PythonPath
    if (-not $SelectedPython) { throw '-PythonPath must point to a working 64-bit Python 3.12.' }
}
if (-not $SelectedPython -and (Test-Path -LiteralPath $PythonRoot)) {
    foreach ($folder in Get-ChildItem -LiteralPath $PythonRoot -Directory) {
        if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $candidate = Join-Path $folder.FullName 'python.exe'
        $SelectedPython = Get-AgentPython $candidate
        if ($SelectedPython) { break }
    }
}
if (-not $SelectedPython -and -not $ForceManagedPython) {
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $SelectedPython = Get-AgentPython $command.Source }
        if ($SelectedPython) { break }
    }
}
if (-not $SelectedPython) {
    if ($NoRuntimeDownloads -or $Uninstall -or $Check) { throw 'Python 3.12 is unavailable; runtime downloads are disabled for this operation.' }
    Protect-AgentDirectory $AgentRoot
    $AgentRoot = [DmriAgentFinalPath]::DirectoryPath($AgentRoot)
    $RuntimeRoot = Join-Path $AgentRoot 'runtime'
    $PythonRoot = Join-Path $RuntimeRoot 'python'
    foreach ($folder in @($RuntimeRoot, $PythonRoot)) { Protect-AgentDirectory $folder }
    $staging = Join-Path $RuntimeRoot ('bootstrap-' + [Guid]::NewGuid().ToString('N'))
    Protect-AgentDirectory $staging
    try {
        $archive = Join-Path $staging 'uv.zip'
        $url = 'https://github.com/astral-sh/uv/releases/download/0.12.0/uv-x86_64-pc-windows-msvc.zip'
        $expected = '68200e25de594df92387186bbfb9d9df606ec1d87efaa0ae0c7f690970e53db6'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'Python bootstrap download failed SHA-256 verification.' }
        Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $staging 'uv')
        $uv = @(Get-ChildItem -LiteralPath (Join-Path $staging 'uv') -Filter 'uv.exe' -Recurse)[0].FullName
        $env:UV_PYTHON_INSTALL_DIR = $PythonRoot
        $env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeRoot 'python-bin'
        $env:UV_CACHE_DIR = Join-Path $RuntimeRoot 'uv-cache'
        $env:UV_PYTHON_INSTALL_REGISTRY = 'false'
        $env:UV_PYTHON_INSTALL_BIN = 'false'
        & $uv python install '3.12.13'
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare private Python 3.12.13.' }
        foreach ($folder in Get-ChildItem -LiteralPath $PythonRoot -Directory) {
            if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $candidate = Join-Path $folder.FullName 'python.exe'
            $SelectedPython = Get-AgentPython $candidate
            if ($SelectedPython) { break }
        }
        if (-not $SelectedPython) { throw 'Downloaded Python could not start.' }
    } finally {
        if ([IO.Path]::GetFullPath($staging).StartsWith([IO.Path]::GetFullPath($RuntimeRoot) + '\', [StringComparison]::OrdinalIgnoreCase) -ne $true) { throw 'Unsafe runtime staging cleanup path.' }
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    }
}
$arguments = @((Join-Path $PSScriptRoot 'scripts/native_install.py'), '--backend-port', [string]$BackendPort)
if ($Slicer) { $arguments += @('--slicer', $Slicer) }
if ($Workspace) { $arguments += @('--workspace', $Workspace) }
if ($InstallDir) { $arguments += @('--install-dir', $InstallDir) }
if ($NoSlicerRegister) { $arguments += '--no-slicer-register' }
if ($Uninstall) { $arguments += '--uninstall' }
if ($Purge) { $arguments += '--purge' }
if ($Yes) { $arguments += '--yes' }
if ($Check) { $arguments += '--check' }
try {
    if (-not $Uninstall -and -not $Check -and -not (Test-Path -LiteralPath $AgentRoot)) { Protect-AgentDirectory $AgentRoot }
    if (Test-Path -LiteralPath $AgentRoot -PathType Container) {
        if ((Get-Item -LiteralPath $AgentRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Agent directory cannot be a reparse point.' }
        # Reuse exactly the same physical config/state layout when the selected
        # private Python runs outside the parent's MSIX redirection context.
        $env:LOCALAPPDATA = Split-Path ([DmriAgentFinalPath]::DirectoryPath($AgentRoot))
    }
    $env:PYTHONUTF8 = '1'
    & $SelectedPython @arguments
    if ($LASTEXITCODE -ne 0) { throw 'dMRI-Agent operation did not complete. Review the message above.' }
} finally {
    if ($null -eq $SavedPythonUtf8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue }
    else { $env:PYTHONUTF8 = $SavedPythonUtf8 }
    $env:LOCALAPPDATA = $SavedLocalAppData
}
