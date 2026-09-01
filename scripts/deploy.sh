#!/usr/bin/env bash
#
# CloudLens — one command, from nothing to a running app.
#
# Creates the Entra app registration, provisions the infrastructure, deploys the code, and
# waits until the app actually answers rather than assuming the upload worked.
#
# Safe to re-run: every step is idempotent. Re-running after a failure picks up where it
# stopped, and re-running after success updates in place.
#
#   ./scripts/deploy.sh --admin you@yourcompany.com
#
# What you need before starting:
#   * Azure CLI, signed in       (az login)
#   * Owner, or Contributor + User Access Administrator, on the subscription
#   * Permission to register an application in your tenant (most tenants allow this; if
#     yours does not, see --no-sso below)

set -euo pipefail

RG="rg-cloudlens"
APP_NAME="cloudlens"
LOCATION=""
AI_LOCATION="eastus"
SKU="B1"
MODEL="gpt-4.1-mini"
MODEL_SKU="GlobalStandard"
ADMIN=""
NO_SSO=0
CHECK_ONLY=0
SUBSCRIPTION=""
PASSPHRASE="${CLOUDLENS_PASSPHRASE:-}"
REPO="https://github.com/samy6352/CloudLens-Deploy"
BRANCH="main"

usage() {
  cat <<'EOF'
CloudLens deployment

  --admin EMAIL         Who may refresh data and run first-time setup. Defaults to you.
  --passphrase PHRASE   Deployment passphrase. Also read from CLOUDLENS_PASSPHRASE.
                        Prompted for if not supplied.
  --resource-group NAME Resource group to create or reuse       (default: rg-cloudlens)
  --name NAME           Base name for resources                 (default: cloudlens)
  --location REGION     Region for the app. Must have App Service quota.
  --ai-location REGION  Region for the model                    (default: eastus)
  --sku SKU             App Service plan                        (default: B1)
  --model NAME          Model deployment                        (default: gpt-4.1-mini)
  --model-sku SKU       GlobalStandard | Standard               (default: GlobalStandard)
  --subscription ID     Subscription to deploy into             (default: current)
  --no-sso              Skip the app registration and use a local admin password
  --check-only          Run the checks and stop, without creating anything
  -h, --help            This message

Examples:
  ./scripts/deploy.sh --admin me@contoso.com
  ./scripts/deploy.sh --admin me@contoso.com --location westeurope --ai-location swedencentral
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin) ADMIN="$2"; shift 2 ;;
    --passphrase) PASSPHRASE="$2"; shift 2 ;;
    --resource-group) RG="$2"; shift 2 ;;
    --name) APP_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --ai-location) AI_LOCATION="$2"; shift 2 ;;
    --sku) SKU="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --model-sku) MODEL_SKU="$2"; shift 2 ;;
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --no-sso) NO_SSO=1; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
fail() { printf '\n\033[31mFailed: %s\033[0m\n' "$1" >&2; exit 1; }

command -v az >/dev/null 2>&1 || fail "Azure CLI is not installed. See https://aka.ms/azcli"

step "Checking your Azure sign-in"
az account show >/dev/null 2>&1 || fail "Not signed in. Run: az login"
[[ -n "$SUBSCRIPTION" ]] && az account set --subscription "$SUBSCRIPTION"

