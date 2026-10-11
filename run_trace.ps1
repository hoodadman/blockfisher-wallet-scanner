param(
    [string]$Config = ".\config.json",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "Python is not installed or not on PATH. Install Python 3.10+ first."
    exit 1
}

if (-not $Args -or $Args.Count -eq 0) {
    $Args = @("stats")
}

$env:PYTHONIOENCODING = "utf-8"
$callArgs = @(".\trace.py", "--config", $Config) + $Args
python @callArgs
