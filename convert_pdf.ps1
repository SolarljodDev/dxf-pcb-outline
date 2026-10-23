param(
    [Parameter(Position = 0)] [string] $InputPdf,
    [Alias('o')] [string] $Output,
    [switch] $NoHoles,
    [switch] $NoBoard,
    [switch] $NoSubtractHoles
)

$ErrorActionPreference = 'Stop'
$program = Join-Path $PSScriptRoot 'portable\PcbPdfOutline.exe'
if (-not (Test-Path -LiteralPath $program)) {
    throw "EXE not found: $program. Build it with .\build_portable.ps1 (.NET SDK 8 required)."
}

if ([string]::IsNullOrWhiteSpace($InputPdf)) {
    Start-Process -FilePath $program
    return
}

$arguments = @('"' + (Resolve-Path -LiteralPath $InputPdf).Path + '"')
if ($Output) {
    $arguments += '-o'
    $arguments += '"' + [System.IO.Path]::GetFullPath($Output) + '"'
}
if ($NoHoles) { $arguments += '--no-holes' }
if ($NoBoard) { $arguments += '--no-board' }
if ($NoSubtractHoles) { $arguments += '--no-subtract-holes' }
if (-not $Output) { $arguments += '--convert' }

$process = Start-Process -FilePath $program -ArgumentList $arguments -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Conversion failed with exit code $($process.ExitCode)." }
Write-Host 'DXF created.'
