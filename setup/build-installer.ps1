param(
    [Parameter(Mandatory)]
    [string]$NsisCompiler,
    [string]$Version = '0.6.0'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$compiler = (Resolve-Path -LiteralPath $NsisCompiler).Path
$build = Join-Path $repo 'runtime\installer-build'
$payload = Join-Path $build ('payload-' + [Guid]::NewGuid().ToString('N'))
$player = Join-Path $build ('desktop-' + [Guid]::NewGuid().ToString('N'))
$python = Join-Path $payload 'runtime\python'
$worker = Join-Path $payload 'runtime\lyrics-gpu'
$taskbar = Join-Path $payload 'setup\taskbar'
$zip = Join-Path $build 'python-3.13.16-embed-amd64.zip'
$output = Join-Path $build 'VerseLab-Setup-win-x64.exe'

function Get-CheckedDownload($Url, $Destination, $Sha256) {
    if (!(Test-Path -LiteralPath $Destination) -or (Get-FileHash -LiteralPath $Destination).Hash -ne $Sha256) {
        & curl.exe --fail --location --silent --show-error $Url --output $Destination
        if ($LASTEXITCODE) { throw "Download failed for $Url" }
    }
    if ((Get-FileHash -LiteralPath $Destination).Hash -ne $Sha256) {
        throw "Unexpected checksum for $Destination"
    }
}

New-Item -ItemType Directory -Path $python, $taskbar -Force | Out-Null
Get-CheckedDownload 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip' $zip '97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297'
Expand-Archive -LiteralPath $zip -DestinationPath $python
@('python313.zip', '.', '..\..', '..\python-deps') | Set-Content -LiteralPath (Join-Path $python 'python313._pth') -Encoding ascii

& dotnet publish (Join-Path $repo 'desktop\VerseLab.csproj') -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true -o $player
if ($LASTEXITCODE) { throw 'Verse Lab build failed.' }
& dotnet publish (Join-Path $repo 'experiments\large-model\LargeModelBenchmark.csproj') -c Release -r win-x64 --self-contained true "-p:NuGetLockFilePath=$build\worker-win-x64.lock.json" -o $worker
if ($LASTEXITCODE) { throw 'Transcription worker build failed.' }

Copy-Item -LiteralPath (Join-Path $player 'Verse Lab.exe') -Destination $payload
foreach ($name in @('app.py','audio_alignment.py','audio_tags.py','desktop_bridge.py','gpu_budget.py','lyric_pipeline.py',
                    'setup_transcription.py','transcription.py','config.json','LICENSE','THIRD_PARTY.md')) {
    Copy-Item -LiteralPath (Join-Path $repo $name) -Destination $payload
}
Copy-Item -LiteralPath (Join-Path $repo 'dist') -Destination $payload -Recurse
Copy-Item -LiteralPath (Join-Path $repo 'setup\runtime_dependencies.py') -Destination (Join-Path $payload 'setup')
Copy-Item -LiteralPath (Join-Path $repo 'setup\taskbar\SETUP.md'), (Join-Path $repo 'setup\taskbar\LICENSE.txt'), (Join-Path $repo 'setup\taskbar\taskbar-media-presence.wh.cpp') -Destination $taskbar
Get-CheckedDownload 'https://github.com/MrBoxik/Taskbar-Media-Presence/releases/download/v1.0.37/mb_TaskbarMediaPresence.dll' (Join-Path $taskbar 'mb_TaskbarMediaPresence.dll') '801ce35d547b02231d078c47b36a1635c9d67cb1a69efaa7350437e6e1b5460f'
& $compiler /V3 "/DPAYLOAD=$payload" "/DOUTPUT=$output" "/DVERSION=$Version" (Join-Path $PSScriptRoot 'VerseLab.nsi')
if ($LASTEXITCODE) { throw 'Installer compilation failed.' }
Get-FileHash -LiteralPath $output
