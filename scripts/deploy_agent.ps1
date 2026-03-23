param(
    [string]$AgentName = "my_agent1",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([string]$Path)
    if (!(Test-Path $Path)) {
        throw "Env file not found: $Path"
    }

    $map = @{}
    foreach ($raw in Get-Content $Path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) {
            continue
        }
        $key = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1)
        if ($value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $map[$key] = $value
    }
    return $map
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$envPath = Join-Path $repoRoot $EnvFile
$envMap = Read-DotEnv -Path $envPath

$required = @(
    "MODEL_ID",
    "AGENT_NAME",
    "AGENTCORE_RUNTIME_ID",
    "AWS_ACCOUNT_ID",
    "BEDROCK_REGION",
    "RUNTIME_LOG_GROUP",
    "RUNTIME_LOG_STREAM",
    "PRICE_BUCKET",
    "PRICE_KEY"
)

$missing = @()
foreach ($k in $required) {
    if (-not $envMap.ContainsKey($k) -or [string]::IsNullOrWhiteSpace($envMap[$k])) {
        $missing += $k
    }
}
if ($missing.Count -gt 0) {
    throw "Missing required env keys in ${EnvFile}: $($missing -join ', ')"
}

$envArgs = @()
foreach ($k in $required) {
    $envArgs += "--env"
    $envArgs += "$k=$($envMap[$k])"
}

Write-Host "Deploying AgentCore runtime '$AgentName' using env from ${EnvFile}..."

& agentcore deploy -a $AgentName --auto-update-on-conflict @envArgs
if ($LASTEXITCODE -ne 0) {
    throw "agentcore deploy failed with exit code $LASTEXITCODE"
}

Write-Host "Deployment completed successfully."
