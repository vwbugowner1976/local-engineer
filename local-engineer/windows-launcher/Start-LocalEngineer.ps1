param(
    [Parameter(Position=0)][string]$Task,
    [string]$Project = 'prospector',
    [switch]$Inspect,
    [string]$Resume
)
$ErrorActionPreference = 'Stop'
if (-not $Task -and -not $Resume) {
    throw 'Specify a task, or -Resume with a Mac checkpoint path.'
}
$request = @{ task=$Task; project=$Project; mode=$(if ($Inspect) {'inspect'} else {'fix'}); resume=$Resume }
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Compress)))
$helper = Join-Path $PSScriptRoot 'local_engineer_connect.py'
$helperForWsl = $helper.Replace('\','/')
$helperResult = & wsl -d Ubuntu-24.04 -- wslpath -u $helperForWsl
if ($LASTEXITCODE -ne 0 -or -not $helperResult) { throw 'Could not resolve the WSL launcher path.' }
$helperWsl = $helperResult.Trim()
& wsl -d Ubuntu-24.04 -- python3 $helperWsl $payload
exit $LASTEXITCODE
