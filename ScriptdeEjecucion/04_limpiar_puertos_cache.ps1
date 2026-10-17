# Script 04: Limpieza de Puertos y Cache
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Liberando puertos y limpiando procesos..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

wsl --exec bash -c "fuser -k 8888/tcp 8889/tcp 2>/dev/null; echo '[OK] Puertos 8888 y 8889 liberados en WSL.'"

Write-Host "==========================================" -ForegroundColor Cyan