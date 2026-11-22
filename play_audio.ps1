param(
    [Parameter(Mandatory=$true)]
    [string]$FilePath
)

try {
    Add-Type -AssemblyName presentationCore
    $player = New-Object System.Windows.Media.MediaPlayer
    $absPath = (Resolve-Path $FilePath).Path
    $player.Open([System.Uri]$absPath)
    $player.Play()

    # Wait for media duration with up to 10 seconds timeout
    $timeout = 0
    while (-not $player.NaturalDuration.HasTimeSpan -and $timeout -lt 100) {
        Start-Sleep -Milliseconds 100
        $timeout++
    }

    if ($player.NaturalDuration.HasTimeSpan) {
        $totalMs = [int]$player.NaturalDuration.TimeSpan.TotalMilliseconds
        Start-Sleep -Milliseconds ($totalMs + 300)
    } else {
        # Fallback duration calculated from file size (Edge-TTS is ~5500 bytes/sec)
        $fileLen = (Get-Item $absPath).Length
        $estSec = [math]::Max(3, [int]($fileLen / 5500) + 1)
        Start-Sleep -Seconds $estSec
    }

    $player.Stop()
    $player.Close()
    exit 0
} catch {
    exit 1
}
