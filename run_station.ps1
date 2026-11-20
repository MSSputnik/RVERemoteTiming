# Starts the app for one station with its own config file config_<station>.json.
# If that file does not exist yet, it is created from config.example.json with
# the station set and the username cleared. Everything else is copied as is.
#
# Usage: .\run_station.ps1 -Station start|3000m|6000m|regattaleitung [-PrepareOnly]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "3000m", "6000m", "regattaleitung")]
    [string]$Station,

    # Only create the config file, do not start the app.
    [switch]$PrepareOnly
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$config = "config_$Station.json"
$example = "config.example.json"

if (-not (Test-Path -LiteralPath $config)) {
    if (-not (Test-Path -LiteralPath $example)) {
        throw "Cannot create $config, $example not found in $PSScriptRoot."
    }
    # Edit the text instead of parsing the JSON, so the layout stays unchanged.
    $text = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $example), [Text.Encoding]::UTF8)
    $text = $text -replace '("station"\s*:\s*)"[^"]*"', "`$1`"$Station`""
    $text = $text -replace '("username"\s*:\s*)"[^"]*"', '$1""'
    # UTF-8 without BOM, the app's JSON reader rejects a BOM.
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot $config), $text, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Created $config from $example (station $Station)."
    Write-Host 'Enter client ID, region and base URL with "Set Login Parameters ..." in the app.'
}

if ($PrepareOnly) { return }

$env:RVE_TIMING_CONFIG = $config
uv run python -m rve_timing
