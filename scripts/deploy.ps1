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

  # Deployment passphrase. Also read from $env:CLOUDLENS_PASSPHRASE; prompted for if neither
  # is set. Typed as SecureString so it does not land in the PowerShell history file.
  [System.Security.SecureString] $Passphrase,

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
  [switch] $NoSso,

  # Run the checks and stop, without creating anything. For finding out whether a subscription
  # can host CloudLens before committing to it — which is a fair question to want answered
  # separately, particularly when the subscription belongs to somebody else.
  [switch] $CheckOnly
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

$subId = $acct.id
$tenantId = $acct.tenantId
$signedIn = $acct.user.name
Info "Subscription: $($acct.name)"
Info "Tenant:       $tenantId"
Info "Signed in as: $signedIn"

if (-not $Admin) { $Admin = $signedIn }

# Asked for rather than defaulted. Prompting beats failing three minutes into a deployment
# with an ARM error nobody can read.
if (-not $Passphrase) {
  if ($env:CLOUDLENS_PASSPHRASE) {
    $Passphrase = ConvertTo-SecureString $env:CLOUDLENS_PASSPHRASE -AsPlainText -Force
  } else {
    $Passphrase = Read-Host "    Deployment passphrase" -AsSecureString
  }
}
$plainPassphrase = [System.Net.NetworkCredential]::new("", $Passphrase).Password
if (-not $plainPassphrase) {
  Fail "A deployment passphrase is required. Ask whoever gave you this repository, or set
CLOUDLENS_PASSPHRASE."
}

# A region has to be chosen before anything is created, and the honest default is one the
# subscription already uses rather than a guess that may have no quota behind it.
if (-not $Location) {
  $Location = (az group list --query "[0].location" -o tsv 2>$null)
  if (-not $Location) { $Location = "eastus" }
  Info "Region:       $Location (override with -Location)"
}

# ---------------------------------------------------------------- preflight
#
# Everything that can refuse this deployment, asked before anything is created.
#
# This matters more than it looks. The template creates resources first and role assignments
# second, because a role assignment needs the managed identity's object id and that identity
# does not exist until the web app does. So a subscription that cannot create role assignments
# builds the whole estate and *then* fails, leaving a running app, a real bill and no data —
# which reads as a successful deployment to everybody except the person looking for their
# costs.
#
# The checks are also reported together rather than one per run. They are independent, and
# discovering them one at a time is four deployments and half an hour to learn what a single
# pass can say at once.

Step "Checking this subscription can host CloudLens"

$blockers = [System.Collections.Generic.List[string]]::new()
$notes = [System.Collections.Generic.List[string]]::new()

# Azure matches RBAC action patterns case-insensitively, and `*` stands for any run of
# characters including `/` — so `Microsoft.Authorization/*/Write` covers roleAssignments/write.
function Test-ActionMatch([string] $pattern, [string] $action) {
  return $action -imatch ('^' + [Regex]::Escape($pattern).Replace('\*', '.*') + '$')
}

# The authoritative answer to "may I create a role assignment here", which is what the
# Owner-versus-Contributor distinction actually decides. Contributor carries `*` in its
# actions and excludes `Microsoft.Authorization/*/Write` in its notActions, so asking for the
# effective permissions is exact — and free, where the alternative is creating one to find out.
$roleWrite = 'Microsoft.Authorization/roleAssignments/write'
$canAssignRoles = $false
$permJson = az rest --method GET `
  --url "https://management.azure.com/subscriptions/$subId/providers/Microsoft.Authorization/permissions?api-version=2022-04-01" `
  -o json 2>$null
if ($permJson) {
  foreach ($p in ($permJson | ConvertFrom-Json).value) {
    $granted = @($p.actions) | Where-Object { Test-ActionMatch $_ $roleWrite }
    if (-not $granted) { continue }
    $withheld = @($p.notActions) | Where-Object { Test-ActionMatch $_ $roleWrite }
    if (-not $withheld) { $canAssignRoles = $true; break }
  }
} else {
  $notes.Add("Could not read your permissions on this subscription; skipping the rights check.")
}

