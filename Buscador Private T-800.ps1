# ==============================================================================
# BUSCADOR PRIVATE T-800 - SCRIPT MAESTRO DE DESPLIEGUE (1 CLIC)
# ==============================================================================
param(
    [ValidateSet("Directo", "Tor", "Hibrido")]
    [string]$Modo = "Directo"
)

$baseDir      = "C:\Users\astur\Desktop\searxng-nextgen"
$searxDir     = Join-Path $baseDir "searx"
$settingsFile = Join-Path $searxDir "settings.yml"
$prodFile     = Join-Path $baseDir "docker-compose.prod.yml"
$uiFile       = Join-Path $baseDir "docker-compose.ui.yml"
$utf8NoBom    = New-Object System.Text.UTF8Encoding($false)
$backupFile   = $null

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Wait-Url($url, $seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try {
            $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

Write-Host "[0/5] Modo de red seleccionado: $Modo. Comprobando entorno..." -ForegroundColor Cyan
if (-not (Test-Path $baseDir))  { Fail "No existe la carpeta $baseDir" }
if (-not (Test-Path $prodFile)) { Fail "Falta docker-compose.prod.yml en la raíz." }
if (-not (Test-Path $uiFile))   { Fail "Falta docker-compose.ui.yml." }
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0)        { Fail "Docker no responde. Inicia Docker Desktop." }
Set-Location $baseDir

Write-Host "[1/5] Escribiendo settings.yml optimizado..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $searxDir | Out-Null

if (Test-Path $settingsFile) {
    $backupFile = "$settingsFile.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item $settingsFile $backupFile -Force
}

$secret = $null
if (Test-Path $settingsFile) {
    $m = Select-String -Path $settingsFile -Pattern 'secret_key:\s*"([0-9a-fA-F]{32,})"' | Select-Object -First 1
    if ($m) { $secret = $m.Matches[0].Groups[1].Value }
}
if (-not $secret) { $secret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N") }

if ($Modo -eq "Tor") {
    $outgoing = @"
outgoing:
  request_timeout: 15.0
  max_request_timeout: 30.0
  retries: 1
  using_tor_proxy: true
  extra_proxy_timeout: 10
  proxies:
    all://:
      - socks5h://tor:9150
"@
} elseif ($Modo -eq "Hibrido") {
    $outgoing = @"
outgoing:
  request_timeout: 8.0
  max_request_timeout: 20.0
  retries: 1
"@
} else {
    $outgoing = @"
outgoing:
  request_timeout: 8.0
  max_request_timeout: 12.0
  retries: 1
"@
}

$engineNames = @("duckduckgo", "google", "bing", "brave", "qwant", "mojeek", "wikipedia", "github", "stackoverflow")
$torTolerant = @("wikipedia", "github", "stackoverflow", "mojeek")

$engineBlocks = foreach ($e in $engineNames) {
    $blk = "  - name: $e`n    disabled: false"
    if ($Modo -eq "Hibrido" -and ($torTolerant -contains $e)) {$blk += "`n    timeout: 15.0`n    proxies:`n      all://:`n        - socks5h://tor:9150"
    }
    $blk
}
$enginesYaml = "engines:`n" + ($engineBlocks -join "`n")

$head = @"
use_default_settings: true

server:
  secret_key: "$secret"
  limiter: false
  bind_address: "0.0.0.0"
  port: 8080
  debug: false

search:
  safe_search: 0
  ban_time_on_fail: 5
  max_ban_time_on_fail: 60
  formats:
    - html
    - json

"@

$settingsContent = $head +$outgoing + "`n`n" + $enginesYaml + "`n"
[System.IO.File]::WriteAllText($settingsFile, $settingsContent, $utf8NoBom)

Write-Host "[2/5] Levantando clúster en Docker (Core, Tor y UI)..." -ForegroundColor Cyan
docker compose -f $prodFile -f $uiFile up -d
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose falló al iniciar los servicios." }
docker compose -f $prodFile -f $uiFile restart core
if ($LASTEXITCODE -ne 0) { Fail "No se pudo reiniciar el contenedor core." }

Write-Host "[3/5] Verificando salud del núcleo SearXNG..." -ForegroundColor Cyan
if (-not (Wait-Url "http://localhost:8889/healthz" 90)) {
    Write-Host "El núcleo tardó en responder. Restaurando configuración previa..." -ForegroundColor Yellow
    if ($backupFile) { Copy-Item $backupFile $settingsFile -Force; docker compose -f $prodFile -f $uiFile restart core | Out-Null }
    Fail "Se abortó el arranque por un fallo de salud en el servicio."
}

if ($Modo -ne "Directo") {
    Write-Host "[4/5] Comprobando disponibilidad de la red Tor..." -ForegroundColor Cyan
    $torReady = $false
    $end = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $end) {
        $log = docker logs searvga_tor 2>&1 | Out-String
        if ($log -match "Bootstrapped 100%") { $torReady = $true; break }
        Start-Sleep -Seconds 3
    }
    if ($torReady) { Write-Host "  Red Tor conectada correctamente." -ForegroundColor Green }
    else { Write-Host "  AVISO: Tor sigue conectando en segundo plano." -ForegroundColor Yellow }
} else {
    Write-Host "[4/5] Modo Directo activo: Omitiendo espera de Tor." -ForegroundColor Cyan
}

Write-Host "[5/5] Realizando prueba de diagnóstico de búsqueda..." -ForegroundColor Cyan
try {
    $r     = Invoke-WebRequest "http://localhost:8890/api/search?q=PDF+Java&format=json" -UseBasicParsing -TimeoutSec 60
    $data  = $r.Content | ConvertFrom-Json
    $count = @($data.results).Count
    Write-Host "  ¡Prueba exitosa! Resultados obtenidos: $count" -ForegroundColor Green
} catch {
    Write-Host "  Aviso menor en la prueba de diagnóstico (la interfaz abrirá con normalidad)." -ForegroundColor Yellow
}

Write-Host "`n¡SISTEMA LISTO! Abriendo Buscador Private T-800 en http://localhost:8890..." -ForegroundColor Green
Start-Process "http://localhost:8890"