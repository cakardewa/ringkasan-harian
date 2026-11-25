@echo off
setlocal
cd /d "D:\K12 DRIVE\Ringkasan Harian"

echo [%date% %time%] Memeriksa perubahan berkas... >> sync_publish.log
git add -A >> sync_publish.log 2>&1

git diff --cached --quiet
if %errorlevel% neq 0 (
    git commit -m "Publish digest updates %date%" >> sync_publish.log 2>&1
) else (
    echo [%date% %time%] Tidak ada perubahan baru untuk di-commit. >> sync_publish.log
)

set /a attempt=1
:push_loop
git push origin main >> sync_publish.log 2>&1
if %errorlevel% equ 0 (
    echo [%date% %time%] Berhasil push ke origin/main. >> sync_publish.log
    exit /b 0
)

if %attempt% geq 3 (
    echo [%date% %time%] Gagal push setelah 3 percobaan. Cek sync_publish.log >> sync_publish.log
    exit /b 1
)

set /a attempt+=1
echo [%date% %time%] Push gagal, mencoba ulang percobaan %attempt%/3 dalam 15 detik... >> sync_publish.log
timeout /t 15 /nobreak >nul
goto push_loop