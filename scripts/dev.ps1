# Windows (PowerShell) equivalent of the Makefile.
#   .\scripts\dev.ps1 setup    install everything (run inside the activated .venv)
#   .\scripts\dev.ps1 test     run all tests
#   .\scripts\dev.ps1 server   MCP server on http://127.0.0.1:8000/mcp
#   .\scripts\dev.ps1 sim      Alexa+ simulator on http://127.0.0.1:8080
#   .\scripts\dev.ps1 build    package the Lambda into .build\lambda
#   .\scripts\dev.ps1 deploy   build + sam deploy (needs AWS_REGION, BUDGET_EMAIL, MCP_AUTH_TOKEN in .env)
#   .\scripts\dev.ps1 seed     load the demo household into the deployed table (needs TABLE_NAME in .env)
#   .\scripts\dev.ps1 logs     tail the Lambda's logs
# Settings come from .env, like the Makefile. AWS credentials come from `aws configure`, never from .env.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("setup", "test", "server", "sim", "build", "deploy", "seed", "logs")]
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

function Require($name) {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Set $name in .env" }
}

$Stack = if ($env:STACK_NAME) { $env:STACK_NAME } else { "cat-nanny" }

switch ($Task) {
    "setup" {
        & $Py -m pip install -e (Join-Path (Join-Path $Root "..") "cat-health-signals") -r (Join-Path $Root "requirements-dev.txt")
    }
    "test" {
        Push-Location $Root
        try { & $Py -m pytest } finally { Pop-Location }
    }
    "server" {
        Push-Location (Join-Path $Root "server")
        try { & $Py -m uvicorn cat_nanny.app:app --host 127.0.0.1 --port 8000 --reload } finally { Pop-Location }
    }
    "sim" {
        Push-Location (Join-Path $Root "simulator")
        try { & $Py -m uvicorn sim.app:app --host 127.0.0.1 --port 8080 --reload } finally { Pop-Location }
    }
    "build" {
        & $Py (Join-Path $PSScriptRoot "build_lambda.py")
    }
    "deploy" {
        Require "AWS_REGION"; Require "BUDGET_EMAIL"; Require "MCP_AUTH_TOKEN"
        & $Py (Join-Path $PSScriptRoot "build_lambda.py")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $Household = if ($env:HOUSEHOLD_ID) { $env:HOUSEHOLD_ID } else { "demo" }
        $Tz = if ($env:HOUSEHOLD_TZ) { $env:HOUSEHOLD_TZ } else { "America/Los_Angeles" }
        $Origins = if ($env:ALLOWED_ORIGINS) { $env:ALLOWED_ORIGINS } else { "http://127.0.0.1:8080,http://localhost:8080" }
        Push-Location $Root
        try {
            sam deploy --template-file (Join-Path "infra" "template.yaml") --stack-name $Stack --region $env:AWS_REGION `
                --resolve-s3 --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset `
                --parameter-overrides "BudgetEmail=$env:BUDGET_EMAIL" "McpAuthToken=$env:MCP_AUTH_TOKEN" `
                "HouseholdId=$Household" "HouseholdTz=$Tz" "AllowedOrigins=$Origins"
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            aws cloudformation describe-stacks --stack-name $Stack --region $env:AWS_REGION `
                --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
        } finally { Pop-Location }
    }
    "seed" {
        Require "TABLE_NAME"
        & $Py (Join-Path $PSScriptRoot "seed.py")
    }
    "logs" {
        aws logs tail "/aws/lambda/$Stack-mcp" --follow --region $env:AWS_REGION
    }
}
exit $LASTEXITCODE