SUB_ID=$(az account show --query id -o tsv)
SUB_NAME=$(az account show --query name -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SIGNED_IN=$(az account show --query user.name -o tsv)
info "Subscription: $SUB_NAME"
info "Tenant:       $TENANT_ID"
info "Signed in as: $SIGNED_IN"

[[ -z "$ADMIN" ]] && ADMIN="$SIGNED_IN"

# Asked for rather than defaulted, and read with -s so it does not end up in a shell history
# file or a CI log. Prompting beats failing three minutes into a deployment with an ARM error.
#
# Never *prompted* for under --check-only, but used when one was supplied via --passphrase or
# CLOUDLENS_PASSPHRASE. Somebody deciding whether their subscription can host this has no reason
# to have been given the passphrase yet, and demanding one to run a read-only check makes the
# question unanswerable for exactly the person asking it. But the template is what ARM
# validates, and it stops on the passphrase before evaluating a single resource — so when one
# *is* to hand it buys the real quota and template checks.
#
# `|| true` because a failed read must not be the end of it. With no terminal attached the read
# returns non-zero at EOF, and `set -e` would take that as the script's cue to exit — silently,
# with status 1 and not a word about why, since the prompt is only shown to a terminal in the
# first place. Letting it fall through leaves the variable empty, which the next line explains.
if [[ $CHECK_ONLY -eq 0 && -z "$PASSPHRASE" ]]; then
  read -r -s -p "    Deployment passphrase: " PASSPHRASE || true
  echo
fi
if [[ $CHECK_ONLY -eq 0 && -z "$PASSPHRASE" ]]; then
  fail "A deployment passphrase is required. Ask whoever gave you this
    repository, or set CLOUDLENS_PASSPHRASE."
fi

# A region has to be chosen before anything is created, and the honest default is the one the
# subscription is already using rather than a guess that may have no quota.
if [[ -z "$LOCATION" ]]; then
  LOCATION=$(az group list --query "[0].location" -o tsv 2>/dev/null || echo "")
  [[ -z "$LOCATION" ]] && LOCATION="eastus"
  info "Region:       $LOCATION (override with --location)"
fi

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

step "Checking this subscription can host CloudLens"

BLOCKERS=()
NOTES=()

# The authoritative answer to "may I create a role assignment here", which is what the
# Owner-versus-Contributor distinction actually decides. Contributor carries `*` in its actions
# and excludes `Microsoft.Authorization/*/Write` in its notActions, so asking for the effective
# permissions is exact — and free, where the alternative is creating one to find out.
#
# The patterns are matched with bash's own `case` globbing, which is the same semantic Azure
# uses: `*` stands for any run of characters, `/` included. That keeps this to the Azure CLI
# and bash, rather than adding a dependency to a script whose whole point is being easy to run.
WANTED_ACTION="Microsoft.Authorization/roleAssignments/write"
PERMS=$(az rest --method GET \
  --url "https://management.azure.com/subscriptions/${SUB_ID}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01" \
  --query "value[].{a: join(';', actions || ['']), n: join(';', notActions || [''])}" \
  -o tsv 2>/dev/null || echo "")

CAN_ASSIGN_ROLES="unknown"
if [[ -n "$PERMS" ]]; then
  CAN_ASSIGN_ROLES="no"
  shopt -s nocasematch
  while IFS=$'\t' read -r ACTIONS NOT_ACTIONS; do
    [[ -z "${ACTIONS:-}" ]] && continue
    GRANTED=0
    IFS=';' read -ra ACTION_LIST <<< "$ACTIONS"
    for PATTERN in "${ACTION_LIST[@]}"; do
      [[ -z "$PATTERN" ]] && continue
      case "$WANTED_ACTION" in $PATTERN) GRANTED=1; break ;; esac
    done
    [[ $GRANTED -eq 0 ]] && continue

    WITHHELD=0
    IFS=';' read -ra NOT_LIST <<< "${NOT_ACTIONS:-}"
    for PATTERN in "${NOT_LIST[@]}"; do
      [[ -z "$PATTERN" ]] && continue
      case "$WANTED_ACTION" in $PATTERN) WITHHELD=1; break ;; esac
    done
    if [[ $WITHHELD -eq 0 ]]; then CAN_ASSIGN_ROLES="yes"; break; fi
  done <<< "$PERMS"
  shopt -u nocasematch
fi

