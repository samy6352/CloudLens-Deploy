<#
.SYNOPSIS
  CloudLens — one command, from nothing to a running app.

.DESCRIPTION
  Creates the Entra app registration, provisions the infrastructure, deploys the code, and
  waits until the app actually answers rather than assuming the upload worked.

  Safe to re-run: every step is idempotent. Re-running after a failure picks up where it
  stopped, and re-running after success updates in place.

  What you need before starting:
    * Azure CLI, signed in (az login)
    * Owner, or Contributor plus User Access Administrator, on the subscription
    * Permission to register an application in your tenant. Most tenants allow this; if
      yours does not, use -NoSso.

.EXAMPLE
  ./scripts/deploy.ps1 -Admin you@yourcompany.com

.EXAMPLE
  ./scripts/deploy.ps1 -Admin you@contoso.com -Location westeurope -AiLocation swedencentral
#>
[CmdletBinding()]
param(
  # Who may refresh data and run first-time setup. Defaults to whoever is signed in.
  [string] $Admin,

  [string] $ResourceGroup = "rg-cloudlens",
  [string] $Name = "cloudlens",

  # Region for the app. Must have App Service quota, which many subscriptions lack in many
  # regions — see the README if this fails.
  [string] $Location,

  # Region for the model. Separate from the app on purpose: App Service quota and model quota
  # are independent, and the regions with one often lack the other.
  [string] $AiLocation = "eastus",

  [ValidateSet("B1", "B2", "B3", "S1", "P0v3", "P1v3")]
  [string] $Sku = "B1",

  [ValidateSet("gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4.1-nano")]
  [string] $Model = "gpt-4.1-mini",

  [ValidateSet("GlobalStandard", "Standard", "DataZoneStandard")]
  [string] $ModelSku = "GlobalStandard",

  [string] $Subscription,
  [string] $Repo = "https://github.com/samy6352/CloudLens-Deploy",
  [string] $Branch = "main",

  # Skip the app registration and use a local admin password instead. For tenants that do not
  # let ordinary users register applications.
  [switch] $NoSso
)

$ErrorActionPreference = "Stop"

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
function Fail($m) { Write-Host "`nFailed: $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
  Fail "Azure CLI is not installed. See https://aka.ms/azcli"
}

Step "Checking your Azure sign-in"
$acct = az account show 2>$null | ConvertFrom-Json
if (-not $acct) { Fail "Not signed in. Run: az login" }
if ($Subscription) {
  az account set --subscription $Subscription
  $acct = az account show | ConvertFrom-Json
}

$tenantId = $acct.tenantId
$signedIn = $acct.user.name
Info "Subscription: $($acct.name)"
Info "Tenant:       $tenantId"
Info "Signed in as: $signedIn"

if (-not $Admin) { $Admin = $signedIn }

# A region has to be chosen before anything is created, and the honest default is one the
# subscription already uses rather than a guess that may have no quota behind it.
if (-not $Location) {
  $Location = (az group list --query "[0].location" -o tsv 2>$null)
  if (-not $Location) { $Location = "eastus" }
  Info "Region:       $Location (override with -Location)"
}

