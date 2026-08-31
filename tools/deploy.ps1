<#
.SYNOPSIS
  Deploy CloudLens into your own Azure subscription.

.DESCRIPTION
  Idempotent: safe to re-run. Creates the resource group, plan and web app if they are missing,
  grants the app's managed identity the read access it needs, points the warehouse at persistent
  storage, deploys the code, and verifies the result.

  Three things this script will not let you get wrong, because each one produces an app that
  looks deployed and is not:

    * One worker, one instance. Sessions and the header snapshot live in process memory, so a
      second worker sends a signed-in person to a process that has never heard of them.

    * COST_DB outside wwwroot. A zip deploy replaces /home/site/wwwroot and data/ is gitignored,
      so a warehouse at the default path is deleted by the next deploy and the app returns empty.

    * The managed identity's own access is the ceiling. A subscription it has no Reader on is
      invisible to every user, however much access they personally hold.

  It also grants the identity a data-plane role on the Foundry account behind -ProjectEndpoint.
  Reader on the subscription does not cover that, so without it the dashboard loads and every
  question comes back 401.

.EXAMPLE
  ./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -Location centralus `
    -ProjectEndpoint https://my-foundry.services.ai.azure.com/api/projects/my-project

.EXAMPLE
  # With Entra sign-in, so people use their Azure credentials rather than a local password.
  ./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me `
    -ProjectEndpoint https://... -EntraClientId <app-id> -EntraTenantId <tenant-id> `
    -Admins you@yourdomain.com
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $ResourceGroup,
  [Parameter(Mandatory)] [string] $AppName,
  [string] $Location = "centralus",

  # F1 is free but has no Always On, so the app cold-starts and a long ingest can be killed
  # mid-run. B1 is about 13 USD a month and is what you want if you care about scheduled loads.
  [ValidateSet("F1", "B1", "B2", "S1", "P0v3")] [string] $Sku = "B1",

  # Required: the app refuses to start without it. See -NoAgent below if you only want the
  # dashboard and would rather not pay for a model.
  [string] $ProjectEndpoint,
  [string] $ModelDeployment = "gpt-4.1",

  # Subscriptions the app may read. Defaults to the one you are currently signed in to.
  [string[]] $Subscriptions,

  # Entra sign-in. Leave these unset to use a local username and password instead.
  [string] $EntraClientId,
  [string] $EntraTenantId,
  [string] $EntraClientSecret,
  [string[]] $Admins,

  # Let anyone with an Azure account sign in, from any Entra directory, and see whatever their own
  # Azure RBAC allows -- across every tenant they belong to. Their account does not need to exist
  # in the tenant this app is registered in.
  #
  # This uses the 'common' authority, which admits personal Microsoft accounts as well as work
  # ones. That is not a loosening for its own sake: a Visual Studio or pay-as-you-go subscription
  # is very often owned by an MSA that is a guest in its own directory, and Entra issues such an
  # account a normal organisational ARM token. Excluding them can lock out the only person with
  # access to the subscription.
  #
  # The registration needs "Accounts in any organizational directory and personal Microsoft
  # accounts", and this implies -DelegatedArm: resolving access as the app only ever works inside
  # the app's own directory, so without a delegated token every external user would sign in and
  # see nothing.
  [switch] $MultiTenant,

  # Names a directory whose sign-in page is offered as a second option, for people whose Azure
  # access comes through a personal Microsoft account. Azure Resource Manager is an Entra-only
  # resource, so the multi-tenant endpoints refuse a personal account for an ARM scope -- but a
  # tenant authority federates them to login.live.com and issues an ordinary token. Without this
  # such a person is told to "use your work or school account" and has no way in, which matters
  # because those accounts commonly own exactly the subscriptions this app reports on.
  [string] $HomeTenant,

  # As -MultiTenant, but work and school accounts only. Use where personal accounts should not
  # be able to sign in even if they hold Azure access.
  [switch] $WorkAccountsOnly,

  # Use the signed-in person's own ARM token rather than resolving their role assignments as
  # the app. Stronger -- Azure refuses rather than the app filtering -- but it needs admin
  # consent for Azure Service Management user_impersonation. In your own tenant you are the
  # admin, so this is usually the right choice for a personal deployment.
  [switch] $DelegatedArm,

  [switch] $SkipRoleAssignment
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# az is a native command, so a failure sets $LASTEXITCODE and nothing else: ErrorActionPreference
# does not apply. Calling it bare gives a script that prints ERROR, keeps going, and reports a
# deployment that never happened. Every call goes through one of these two.
#
# The wrapper is named Az on purpose: PowerShell resolves names case-insensitively and prefers
# functions to executables, so a bare `az` anywhere below lands here and gets checked. That also
# means the wrapper must not say `az` itself, or it calls itself forever -- hence the resolved path.
$AzExe = (Get-Command az -CommandType Application -ErrorAction SilentlyContinue |
          Select-Object -First 1).Source
