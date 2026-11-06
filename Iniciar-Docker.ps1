# Prepara o Docker, sobe o bot e abre a interface.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$url = "http://127.0.0.1:8766"
$desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"

function Fail($text) {
    Write-Host ""
    Write-Host "ERRO: $text" -ForegroundColor Red
    Read-Host "Pressione Enter para fechar"
    exit 1
}
function DockerRunning {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}
function New-Password {
    $chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
    return -join (1..32 | ForEach-Object { $chars | Get-Random })
}

# 1. Docker instalado
if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and -not (Test-Path $desktop)) {
    Write-Host "O Docker Desktop não está instalado."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "Instale o Docker Desktop em https://www.docker.com/products/docker-desktop e rode de novo."
    }
    $answer = Read-Host "Instalar agora com o winget? Isso aceita os termos do Docker e do winget e pode pedir permissão de administrador (s/n)"
    if ($answer -notmatch "^[sS]") { Fail "Instalação cancelada." }
    winget install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "Não foi possível instalar o Docker Desktop." }
    Write-Host ""
    Write-Host "Docker instalado. Reinicie o Windows, abra o Docker Desktop uma vez, aceite os termos e rode este arquivo de novo." -ForegroundColor Yellow
    Read-Host "Pressione Enter para fechar"
    exit 0
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $env:Path += ";" + (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin")
}

# 2. Docker aberto
if (-not (DockerRunning)) {
    if (-not (Test-Path $desktop)) { Fail "Abra o Docker Desktop e rode este arquivo de novo." }
    Write-Host "Abrindo o Docker Desktop..."
    Start-Process -FilePath $desktop
    $ready = $false
    foreach ($second in 1..60) {
        Start-Sleep -Seconds 3
        if (DockerRunning) { $ready = $true; break }
    }
    if (-not $ready) { Fail "O Docker não respondeu em 3 minutos. Veja se ele pede alguma confirmação." }
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "O Docker Compose não está disponível. Atualize o Docker Desktop." }

# 3. Arquivo .env
$volumeExists = $false
docker volume inspect crypto-bot_postgres_data *> $null
if ($LASTEXITCODE -eq 0) { $volumeExists = $true }
if (-not (Test-Path ".env")) {
    if (-not (Test-Path ".env.example")) { Fail "Arquivo .env.example não encontrado." }
    $content = (Get-Content ".env.example" -Raw) -replace "troque_esta_senha", (New-Password)
    Set-Content -Path ".env" -Value $content -NoNewline
    Write-Host "Arquivo .env criado. Conecte as chaves da testnet na interface."
} elseif (-not (Select-String -Path ".env" -Pattern "^POSTGRES_PASSWORD=" -Quiet)) {
    if ($volumeExists) {
        Write-Host "O banco já existe com a senha padrão. A senha não foi alterada." -ForegroundColor Yellow
    } else {
        Add-Content -Path ".env" -Value "`nPOSTGRES_PASSWORD=$(New-Password)"
    }
}

# 4. Contêineres
Write-Host "Preparando o bot..."
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    docker compose ps -a
    Fail "Não foi possível subir os contêineres. Se a porta 8766 estiver em uso, feche o programa que a ocupa."
}

# 5. Interface pronta
Write-Host "Aguardando a interface..."
$healthy = $false
foreach ($second in 1..40) {
    try {
        $response = Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}
if (-not $healthy) {
    docker compose ps -a
    docker compose logs --tail=40 bot
    Fail "A interface não respondeu. Veja as mensagens acima."
}
Write-Host "Tudo pronto: $url" -ForegroundColor Green
Start-Process $url
