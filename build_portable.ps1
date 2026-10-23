$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot 'csharp\PcbPdfOutline.csproj'
$destination = Join-Path $PSScriptRoot 'portable'

dotnet publish $project -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:DebugType=None -p:DebugSymbols=false -o $destination

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Ready: $(Join-Path $destination 'PcbPdfOutline.exe')"