if (-not $AzExe) { throw "Azure CLI not found on PATH. Install it from https://aka.ms/azcli" }

function Az {
  $errFile = [IO.Path]::GetTempFileName()
  try {
    $out = & $AzExe @args 2>$errFile
    if ($LASTEXITCODE -ne 0) {
      throw "az $($args -join ' ') failed (exit $LASTEXITCODE):`n$(Get-Content $errFile -Raw)"
    }
    $out
  }
  finally { Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
}

# For calls that are allowed to fail: a role that already exists, a resource being probed for.
function AzTry {
  $errFile = [IO.Path]::GetTempFileName()
  try {
    $out = & $AzExe @args 2>$errFile
    if ($LASTEXITCODE -eq 0) { $out }
  }
  finally { Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
}

$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo
try {
  Step "Checking prerequisites"
  $account = Az account show --only-show-errors | ConvertFrom-Json
  if (-not $account) { throw "Not signed in. Run 'az login' first." }
  Ok "signed in as $($account.user.name)"
  Ok "subscription  $($account.name) ($($account.id))"
  Ok "tenant        $($account.tenantId)"

  if (-not $Subscriptions) { $Subscriptions = @($account.id) }

  if (-not $ProjectEndpoint) {
    throw @"
-ProjectEndpoint is required: the app calls RuntimeError on startup without it.

Create one in Azure AI Foundry (https://ai.azure.com): a project, then a model deployment named
'$ModelDeployment'. The endpoint looks like
  https://<resource>.services.ai.azure.com/api/projects/<project>

If you only want the cost dashboard and would rather not deploy a model at all, say so and the
agent can be made optional -- it is a single startup check.
"@
  }

  Step "Resource group"
  # A group's location is metadata only -- resources inside it can live anywhere -- so an existing
  # group in another region is fine. Recreating it with a new -Location just errors.
  $rgLocation = AzTry group show -n $ResourceGroup --query location -o tsv --only-show-errors
  if ($rgLocation) {
    Ok "$ResourceGroup already exists in $rgLocation"
    if ($rgLocation -ne $Location) { Ok "resources will be created in $Location" }
  } else {
    Az group create -n $ResourceGroup -l $Location --only-show-errors | Out-Null
    Ok "$ResourceGroup in $Location"
  }

  Step "App Service plan and web app"
  $planName = "asp-$AppName"
  try {
    Az appservice plan create -g $ResourceGroup -n $planName --is-linux --sku $Sku -l $Location `
      --only-show-errors | Out-Null
  }
  catch {
    if ("$_" -match "quota") {
      throw @"
$Location has no App Service compute quota on this subscription, so the plan cannot be created.
This is common on Visual Studio and other credit-based subscriptions, which start at zero VMs
in most regions.

Find a region that will take it:
  az appservice list-locations --sku $Sku -o table

Then re-run with -Location <region>. Raising the quota instead is a support request:
  https://aka.ms/appservicequota

Original error:
$_
"@
    }
    throw
  }
  Ok "plan $planName ($Sku)"

  $exists = AzTry webapp show -g $ResourceGroup -n $AppName --only-show-errors
  if (-not $exists) {
    Az webapp create -g $ResourceGroup -p $planName -n $AppName --runtime "PYTHON:3.13" `
      --only-show-errors | Out-Null
    Ok "created $AppName"
  } else {
    Ok "$AppName already exists"
  }

  # One worker, deliberately. Sessions are in process memory.
  $startup = "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"
  $alwaysOn = if ($Sku -eq "F1") { "false" } else { "true" }
  Az webapp config set -g $ResourceGroup -n $AppName --always-on $alwaysOn `
    --startup-file $startup --number-of-workers 1 --only-show-errors | Out-Null
  Ok "single worker, always-on=$alwaysOn"  if ($Sku -eq "F1") {
    Warn "F1 has no Always On: the app cold-starts and a long ingest may be killed part-way."
  }

  Step "Managed identity"
  $mi = Az webapp identity assign -g $ResourceGroup -n $AppName `
    --query principalId -o tsv --only-show-errors
  if (-not $mi) { throw "No principal id came back for $AppName." }
  Ok "principal $mi"

  if ($SkipRoleAssignment) {
    Warn "skipping role assignment as asked; the app will see nothing until it is granted."
  } else {
    Step "Granting read access (this is the ceiling for every user)"
    foreach ($sub in $Subscriptions) {
      foreach ($role in @("Reader", "Cost Management Reader")) {
        AzTry role assignment create --assignee-object-id $mi `
          --assignee-principal-type ServicePrincipal --role $role `
          --scope "/subscriptions/$sub" --only-show-errors | Out-Null
      }
      # Verify rather than trust the exit code: a lock or a missing Owner right fails quietly.
      $have = AzTry role assignment list --assignee $mi --scope "/subscriptions/$sub" `
        --query "[].roleDefinitionName" -o tsv --only-show-errors
      if ($have -match "Reader") { Ok "$sub -> $($have -join ', ')" }
      else { Warn "$sub -> NO ROLE. You need Owner or User Access Administrator on it." }
    }

    # The Foundry account behind -ProjectEndpoint is a separate data plane. Subscription Reader
    # does not reach it, so skipping this gives an app that loads and 401s on every question.
    $foundry = ([Uri]$ProjectEndpoint).Host.Split('.')[0]
    $foundryId = AzTry cognitiveservices account list `
      --query "[?name=='$foundry'].id | [0]" -o tsv --only-show-errors
    if (-not $foundryId) {
      Warn "Could not find Foundry account '$foundry' in this subscription."
      Warn "If it lives elsewhere, grant '$mi' the Azure AI User role on it yourself."
    } else {
      foreach ($role in @("Azure AI User", "Cognitive Services User")) {
        AzTry role assignment create --assignee-object-id $mi `
          --assignee-principal-type ServicePrincipal --role $role `
          --scope $foundryId --only-show-errors | Out-Null
      }
      $have = AzTry role assignment list --assignee $mi --scope $foundryId `
        --query "[].roleDefinitionName" -o tsv --only-show-errors
      if ($have) { Ok "$foundry -> $($have -join ', ')" }
      else { Warn "$foundry -> NO ROLE. The agent will answer 401 until it has one." }
    }
  }

  Step "Application settings"
  $settings = @(
    "COST_DB=/home/data/costs.duckdb"      # persistent Azure Files; survives a zip deploy
    "AUTH_DATA_DIR=/home/data"             # same reason: the account file must outlive a deploy
    "PROJECT_ENDPOINT=$ProjectEndpoint"
    "MODEL_DEPLOYMENT_NAME=$ModelDeployment"
    "AUTH_COOKIE_SECURE=true"
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
    # Kudu kills a build command that has produced no output for SCM_COMMAND_IDLE_TIMEOUT
    # seconds, and the default is 180. Extracting the Python SDK and resolving wheels on a B1
    # is silent for longer than that, so the build is killed mid-extraction and reported as a
    # failure with a log that simply stops. Nothing is wrong with the dependencies.
    "SCM_COMMAND_IDLE_TIMEOUT=1800"
    "SCM_LOGSTREAM_TIMEOUT=1800"
    # The default 230s is not enough: this app opens the warehouse and builds its first
    # overview before serving, and a cold B1 in a busy region needs longer. Falling short
    # gets the container killed mid-start and looks exactly like a crash.
    "WEBSITES_CONTAINER_START_TIME_LIMIT=600"
  )

  # Set once and keep: regenerating it on every deploy signs everyone out.
  $existingSecret = AzTry webapp config appsettings list -g $ResourceGroup -n $AppName `
    --query "[?name=='AUTH_SECRET'].value" -o tsv --only-show-errors
  if (-not $existingSecret) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $settings += "AUTH_SECRET=$([Convert]::ToBase64String($bytes))"
    Ok "generated AUTH_SECRET"
  } else {
    Ok "kept existing AUTH_SECRET (sessions survive this deploy)"
  }

  if ($EntraClientId -and ($EntraTenantId -or $MultiTenant -or $WorkAccountsOnly)) {
    # 'common' admits work, school and personal Microsoft accounts; 'organizations' excludes the
    # last. Personal accounts are included by default because they frequently own exactly the
    # kind of subscription this app is pointed at.
    $tenantSetting = if ($WorkAccountsOnly) { "organizations" }
                     elseif ($MultiTenant)  { "common" }
                     else                   { $EntraTenantId }
    $anyDirectory = $MultiTenant -or $WorkAccountsOnly
    $settings += "AUTH_CLIENT_ID=$EntraClientId"
    $settings += "AUTH_TENANT_ID=$tenantSetting"
    $settings += "AUTH_REDIRECT_URI=https://$AppName.azurewebsites.net/auth/callback"
    if ($EntraClientSecret) { $settings += "AUTH_CLIENT_SECRET=$EntraClientSecret" }
    if ($DelegatedArm -or $anyDirectory) { $settings += "AUTH_DELEGATED_ARM=true" }
    if ($Admins)            { $settings += "AUTH_ADMINS=$($Admins -join ',')" }
    if ($HomeTenant)        { $settings += "AUTH_HOME_TENANT=$HomeTenant" }

    if ($anyDirectory) {
      $wanted = if ($WorkAccountsOnly) { "AzureADMultipleOrgs" }
                else { "AzureADandPersonalMicrosoftAccount" }
      Ok "Entra sign-in configured for $tenantSetting"
      $audience = AzTry ad app show --id $EntraClientId --query signInAudience -o tsv
      if ($audience -eq $wanted) {
        Ok "app registration audience is $audience"
      } else {
        Warn "The app registration says signInAudience=$audience, which will turn away the"
        Warn "accounts this deployment is meant to admit. Fix it with:"
        Warn "  az ad app update --id $EntraClientId --sign-in-audience $wanted"
        if ($wanted -eq "AzureADandPersonalMicrosoftAccount") {
          Warn "That change needs v2 access tokens first, if it is refused:"
          Warn "  az rest --method PATCH --url https://graph.microsoft.com/v1.0/applications/<objectId> \"
          Warn "    --headers Content-Type=application/json --body '{\"api\":{\"requestedAccessTokenVersion\":2}}'"
        }
      }
    } else {
      Ok "Entra sign-in configured"
    }
    Warn "Add this exact redirect URI to the app registration (type: Web):"
    Warn "  https://$AppName.azurewebsites.net/auth/callback"
  } else {
    Warn "No Entra settings given, so the app falls back to a local username and password."
    Warn "The generated password is written to the log on first start:"
    Warn "  az webapp log tail -g $ResourceGroup -n $AppName"
  }

  Az webapp config appsettings set -g $ResourceGroup -n $AppName --settings $settings `
    --only-show-errors | Out-Null
  Ok "$($settings.Count) settings applied"

  Step "Packaging"
  # Only tracked files: .env, data/users.json and the session secret are gitignored and must
  # never travel in the package.
  $zip = Join-Path ([IO.Path]::GetTempPath()) "cloudlens-deploy.zip"
  $stage = Join-Path ([IO.Path]::GetTempPath()) "cloudlens-stage"
  if (Test-Path $zip)   { Remove-Item $zip -Force }
  if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
  New-Item -ItemType Directory -Path $stage | Out-Null
  $tracked = git ls-files
  if (-not $tracked) { throw "No tracked files found. Run this from inside the repository." }
  foreach ($f in $tracked) {
    $dest = Join-Path $stage $f
    New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
    Copy-Item $f $dest
  }
  Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
  Ok "$($tracked.Count) files, $([Math]::Round((Get-Item $zip).Length / 1MB, 1)) MB"

  Step "Deploying"
  # A build that outruns the deploy API's sync wait comes back 502 or 504 while Oryx carries on
  # and finishes. Guessing from the status code would be its own kind of lie, so on any failure
  # ask ARM what actually happened before deciding.
  #
  # This matters more than a tidy exit code: a build that dies part-way leaves wwwroot holding
  # the new source with no virtualenv, and the site then fails to start with 'No module named
  # uvicorn'. There is no previous version to fall back to, so a wrong answer here is the
  # difference between a known-broken app and a mystery.
  $deployments = "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/" +
                 "$ResourceGroup/providers/Microsoft.Web/sites/$AppName/deployments" +
                 "?api-version=2022-03-01"

  # Stop the site first. Once wwwroot is mid-replacement the container cannot start, and App
  # Service retries it every few seconds -- each attempt competing with pip for the single core
  # a B1 has.
  Az webapp stop -g $ResourceGroup -n $AppName --only-show-errors | Out-Null
  Ok "site stopped, so the build gets the machine to itself"

  # Oryx fetches the Python SDK from a registry on every build, and on a small plan that fetch
  # fails often enough to matter -- at a different point each time, which is the signature of a
  # transient rather than anything wrong with the app. A failed build is not survivable here
  # (wwwroot ends up holding source with no virtualenv, so the site cannot start at all), so
  # retry rather than leave it broken and call that a result.
  $deployed = $false
  for ($try = 1; $try -le 3 -and -not $deployed; $try++) {
    if ($try -gt 1) {
      Warn "Retrying the deploy (attempt $try of 3)."
      Start-Sleep -Seconds 20
    }
    try {
      try {
        Az webapp deploy -g $ResourceGroup -n $AppName --src-path $zip --type zip `
          --only-show-errors | Out-Null
        Ok "uploaded"
        $deployed = $true
      }
      catch {
        $uploadError = $_
        Warn "The deploy call returned an error. Asking Azure whether the build finished anyway."
        Warn "(Oryx installs dependencies server-side and can take 20 minutes on a B1.)"
        $state = 0
        for ($i = 1; $i -le 120; $i++) {
          Start-Sleep -Seconds 15
          $latest = (AzTry rest --method GET --url $deployments `
            --query "value[0].properties.{s:status,c:complete}" -o json) | ConvertFrom-Json
          if ($latest) {
            # 3 = Failed, 4 = Success.
            if ($latest.s -eq 4) { $state = 4; break }
            if ($latest.s -eq 3 -and $latest.c) { $state = 3; break }
          }
          if ($i % 4 -eq 0) { Write-Host "    still building ($($i * 15)s)" }
        }
        if ($state -eq 4) {
          Ok "the build completed despite the failed call"
          $deployed = $true
        } elseif ($state -eq 3) {
          Warn "The build failed."
          if ($try -eq 3) {
            Warn "Three builds failed. wwwroot holds the new source with no dependencies, so"
            Warn "the site will not start. Read the reason with:"
            Warn "  az webapp log deployment show -g $ResourceGroup -n $AppName"
            throw $uploadError
          }
        } else {
          Warn "Still building after 30 minutes. Check it with:"
          Warn "  az webapp log deployment show -g $ResourceGroup -n $AppName"
          throw $uploadError
        }
      }
    }
    catch {
      if ($try -eq 3) { throw }
    }
  }

  # Always, including after a failed build: leaving the site stopped would turn a bad deploy
  # into an outage with no error page to explain it.
  Az webapp start -g $ResourceGroup -n $AppName --only-show-errors | Out-Null
  Ok "site started"
  Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item $zip -Force -ErrorAction SilentlyContinue

  Step "Verifying"
  $base = "https://$AppName.azurewebsites.net"
  $healthy = $false
  for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 15
    try {
      $r = Invoke-WebRequest "$base/healthz" -TimeoutSec 60 -SkipHttpErrorCheck
      Write-Host "    attempt $i : $($r.StatusCode)"
      if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
      Write-Host "    attempt $i : starting..."
    }
  }

  if ($healthy) {
    Ok "healthy"
    $root = Invoke-WebRequest $base -TimeoutSec 60 -SkipHttpErrorCheck -MaximumRedirection 0
    if ($root.StatusCode -eq 401 -or $root.StatusCode -eq 302) {
      Ok "sign-in is being enforced (got $($root.StatusCode) on /)"
    } else {
      Warn "/ returned $($root.StatusCode) -- check that authentication is configured."
    }
    Write-Host "`nOpen $base" -ForegroundColor Green
  } else {
    Warn "Never became healthy. The usual cause is a missing or wrong PROJECT_ENDPOINT."
    Warn "Read the log:  az webapp log tail -g $ResourceGroup -n $AppName"
    exit 1
  }
}
finally {
  Pop-Location
}
