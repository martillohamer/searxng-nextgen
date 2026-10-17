# Script 01: Iniciar Servidor SearXNG Next-Gen
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Iniciando Servidor Martillohamer Sear Edition..." -ForegroundColor Cyan
Write-Host " URL: http://localhost:8889" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Cyan

wsl --exec bash -c "cd /mnt/c/Users/astur/Desktop/searxng-nextgen && source ~/.venv/bin/activate && export PYTHONPATH=. && export SEARXNG_SECRET=mi_clave_dev_super_segura_12345 && export SEARXNG_PORT=8889 && python -m searx.webapp"