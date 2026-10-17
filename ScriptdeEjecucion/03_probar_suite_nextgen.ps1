# Script 03: Pruebas de Integración y Tests Next-Gen
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Ejecutando Suite de Tests Next-Gen (WSL)..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

wsl --exec bash -c "cd /mnt/c/Users/astur/Desktop/searxng-nextgen && source ~/.venv/bin/activate && export PYTHONPATH=. && python -m unittest discover -s tests -p 'test_*.py'"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Pruebas finalizadas." -ForegroundColor Cyan