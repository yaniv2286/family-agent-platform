# start_server.ps1 - Start Family Agent Platform for home network access
# Run from an elevated (Administrator) PowerShell prompt.

$ErrorActionPreference = 'Stop'

# Make the script's directory the working directory
Set-Location $PSScriptRoot

# Stop any existing server on port 8000
$existing = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -Property OwningProcess -Unique
if ($existing) {
    foreach ($proc in $existing) {
        Write-Host "Stopping process on port 8000 (PID: $($proc.OwningProcess))..."
        Stop-Process -Id $proc.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

# Activate a virtual environment if one exists
$venvPaths = @('\.venv\Scripts\Activate.ps1', '\venv\Scripts\Activate.ps1')
foreach ($venv in $venvPaths) {
    $fullPath = Join-Path $PSScriptRoot $venv
    if (Test-Path $fullPath) {
        & $fullPath
        Write-Host "Activated virtual environment: $fullPath"
        break
    }
}

# Install/update requirements so the environment is always ready
Write-Host "Checking/installing Python requirements..."
python -m pip install -r requirements.txt --quiet

# Add Windows Firewall rule for port 8000 if missing
$ruleName = 'Family Platform 8000'
$rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 | Out-Null
    Write-Host "Firewall rule '$ruleName' added."
} else {
    Write-Host "Firewall rule '$ruleName' already exists."
}

# Show the local IP address
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress
if ($ip) {
    Write-Host "Server will be available at: https://${ip}:8000"
} else {
    Write-Host "Could not detect local IP. Run 'ipconfig' to find it."
}

# Start the server
Write-Host "Starting Uvicorn..."
uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