if (-not $canAssignRoles) {
  $blockers.Add(@"
You cannot create role assignments on this subscription.
    CloudLens grants its managed identity Reader and Cost Management Reader. Without them the
    app deploys perfectly and reports an empty estate, so this stops now rather than after
    building one.
    Fix: ask for Owner, or Contributor plus User Access Administrator.
"@)
}

# Providers are registered rather than reported. A missing one is a real failure, but it is
# also the one obstacle here that can simply be removed without asking anybody for anything.
foreach ($ns in @('Microsoft.Web', 'Microsoft.CognitiveServices', 'Microsoft.Storage')) {
  $state = az provider show -n $ns --query registrationState -o tsv 2>$null
  if ($state -and $state -ne 'Registered') {
    Info "Registering resource provider $ns"
    az provider register -n $ns 2>$null | Out-Null
  }
}

# App Service quota. This lists what the subscription may create, not merely what the region
# offers, which is the distinction that catches credit-based subscriptions — they start with
# zero compute in most regions.
$wanted = ($Location -replace '\s', '').ToLowerInvariant()
$skuRegions = az appservice list-locations --sku $Sku --query "[].name" -o tsv 2>$null
if ($skuRegions) {
  $match = @($skuRegions) | Where-Object { ($_ -replace '\s', '').ToLowerInvariant() -eq $wanted }
  if (-not $match) {
    $blockers.Add(@"
This subscription has no $Sku App Service quota in $Location.
    Credit-based subscriptions start with none in most regions.
    Fix: -Location with another region, or request quota at https://aka.ms/antquotahelp
"@)
  }
}

# Model availability, which is quota of a different kind and independent of the one above —
# the regions with App Service capacity frequently lack model capacity and the reverse.
$modelSkus = az cognitiveservices model list -l $AiLocation `
  --query "[?model.name=='$Model'].model.skus[].name" -o tsv 2>$null
if ($modelSkus) {
  if (@($modelSkus) -notcontains $ModelSku) {
    $offered = (@($modelSkus) | Sort-Object -Unique) -join ', '
    $blockers.Add(@"
$Model is not offered as $ModelSku in $AiLocation.
    That region offers: $offered
    Fix: -ModelSku with one of those, or -AiLocation with another region.
"@)
  }
} else {
  $notes.Add("$Model does not appear to be available in $AiLocation at all; try -AiLocation eastus.")
}

# An existing group is not an error — re-running over the top is the documented way to update,
# and to add SSO to a deployment that began at the portal button. Saying so removes the doubt.
if ((az group exists -n $ResourceGroup -o tsv 2>$null) -eq 'true') {
  # Counted here rather than with a JMESPath `length(@)`, because the Azure CLI on Windows is a
  # batch file and cmd treats the parentheses as syntax of its own.
  $count = @(az resource list -g $ResourceGroup --query "[].id" -o tsv 2>$null).Count
  $notes.Add("Resource group $ResourceGroup already exists with $count resource(s); this run updates it in place.")
}

foreach ($n in $notes) { Info $n }

if ($blockers.Count -gt 0) {
  Write-Host ""
  Write-Host "This subscription cannot host CloudLens yet:" -ForegroundColor Red
  foreach ($b in $blockers) { Write-Host "  - $b" }
  Fail "Nothing was created."
}
Info "Everything CloudLens needs is available."

if ($CheckOnly) {
  Write-Host ""
  Write-Host "Checks passed. Nothing was created." -ForegroundColor Green
  Write-Host "Re-run without -CheckOnly to deploy."
  exit 0
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
      # Not fatal. A tenant that reserves app registration for administrators is common, and
      # guests can essentially never do it — but that only decides how people sign in, not
      # whether CloudLens can run. Falling back costs a re-run and gets the same estate.
      Info "Your tenant would not let you register an application."
      Info "Continuing with a local password instead; the app is otherwise identical."
      Info "To move to Azure sign-in later, re-run this script once you have the rights."
      $NoSso = $true
      $clientId = ""
    } else {
      Info "Created registration: $clientId"
      az ad sp create --id $clientId 2>$null | Out-Null
    }
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
    deploymentPassphrase=$plainPassphrase `
    repositoryUrl='' `
  -o none 2>&1

if ($LASTEXITCODE -ne 0) {
  $text = $out -join "`n"
  Write-Host $text
  # The refusals that actually happen, each with a different fix. A raw ARM error names
  # none of them.
  if ($text -match "DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT") {
    Fail "That deployment passphrase is not correct. Ask whoever gave you this repository."
  } elseif ($text -match "OverQuotaForSku|quota") {
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

# ---------------------------------------------------------------- code
# A zip built here rather than a repository App Service clones for itself. App Service clones
# anonymously, so a private repository would produce a deployment that succeeds and an app that
# serves nothing — and this way the code deployed is demonstrably the code in front of you,
# not whatever happens to be on a branch somewhere.
Step "Deploying the application"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$zip = Join-Path ([System.IO.Path]::GetTempPath()) "cloudlens-$(Get-Random).zip"

Push-Location $root
try {
  git rev-parse --git-dir 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    # Tracked files only, so .env, the local warehouse and anything else gitignored cannot be
    # swept into the package.
    git archive --format=zip -o $zip HEAD
  } else {
    # A download of the source rather than a clone. Exclude the same things by hand.
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "cl-stage-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Get-ChildItem -Path $root -Force | Where-Object {
      $_.Name -notin @('.git', 'data', '.env', '__pycache__', '.venv')
    } | Copy-Item -Destination $tmp -Recurse -Force
    Compress-Archive -Path "$tmp\*" -DestinationPath $zip -Force
    Remove-Item $tmp -Recurse -Force
  }
} finally {
  Pop-Location
}
Info "Package: $([math]::Round((Get-Item $zip).Length / 1MB, 2)) MB"

az webapp deploy --resource-group $ResourceGroup --name $webApp `
  --src-path $zip --type zip --async false -o none
if ($LASTEXITCODE -ne 0) {
  Fail @"
Could not deploy the application code. The infrastructure is in place, so re-running this
script will retry just this step.
"@
}
Remove-Item $zip -Force -ErrorAction SilentlyContinue

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
