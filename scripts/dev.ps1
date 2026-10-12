# Windows (PowerShell) equivalent of the Makefile's local targets.
#   .\scripts\dev.ps1 setup    install everything (run inside the activated .venv)
#   .\scripts\dev.ps1 test     run all tests
#   .\scripts\dev.ps1 server   MCP server on http://localhost:8000/mcp
#   .\scripts\dev.ps1 sim      Alexa+ simulator on http://localhost:8080
# Settings come from .env, like the Makefile. Deploying (build_lambda.sh, deploy.sh) needs WSL or Git Bash.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("setup", "test", "server", "sim")]
    [string]$Task
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "python3" }

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    foreach ($raw in Get-Content $EnvFile) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $key, $value = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($key.Trim(), $value.Trim(), "Process")
    }
} else {
    Write-Warning "No .env found. Copy .env.example to .env first."
}

switch ($Task) {
    "setup" {
        & $Py -m pip install -e (Join-Path $Root ".." "cat-health-signals") -r (Join-Path $Root "requirements-dev.txt")
    }
    "test" {
        Push-Location $Root
        try { & $Py -m pytest } finally { Pop-Location }
    }
    "server" {
        Push-Location (Join-Path $Root "server")
        try { & $Py -m uvicorn cat_nanny.app:app --port 8000 --reload } finally { Pop-Location }
    }
    "sim" {
        Push-Location (Join-Path $Root "simulator")
        try { & $Py -m uvicorn sim.app:app --port 8080 --reload } finally { Pop-Location }
    }
}
exit $LASTEXITCODE
