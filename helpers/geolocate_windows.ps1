# Windows native geolocation (Level 2).
# Calls Windows.Devices.Geolocation.Geolocator to get a Wi-Fi based position.
# Prints exactly one line of JSON so Python can parse it. Always exits 0;
# the "ok" field reports success.
# Must run under Windows PowerShell 5.1 (powershell.exe); PowerShell 7 has
# incomplete WinRT support.
param(
    [int]$TimeoutSeconds = 12
)

$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # Ignore: older consoles may not support this, and it does not affect locating.
}

function Write-Result {
    param($Object)
    $Object | ConvertTo-Json -Compress -Depth 5
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    # Find the AsTask extension for IAsyncOperation<T> so WinRT async calls can be awaited.
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
    if ($null -eq $asTaskGeneric) {
        throw 'AsTask extension for IAsyncOperation was not found; cannot await geolocation.'
    }

    function Await-Operation {
        param($WinRtTask, [Type]$ResultType, [int]$TimeoutMs)
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        if (-not $netTask.Wait($TimeoutMs)) {
            throw "Geolocation timed out after ${TimeoutMs} ms."
        }
        return $netTask.Result
    }

    [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime] | Out-Null

    $locator = [Windows.Devices.Geolocation.Geolocator]::new()
    $locator.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High

    # Ask for access. An unpackaged desktop app may throw or return Unspecified;
    # that does not prove positioning will fail, so continue and try anyway.
    try {
        $access = Await-Operation ($locator.RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus]) 8000
        if ($access -eq [Windows.Devices.Geolocation.GeolocationAccessStatus]::Denied) {
            Write-Result ([ordered]@{
                    ok     = $false
                    error  = 'Location access denied (GeolocationAccessStatus=Denied).'
                    source = 'windows_geolocator'
                })
            exit 0
        }
    } catch {
        # Ignore access-request failures and try to read a position anyway.
    }

    $position = Await-Operation `
        ($locator.GetGeopositionAsync([TimeSpan]::FromMinutes(5), [TimeSpan]::FromSeconds($TimeoutSeconds))) `
        ([Windows.Devices.Geolocation.Geoposition]) `
        (($TimeoutSeconds + 5) * 1000)

    if ($null -eq $position) {
        throw 'Geolocation returned an empty result.'
    }

    $coord = $position.Coordinate
    $point = $coord.Point.Position

    Write-Result ([ordered]@{
            ok        = $true
            latitude  = [math]::Round([double]$point.Latitude, 6)
            longitude = [math]::Round([double]$point.Longitude, 6)
            accuracy  = [math]::Round([double]$coord.Accuracy, 1)
            altitude  = if ($null -ne $point.Altitude) { [math]::Round([double]$point.Altitude, 1) } else { $null }
            timestamp = $position.Coordinate.Timestamp.ToString('o')
            source    = 'windows_geolocator'
        })
} catch {
    Write-Result ([ordered]@{
            ok     = $false
            error  = $_.Exception.Message
            source = 'windows_geolocator'
        })
}
exit 0