if [[ "$CAN_ASSIGN_ROLES" == "no" ]]; then
  BLOCKERS+=("You cannot create role assignments on this subscription.
    CloudLens grants its managed identity Reader and Cost Management Reader. Without them the
    app deploys perfectly and reports an empty estate, so this stops now rather than after
    building one.
    Fix: ask for Owner, or Contributor plus User Access Administrator.")
elif [[ "$CAN_ASSIGN_ROLES" != "yes" ]]; then
  NOTES+=("Could not read your permissions on this subscription; skipping the rights check.")
fi

# Providers are registered rather than reported. A missing one is a real failure, but it is
# also the one obstacle here that can simply be removed without asking anybody for anything.
#
# CostManagementExports earns its place by being the one that fails late and quietly. It is not
# needed to deploy anything, so a preflight that only considers the estate would leave it out —
# and then first-run setup asks Azure for a daily cost export, gets "RP Not Registered", and
# falls back to the slower API path. The data still arrives, so nothing looks broken; the
# deployment is just permanently slower than it should be, for a reason nobody would think to
# look for. Observed on a Visual Studio subscription where it was the only one unregistered.
for NS in Microsoft.Web Microsoft.CognitiveServices Microsoft.Storage \
          Microsoft.CostManagement Microsoft.CostManagementExports; do
  STATE=$(az provider show -n "$NS" --query registrationState -o tsv 2>/dev/null || echo "")
  if [[ -n "$STATE" && "$STATE" != "Registered" ]]; then
    info "Registering resource provider $NS"
    az provider register -n "$NS" >/dev/null 2>&1 || true
  fi
done

# App Service capacity, asked of ARM rather than of the catalogue.
#
# `az appservice list-locations --sku B1` answers a different question than it appears to: it
# lists the regions that *offer* B1, not the regions this subscription may create one in. On a
# Visual Studio subscription those differ — verified against one that had B1 in Central India
# and a hard limit of zero VMs in East US 2, while the catalogue listed both. The check that
# only reads the catalogue passes and the deployment then fails on quota, which is precisely
# the late failure this whole section exists to prevent.
#
# `deployment sub validate` is ARM's own preflight. It runs the resource providers' quota
# checks and creates nothing, so it is both the authoritative answer and a free one. Doing it
# here also validates the template itself — a bad parameter is caught before the app
# registration is created rather than after.
WANTED_REGION=$(echo "$LOCATION" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
SKU_REGIONS=$(az appservice list-locations --sku "$SKU" --query "[].name" -o tsv 2>/dev/null || echo "")
if [[ -n "$SKU_REGIONS" ]]; then
  if ! echo "$SKU_REGIONS" | tr -d '[:blank:]' | tr '[:upper:]' '[:lower:]' | grep -qx "$WANTED_REGION"; then
    BLOCKERS+=("$SKU App Service is not offered in $LOCATION at all.
    Fix: --location with another region. See https://azure.microsoft.com/regions/services/")
  fi
fi

# Validation needs the real passphrase. The template stops on it deliberately, before ARM
# evaluates a single resource, so validating with a placeholder returns the passphrase error
# and never reaches the quota checks — the fail-fast gate working exactly as designed, and a
# reminder that this check reports on what it was actually able to test.
#
# Under --check-only there is no passphrase by design, so the deep check is skipped and said to
# be skipped, rather than passing quietly and implying more than was verified.
if [[ -n "$PASSPHRASE" ]]; then
  VALIDATION=$(az deployment sub validate \
    --location "$LOCATION" \
    --template-file "$(dirname "$0")/../infra/main.bicep" \
    --parameters \
      resourceGroupName="$RG" \
      location="$LOCATION" \
      aiLocation="$AI_LOCATION" \
      appName="$APP_NAME" \
      sku="$SKU" \
      modelName="$MODEL" \
      modelSku="$MODEL_SKU" \
      adminEmails="$ADMIN" \
      deploymentPassphrase="$PASSPHRASE" \
    2>&1 || true)

  DETAIL=$(grep -oE '"message": *"[^"]{0,300}' <<< "$VALIDATION" | head -1 | cut -c13-)
  if grep -q 'DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT' <<< "$VALIDATION"; then
    BLOCKERS+=("That deployment passphrase is not correct.
    Fix: ask whoever gave you this repository. Nothing was created.")
  elif grep -q 'SubscriptionIsOverQuotaForSku' <<< "$VALIDATION"; then
    BLOCKERS+=("This subscription has no $SKU App Service quota in $LOCATION.
    The region offers it; your subscription has no allowance for it here. Credit-based
    subscriptions such as Visual Studio start with none in most regions, and one already in
    use elsewhere does not grant another.
    Fix: --location with a region you have quota in, or request more at
    https://aka.ms/antquotahelp")
  elif grep -qE 'InsufficientQuota|QuotaExceeded' <<< "$VALIDATION"; then
    # Model capacity is reported this way rather than as a SKU problem, and names its own limit.
    BLOCKERS+=("This subscription is over quota in $LOCATION or $AI_LOCATION.
    ${DETAIL}
    Fix: another region, a smaller --model-capacity, or request more quota.")
  elif grep -qE 'AuthorizationFailed|InvalidTemplateDeployment|MissingSubscriptionRegistration|LocationNotAvailable' <<< "$VALIDATION"; then
    # Deliberately not a catch-all. ARM emits an informational "nested deployment got
    # short-circuited ... WhatIfEvalStopped" whenever a nested template takes a parameter it
    # cannot evaluate ahead of time — which this one does, and which says nothing about whether
    # the deployment would work. Treating every code as fatal turned a perfectly good region
    # into a blocker; only the codes that mean a refusal are listed.
    BLOCKERS+=("Azure rejected this deployment before it started.
    ${DETAIL}")
  fi
else
  NOTES+=("Skipped the quota and template checks, which need the passphrase; region availability was still checked.")
fi

# Model availability, which is quota of a different kind and independent of the one above —
# the regions with App Service capacity frequently lack model capacity and the reverse.
MODEL_SKUS=$(az cognitiveservices model list -l "$AI_LOCATION" \
  --query "[?model.name=='${MODEL}'].model.skus[].name" -o tsv 2>/dev/null || echo "")
if [[ -n "$MODEL_SKUS" ]]; then
  if ! echo "$MODEL_SKUS" | grep -qx "$MODEL_SKU"; then
    OFFERED=$(echo "$MODEL_SKUS" | sort -u | paste -sd ', ' -)
    BLOCKERS+=("$MODEL is not offered as $MODEL_SKU in $AI_LOCATION.
    That region offers: $OFFERED
    Fix: --model-sku with one of those, or --ai-location with another region.")
  fi
else
  NOTES+=("$MODEL does not appear to be available in $AI_LOCATION at all; try --ai-location eastus.")
fi

# An existing group is not an error — re-running over the top is the documented way to update,
# and to add SSO to a deployment that began at the portal button. Saying so removes the doubt.
if [[ "$(az group exists -n "$RG" -o tsv 2>/dev/null)" == "true" ]]; then
  COUNT=$(az resource list -g "$RG" --query "[].id" -o tsv 2>/dev/null | grep -c . || true)
  NOTES+=("Resource group $RG already exists with ${COUNT} resource(s); this run updates it in place.")
fi

for N in "${NOTES[@]:-}"; do [[ -n "$N" ]] && info "$N"; done

if [[ ${#BLOCKERS[@]} -gt 0 ]]; then
  printf '\n\033[31mThis subscription cannot host CloudLens yet:\033[0m\n'
  for B in "${BLOCKERS[@]}"; do printf '  - %s\n' "$B"; done
  fail "Nothing was created."
fi
info "Everything CloudLens needs is available."

if [[ $CHECK_ONLY -eq 1 ]]; then
  printf '\n\033[32mChecks passed. Nothing was created.\033[0m\n'
  printf 'Re-run without --check-only to deploy.\n'
  exit 0
fi

# ---------------------------------------------------------------- app registration
# Created before the infrastructure because the web app needs its client id, and created with
# the redirect URI the app will end up on — which is derived from the same name hash Bicep
# uses, so the two cannot disagree.
CLIENT_ID=""
if [[ $NO_SSO -eq 0 ]]; then
  step "Registering the application in your tenant"

  DISPLAY_NAME="CloudLens (${APP_NAME})"
  EXISTING=$(az ad app list --display-name "$DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null || echo "")

  if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
    CLIENT_ID="$EXISTING"
    info "Reusing existing registration: $CLIENT_ID"
  else
    # A public client using PKCE, with no secret. Tenants that cap secret lifetimes are
    # increasingly common, and a deployment that needs a secret rotated every 90 days is a
    # deployment that breaks in 90 days.
    #
    # The redirect URI is added after deployment rather than here: it contains the app's
    # hostname, which Bicep derives from a hash of the resource group id. Guessing it now and
    # being wrong produces a sign-in that fails at the final step with an error naming a URI
    # the person never typed.
    if CLIENT_ID=$(az ad app create \
      --display-name "$DISPLAY_NAME" \
      --sign-in-audience AzureADMyOrg \
      --query appId -o tsv 2>/dev/null); then
      info "Created registration: $CLIENT_ID"
      # A service principal in this tenant, so the app can be consented to and assigned.
      az ad sp create --id "$CLIENT_ID" >/dev/null 2>&1 || true
    else
      # Not fatal. A tenant that reserves app registration for administrators is common, and
      # guests can essentially never do it — but that only decides how people sign in, not
      # whether CloudLens can run. Falling back costs a re-run and gets the same estate.
      info "Your tenant would not let you register an application."
      info "Continuing with a local password instead; the app is otherwise identical."
      info "To move to Azure sign-in later, re-run this script once you have the rights."
      NO_SSO=1
      CLIENT_ID=""
    fi
  fi
fi

# ---------------------------------------------------------------- infrastructure
step "Provisioning Azure resources"
info "This creates an App Service plan, a web app, an AI account with a model, and storage."
info "It takes about five minutes."

DEPLOY_NAME="cloudlens-$(date +%Y%m%d%H%M%S)"
set +e
OUTPUT=$(az deployment sub create \
  --name "$DEPLOY_NAME" \
  --location "$LOCATION" \
  --template-file "$(dirname "$0")/../infra/main.bicep" \
  --parameters \
      resourceGroupName="$RG" \
      location="$LOCATION" \
      aiLocation="$AI_LOCATION" \
      appName="$APP_NAME" \
      sku="$SKU" \
      modelName="$MODEL" \
      modelSku="$MODEL_SKU" \
      adminEmails="$ADMIN" \
      entraClientId="$CLIENT_ID" \
      entraTenantId="$TENANT_ID" \
      deploymentPassphrase="$PASSPHRASE" \
      repositoryUrl="" \
  -o none 2>&1)
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  echo "$OUTPUT" >&2
  # The refusals that actually happen, each with a different fix. A raw ARM error names
  # none of them.
  if grep -q "DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT" <<<"$OUTPUT"; then
    fail "That deployment passphrase is not correct. Ask whoever gave you this repository."
  elif grep -qi "OverQuotaForSku\|quota" <<<"$OUTPUT"; then
    fail "This subscription has no App Service quota in $LOCATION. Credit-based
    subscriptions start with none in most regions. Try --location with another region, or
    request quota at https://aka.ms/antquotahelp"
  elif grep -qi "not supported in this region" <<<"$OUTPUT"; then
    fail "The model $MODEL is not available as $MODEL_SKU in $AI_LOCATION.
    Try --model-sku Standard, or --ai-location eastus."
  elif grep -qi "AuthorizationFailed\|does not have authorization" <<<"$OUTPUT"; then
    fail "You do not have rights to create role assignments on this subscription.
    CloudLens needs Owner, or Contributor plus User Access Administrator, because the app
    reads cost with a managed identity that has to be granted Cost Management Reader."
  fi
  fail "Deployment failed. The Azure error is above."
fi

# Read the outputs back individually rather than parsing JSON in the shell: it keeps this
# script's only hard dependency the Azure CLI, which is already required, instead of adding jq.
outputs() {
  az deployment sub show --name "$DEPLOY_NAME" \
    --query "properties.outputs.$1.value" -o tsv
}
APP_URL=$(outputs appUrl)
WEB_APP=$(outputs appName)
REDIRECT=$(outputs redirectUri)
info "App:      $WEB_APP"
info "URL:      $APP_URL"

# ---------------------------------------------------------------- code
# A zip built here rather than a repository App Service clones for itself. App Service clones
# anonymously, so a private repository would produce a deployment that succeeds and an app that
# serves nothing — and this way the code that gets deployed is demonstrably the code in front of
# you, not whatever happens to be on a branch somewhere.
step "Deploying the application"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ZIP="$(mktemp -d)/cloudlens.zip"

if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  # Tracked files only, so .env, the local warehouse and anything else gitignored cannot be
  # swept into the package.
  git -C "$ROOT" archive --format=zip -o "$ZIP" HEAD
else
  # A download of the source rather than a clone. Exclude the same things by hand.
  (cd "$ROOT" && zip -qr "$ZIP" . \
     -x '.git/*' '*.pyc' '__pycache__/*' 'data/*' '.env' '*.duckdb' '*.log')
fi
info "Package: $(du -h "$ZIP" | cut -f1)"

az webapp deploy --resource-group "$RG" --name "$WEB_APP" \
  --src-path "$ZIP" --type zip --async false -o none \
  || fail "Could not deploy the application code. The infrastructure is in place, so
  re-running this script will retry just this step."
rm -f "$ZIP"

# ---------------------------------------------------------------- redirect URI
# Now the hostname is known for certain, rather than predicted. Set here rather than at
# creation time because getting this wrong produces a sign-in that fails at the last step with
# an error naming a URI the person never typed.
if [[ -n "$CLIENT_ID" ]]; then
  step "Pointing the sign-in redirect at the deployed app"
  OBJ_ID=$(az ad app show --id "$CLIENT_ID" --query id -o tsv)
  # Registered under `publicClient`, which is what this app actually is. The three redirect
  # buckets are not interchangeable and Entra enforces the difference only at redemption, so a
  # wrong one gets through the prompt and the consent screen and fails afterwards — reading as
  # a broken app rather than a misregistered one. Both wrong answers were observed on a real
  # first-run deployment:
  #
  #   spa    AADSTS9002327, tokens "may only be redeemed via cross-origin requests" — an SPA is
  #          expected to redeem from browser JavaScript, and this redeems server-side.
  #   web    a confidential client, which Entra will not redeem without a client secret. The
  #          app says so itself and names AUTH_CLIENT_SECRET.
  #
  # publicClient is the secretless server-side redemption this uses: MSAL PublicClientApplication
  # with PKCE, no secret to rotate and none to leak. The other two are cleared, so re-running
  # over a deployment made before this repairs it rather than leaving a dead URI beside a live
  # one.
  az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/${OBJ_ID}" \
    --headers "Content-Type=application/json" \
    --body "{\"publicClient\":{\"redirectUris\":[\"${REDIRECT}\"]},\"spa\":{\"redirectUris\":[]},\"web\":{\"redirectUris\":[]},\"isFallbackPublicClient\":true}" >/dev/null \
    || info "Could not set the redirect URI automatically. Add $REDIRECT as a
    Mobile and desktop applications (public client) redirect URI on $CLIENT_ID."
  info "Redirect: $REDIRECT"
fi

# ---------------------------------------------------------------- wait for health
step "Waiting for the app to start"
info "App Service builds the Python environment on first deploy, which takes a few minutes."

READY=0
for i in $(seq 1 60); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${APP_URL}/healthz" || echo "000")
  # 200 is healthy; 401 and 302 mean it is up and asking us to sign in, which is also up.
  if [[ "$CODE" == "200" || "$CODE" == "401" || "$CODE" == "302" ]]; then
    READY=1
    break
  fi
  printf '.'
  sleep 15
done
printf '\n'

if [[ $READY -eq 1 ]]; then
  info "The app is answering."
else
  info "The app has not answered yet. Deployments can take up to ten minutes on first run."
  info "Check: az webapp log tail -g $RG -n $WEB_APP"
fi

cat <<EOF

$(printf '\033[1m')CloudLens is deployed.$(printf '\033[0m')

  Open       $APP_URL
  Admin      $ADMIN
  Group      $RG
EOF

if [[ -n "$CLIENT_ID" ]]; then
  cat <<EOF
  Sign-in    Microsoft Entra (app registration $CLIENT_ID)

Sign in with your Azure account. You will see exactly the subscriptions your own
Azure access allows, and nothing else.
EOF
else
  cat <<EOF
  Sign-in    Local account

The first-run password is printed once in the application log:
  az webapp log tail -g $RG -n $WEB_APP
EOF
fi

cat <<EOF

The first time you open it, CloudLens will offer to set up your cost data. That
takes a few minutes and only happens once.

EOF