# ---------------------------------------------------------------- app registration
$clientId = ""
if (-not $NoSso) {
  Step "Registering the application in your tenant"
  $displayName = "CloudLens ($Name)"
  $existing = az ad app list --display-name $displayName --query "[0].appId" -o tsv 2>$null

  if ($existing -and $existing -ne "None") {
    $clientId = $existing
    Info "Reusing existing registration: $clientId"
  } else {
    # A public client using PKCE, with no secret. Tenants that cap secret lifetimes are
    # increasingly common, and a deployment needing a secret rotated every 90 days is a
    # deployment that breaks in 90 days.
    #
    # The redirect URI is added after deployment, not here: it contains the app's hostname,
    # which Bicep derives from a hash of the resource group id. Guessing it now and being
    # wrong produces a sign-in that fails at the last step naming a URI nobody typed.
    $clientId = az ad app create --display-name $displayName `
      --sign-in-audience AzureADMyOrg --query appId -o tsv 2>$null
    if (-not $clientId) {
      Fail @"
Could not register the application. Your tenant may restrict this to administrators.
Re-run with -NoSso to use a local password instead.
"@
    }
    Info "Created registration: $clientId"
    az ad sp create --id $clientId 2>$null | Out-Null
  }
}

# ---------------------------------------------------------------- infrastructure
Step "Provisioning Azure resources"
Info "This creates an App Service plan, a web app, an AI account with a model, and storage."
Info "It takes about five minutes."

$deployName = "cloudlens-$(Get-Date -Format 'yyyyMMddHHmmss')"
$template = Join-Path $PSScriptRoot "..\infra\main.bicep"

$out = az deployment sub create `
  --name $deployName `
  --location $Location `
  --template-file $template `
  --parameters `
    resourceGroupName=$ResourceGroup `
    location=$Location `
    aiLocation=$AiLocation `
    appName=$Name `
    sku=$Sku `
    modelName=$Model `
    modelSku=$ModelSku `
    adminEmails=$Admin `
    entraClientId=$clientId `
    entraTenantId=$tenantId `
    repositoryUrl=$Repo `
    branch=$Branch `
  -o none 2>&1

if ($LASTEXITCODE -ne 0) {
  $text = $out -join "`n"
  Write-Host $text
  # The three refusals that actually happen, each with a different fix. A raw ARM error names
  # none of them.
  if ($text -match "OverQuotaForSku|quota") {
    Fail @"
This subscription has no App Service quota in $Location. Credit-based subscriptions start
with none in most regions. Try -Location with another region, or request quota at
https://aka.ms/antquotahelp
"@
  } elseif ($text -match "not supported in this region") {
    Fail @"
The model $Model is not available as $ModelSku in $AiLocation.
Try -ModelSku Standard, or -AiLocation eastus.
"@
  } elseif ($text -match "AuthorizationFailed|does not have authorization") {
    Fail @"
You do not have rights to create role assignments on this subscription. CloudLens needs
Owner, or Contributor plus User Access Administrator, because the app reads cost with a
managed identity that has to be granted Cost Management Reader.
"@
  }
  Fail "Deployment failed. The Azure error is above."
}

function Get-Output($name) {
  az deployment sub show --name $deployName --query "properties.outputs.$name.value" -o tsv
}
$appUrl = Get-Output "appUrl"
$webApp = Get-Output "appName"
$redirect = Get-Output "redirectUri"
Info "App:      $webApp"
Info "URL:      $appUrl"

# ---------------------------------------------------------------- redirect URI
if ($clientId) {
  Step "Pointing the sign-in redirect at the deployed app"
  $objId = az ad app show --id $clientId --query id -o tsv
  $body = @{ spa = @{ redirectUris = @($redirect) } } | ConvertTo-Json -Compress -Depth 5
  $tmp = New-TemporaryFile
  Set-Content -Path $tmp -Value $body -NoNewline
  az rest --method PATCH `
    --uri "https://graph.microsoft.com/v1.0/applications/$objId" `
    --headers "Content-Type=application/json" `
    --body "@$tmp" 2>$null | Out-Null
  Remove-Item $tmp -Force
  if ($LASTEXITCODE -ne 0) {
    Info "Could not set the redirect URI automatically. Add $redirect as a"
    Info "single-page-application redirect URI on app registration $clientId."
  } else {
    Info "Redirect: $redirect"
  }
}

# ---------------------------------------------------------------- wait for health
Step "Waiting for the app to start"
Info "App Service builds the Python environment on first deploy, which takes a few minutes."

$ready = $false
foreach ($i in 1..60) {
  try {
    $code = (curl.exe -s -o NUL -w "%{http_code}" --max-time 20 "$appUrl/healthz")
    # 200 is healthy; 401 and 302 mean it is up and asking us to sign in, which is also up.
    if ($code -in @("200", "401", "302")) { $ready = $true; break }
  } catch { }
  Write-Host "." -NoNewline
  Start-Sleep -Seconds 15
}
Write-Host ""

if ($ready) {
  Info "The app is answering."
} else {
  Info "The app has not answered yet. First deployments can take up to ten minutes."
  Info "Check: az webapp log tail -g $ResourceGroup -n $webApp"
}

Write-Host "`nCloudLens is deployed." -ForegroundColor Green
Write-Host ""
Write-Host "  Open       $appUrl"
Write-Host "  Admin      $Admin"
Write-Host "  Group      $ResourceGroup"

if ($clientId) {
  Write-Host "  Sign-in    Microsoft Entra (app registration $clientId)"
  Write-Host ""
  Write-Host "Sign in with your Azure account. You will see exactly the subscriptions your"
  Write-Host "own Azure access allows, and nothing else."
} else {
  Write-Host "  Sign-in    Local account"
  Write-Host ""
  Write-Host "The first-run password is printed once in the application log:"
  Write-Host "  az webapp log tail -g $ResourceGroup -n $webApp"
}

Write-Host ""
Write-Host "The first time you open it, CloudLens will offer to set up your cost data."
Write-Host "That takes a few minutes and only happens once."
Write-Host ""
