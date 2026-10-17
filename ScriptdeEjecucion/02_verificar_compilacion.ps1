# Script 02: Verificación de Compilación y Sintaxis
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Verificando compilacion del proyecto (WSL)..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

wsl --exec bash -c "cd /mnt/c/Users/astur/Desktop/searxng-nextgen && source ~/.venv/bin/activate && export PYTHONPATH=. && python -m py_compile searx/webapp.py"

if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] La compilacion de webapp.py es correcta." -ForegroundColor Green
} else {
    Write-Host "[ERROR] Se encontraron errores de compilacion." -ForegroundColor Red
}
Write-Host "==========================================" -ForegroundColor Cyan