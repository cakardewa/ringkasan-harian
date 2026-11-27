# heal_ringkasan_harian.ps1
# Self-healing gabungan untuk Berita CV dan Email Harian dengan satu kali git push di akhir

$ErrorActionPreference = "Continue"
$WorkDir  = "D:\K12 DRIVE\Ringkasan Harian"
$LogPath  = Join-Path $WorkDir "self_heal_ringkasan_harian.log"
$MaxRuntimeMinutes = 15

# Daftar target task dan skrip terkaitnya
$Targets = @(
    @{
        Name       = "RingkasanHarian_BeritaCV_1200"
        ScriptName = "generate_news.py"
        Args       = @()
    },
    @{
        Name       = "RingkasanHarian_Email_0700"
        ScriptName = "generate_news.py"
        Args       = @("--mode", "email")
    }
)

function Write-Log {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line  = "[$stamp] $Message"
    Write-Output $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Invoke-TaskHealthCheck {
    param([hashtable]$Target)

    $taskName   = $Target.Name
    $scriptPath = Join-Path $WorkDir $Target.ScriptName
    Write-Log "--- Cek target '$taskName' ---"

    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue

    $needsHeal = $false
    if (-not $task -or -not $info) {
        Write-Log "WARNING: Task '$taskName' tidak ditemukan. Jalankan skrip cadangan jika tersedia."
        $needsHeal = $true
    } else {
        Write-Log "State: $($task.State) | LastRunTime: $($info.LastRunTime) | LastTaskResult: $($info.LastTaskResult)"
        if ($task.State -eq "Running" -and $info.LastRunTime -gt [datetime]::MinValue) {
            $elapsed = ((Get-Date) - $info.LastRunTime).TotalMinutes
            if ($elapsed -ge $MaxRuntimeMinutes) {
                $needsHeal = $true
                Write-Log "DETECTED: Task '$taskName' menyangkut Running selama $([math]::Round($elapsed, 1)) menit."
            }
        }
        if ($info.LastTaskResult -ne 0) {
            $needsHeal = $true
            Write-Log "DETECTED: LastTaskResult '$taskName' bernilai $($info.LastTaskResult)."
        }
    }

    if ($needsHeal) {
        Write-Log "ACTION: Menghentikan '$taskName' jika masih aktif..."
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3

        if (Test-Path $scriptPath) {
            Write-Log "ACTION: Menjalankan '$($Target.ScriptName) $($Target.Args -join ' ')'..."
            $pyOut  = & python ".\$($Target.ScriptName)" @($Target.Args) 2>&1
            $pyExit = $LASTEXITCODE
            foreach ($line in $pyOut) { Write-Log "PY [$taskName]: $line" }
            Write-Log "INFO: Selesai '$taskName' dengan exit code $pyExit."
        } else {
            Write-Log "WARNING: File '$scriptPath' tidak ditemukan."
        }
    } else {
        Write-Log "OK: Task '$taskName' sehat."
    }
}

Write-Log "=== Mulai pengecekan gabungan Ringkasan Harian ==="

if (-not (Test-Path $WorkDir)) {
    Write-Log "ERROR: Folder '$WorkDir' tidak ditemukan."
    exit 1
}
Set-Location -Path $WorkDir

foreach ($t in $Targets) {
    Invoke-TaskHealthCheck -Target $t
}

# Satu kali cek & push Git di akhir agar tidak double commit/push
if (Test-Path (Join-Path $WorkDir ".git")) {
    # Hindari file internal skrip/log ikut ter-commit bila belum di-ignore
    $dirtyFiles = & git status --porcelain 2>&1 | Where-Object {
        $_ -notmatch "heal_berita_cv\.ps1$" -and
        $_ -notmatch "heal_ringkasan_harian\.ps1$" -and
        $_ -notmatch "self_heal_.*\.log$"
    }

    if ($dirtyFiles) {
        Write-Log "ACTION: Ada perubahan konten/output. Melakukan satu kali add, commit, dan push..."
        $stampCommit = Get-Date -Format "yyyy-MM-dd HH:mm"

        foreach ($entry in $dirtyFiles) {
            $relPath = ($entry -replace "^...\s*", "").Trim()
            if (Test-Path $relPath) {
                & git add -- "$relPath" 2>&1 | ForEach-Object { Write-Log "GIT ADD: $_" }
            }
        }

        & git commit -m "Auto-heal ringkasan harian: $stampCommit" 2>&1 | ForEach-Object { Write-Log "GIT COMMIT: $_" }

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
        Write-Log "INFO: Tidak ada perubahan file konten yang perlu di-commit/push."
    }
}

Write-Log "=== Selesai pengecekan gabungan ==="