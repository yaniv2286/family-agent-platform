# start_tunnel.ps1 - Expose the local Family Agent Platform to the Internet
#
# This is a convenience wrapper for Cloudflare Tunnels (cloudflared) and Ngrok.
# Start the local server first with start_server.ps1, then run this script.
#
# IMPORTANT: GitHub Pages CANNOT host this Python backend.
# GitHub Pages is a static-site host (HTML/CSS/JS only). This project is a
# FastAPI Python server with an OpenAI API, SQLite databases, and APScheduler
# jobs, so it must run on your own computer, a VPS, or a server host.
# Cloudflared/Ngrok simply create a public URL that forwards to the local
# server that is already running.
#
# HTTPS note: start_server.ps1 runs with a self-signed certificate. Cloudflared
# can use --no-tls-verify to accept it; Ngrok cannot forward to an HTTPS
# backend, so for Ngrok use the -Http switch to spin a plain HTTP Uvicorn
# instance on port 8001 first.

param(
    [Parameter()]
    [ValidateSet('cloudflared', 'ngrok')]
    [string]$Provider = 'cloudflared',

    [Parameter()]
    [string]$LocalPort = '8000',

    [Parameter()]
    [switch]$Http
)

$ErrorActionPreference = 'Stop'

function Test-CommandExists($Name) {
    return [bool](Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress
if (-not $ip) {
    $ip = '127.0.0.1'
}

if ($Http) {
    # Plain HTTP Uvicorn for tunnel compatibility (e.g. Ngrok).
    $localUrl = "http://${ip}:${LocalPort}"
    Write-Host "Starting plain-HTTP Uvicorn on $localUrl for the tunnel..."
    Start-Process -NoNewWindow -FilePath 'python' -ArgumentList "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$LocalPort"
    Start-Sleep -Seconds 2
} else {
    # Assume start_server.ps1 already started HTTPS.
    $localUrl = "https://${ip}:${LocalPort}"
}

if ($Provider -eq 'cloudflared') {
    if (-not (Test-CommandExists 'cloudflared')) {
        Write-Error @"
cloudflared is not installed or not on PATH.
1. Download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
2. Log in once with: cloudflared tunnel login
3. Run this script again.
"@
    }
    Write-Host "Starting Cloudflare Tunnel pointing to $localUrl ..."
    Write-Host "When the URL appears below, share that link (valid for this session)."
    & cloudflared tunnel --no-tls-verify --url $localUrl
}
elseif ($Provider -eq 'ngrok') {
    if (-not (Test-CommandExists 'ngrok')) {
        Write-Error @"
Ngrok is not installed or not on PATH.
1. Sign up at https://ngrok.com, install the agent, and run: ngrok config add-authtoken <YOUR_TOKEN>
2. Run this script again with -Http if your server is using HTTPS, or run: ngrok http 8000
"@
    }
    if (-not $Http) {
        Write-Warning "Ngrok cannot forward to an HTTPS backend. Use -Http to start an HTTP Uvicorn instance, or run: ngrok http ${LocalPort} against an HTTP server."
    }
    Write-Host "Starting Ngrok tunnel on port $LocalPort ..."
    Write-Host "When the URL appears in the Ngrok console, share that link."
    & ngrok http $LocalPort
}
