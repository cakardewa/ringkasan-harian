@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "D:\K12 DRIVE\Ringkasan Harian"

echo ========================================================
echo [%date% %time%] Memulai sinkronisasi ^& publikasi GitHub...
echo ========================================================
echo [%date% %time%] Memeriksa perubahan berkas... >> sync_publish.log

git add -A >> sync_publish.log 2>&1
git reset -- *.log >nul 2>&1

git diff --cached --quiet
if %errorlevel% neq 0 (
    echo [INFO] Menyimpan commit perubahan terbaru...
    git commit -m "Publish digest updates %date% %time%" >> sync_publish.log 2>&1
    if !errorlevel! neq 0 (
        echo [ERROR] Gagal membuat commit Git. Periksa sync_publish.log.
        echo [%date% %time%] [ERROR] Gagal membuat commit Git. >> sync_publish.log
        timeout /t 6
        exit /b 1
    )
) else (
    echo [INFO] Tidak ada perubahan file baru untuk di-commit. Memeriksa push tertunda...
    echo [%date% %time%] Tidak ada perubahan baru untuk di-commit. >> sync_publish.log
)

echo [INFO] Menyelaraskan dengan origin/main...
git pull --rebase --autostash origin main >> sync_publish.log 2>&1

set /a attempt=1
:push_loop
echo [INFO] Mengirim pembaruan ke GitHub Pages (percobaan !attempt!/3)...
git push origin main >> sync_publish.log 2>&1
if %errorlevel% equ 0 (
    echo [OK] Berhasil push ke origin/main!
    echo [%date% %time%] [OK] Berhasil push ke origin/main. >> sync_publish.log
    timeout /t 5
    exit /b 0
)

if !attempt! geq 3 (
    echo [ERROR] Gagal push setelah 3 percobaan. Cek sync_publish.log.
    echo [%date% %time%] [ERROR] Gagal push setelah 3 percobaan. Cek sync_publish.log >> sync_publish.log
    timeout /t 8
    exit /b 1
)

set /a attempt+=1
echo [WARN] Push gagal, mencoba ulang percobaan !attempt!/3 dalam 15 detik...
echo [%date% %time%] [WARN] Push gagal, mencoba ulang percobaan !attempt!/3 dalam 15 detik... >> sync_publish.log
timeout /t 15 /nobreak >nul
goto push_loop