# heal_berita_cv.ps1
# Self-healing watchdog untuk RingkasanHarian_BeritaCV_1200

$ErrorActionPreference = "Continue"
$TaskName   = "RingkasanHarian_BeritaCV_1200"
$WorkDir    = "D:\K12 DRIVE\Ringkasan Harian"
$ScriptPath = Join-Path $WorkDir "generate_news.py"
$LogPath    = Join-Path $WorkDir "self_heal_berita_cv.log"
$MaxRuntimeMinutes = 15

function Write-Log {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line  = "[$stamp] $Message"
    Write-Output $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

Write-Log "=== Memulai pengecekan self-healing untuk '$TaskName' ==="

if (-not (Test-Path $WorkDir)) {
    Write-Log "ERROR: Folder kerja '$WorkDir' tidak ditemukan."
    exit 1
}

Set-Location -Path $WorkDir
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $task -or -not $info) {
    Write-Log "WARNING: Task '$TaskName' tidak ditemukan di Task Scheduler. Melanjutkan cek langsung ke skrip & Git."
    $needsHeal = $true
} else {
    Write-Log "Status Task: $($task.State) | LastRunTime: $($info.LastRunTime) | LastTaskResult: $($info.LastTaskResult)"

    $isStuckRunning = $false
    if ($task.State -eq "Running" -and $info.LastRunTime -gt [datetime]::MinValue) {
        $elapsed = ((Get-Date) - $info.LastRunTime).TotalMinutes
        if ($elapsed -ge $MaxRuntimeMinutes) {
            $isStuckRunning = $true
            Write-Log "DETECTED: Task berstatus Running selama $([math]::Round($elapsed, 1)) menit (melebihi batas $MaxRuntimeMinutes menit)."
        }
    }

    $isFailed = ($info.LastTaskResult -ne 0)
    if ($isFailed) {
        Write-Log "DETECTED: LastTaskResult bernilai $($info.LastTaskResult) (bukan 0)."
    }

    $needsHeal = ($isStuckRunning -or $isFailed)
}

if ($needsHeal) {
    Write-Log "ACTION: Menghentikan task '$TaskName' jika masih berjalan..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    if (Test-Path $ScriptPath) {
        Write-Log "ACTION: Menjalankan 'python .\generate_news.py'..."
        $pyOut = & python ".\generate_news.py" 2>&1
        $pyExit = $LASTEXITCODE
        foreach ($line in $pyOut) { Write-Log "PY: $line" }
        Write-Log "INFO: python .\generate_news.py selesai dengan exit code $pyExit."
    } else {
        Write-Log "WARNING: '$ScriptPath' tidak ditemukan; melewati eksekusi Python."
    }
} else {
    Write-Log "OK: Task '$TaskName' tidak terdeteksi gagal atau macet."
}

# Cek perubahan Git dan push otomatis jika ada pembaruan lokal
if (Test-Path (Join-Path $WorkDir ".git")) {
    $gitStatus = & git status --porcelain 2>&1
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($gitStatus -join ""))) {
        Write-Log "ACTION: Terdeteksi perubahan lokal di repo Git. Melakukan add, commit, dan push..."
        $todayStr = Get-Date -Format "yyyy-MM-dd HH:mm"

        & git add . 2>&1 | ForEach-Object { Write-Log "GIT ADD: $_" }
        & git commit -m "Auto-heal update: $todayStr" 2>&1 | ForEach-Object { Write-Log "GIT COMMIT: $_" }

        $pushed = $false
        for ($i = 1; $i -le 2; $i++) {
            Write-Log "GIT PUSH: Percobaan ke-$i ke origin main..."
            $pushOut = & git push origin main 2>&1
            foreach ($line in $pushOut) { Write-Log "GIT PUSH: $line" }
            if ($LASTEXITCODE -eq 0) {
                $pushed = $true
                Write-Log "SUCCESS: Berhasil push ke origin main."
                break
            }
            Start-Sleep -Seconds 5
        }

        if (-not $pushed) {
            Write-Log "ERROR: Gagal push ke origin main setelah 2 percobaan."
        }
    } else {
        Write-Log "INFO: Tidak ada perubahan lokal baru untuk di-push."
    }
} else {
    Write-Log "INFO: Direktori '$WorkDir' bukan repositori Git."
}

Write-Log "=== Selesai pengecekan self-healing ==="