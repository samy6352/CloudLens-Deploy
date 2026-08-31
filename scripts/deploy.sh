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
if [[ -z "$PASSPHRASE" ]]; then
  read -r -s -p "    Deployment passphrase: " PASSPHRASE
  echo
  [[ -z "$PASSPHRASE" ]] && fail "A deployment passphrase is required. Ask whoever gave you
    this repository, or set CLOUDLENS_PASSPHRASE."
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
for NS in Microsoft.Web Microsoft.CognitiveServices Microsoft.Storage; do
  STATE=$(az provider show -n "$NS" --query registrationState -o tsv 2>/dev/null || echo "")
  if [[ -n "$STATE" && "$STATE" != "Registered" ]]; then
    info "Registering resource provider $NS"
    az provider register -n "$NS" >/dev/null 2>&1 || true
  fi
done

# App Service quota. This lists what the subscription may create, not merely what the region
# offers, which is the distinction that catches credit-based subscriptions — they start with
# zero compute in most regions.
WANTED_REGION=$(echo "$LOCATION" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
SKU_REGIONS=$(az appservice list-locations --sku "$SKU" --query "[].name" -o tsv 2>/dev/null || echo "")
if [[ -n "$SKU_REGIONS" ]]; then
  if ! echo "$SKU_REGIONS" | tr -d '[:blank:]' | tr '[:upper:]' '[:lower:]' | grep -qx "$WANTED_REGION"; then
    BLOCKERS+=("This subscription has no $SKU App Service quota in $LOCATION.
    Credit-based subscriptions start with none in most regions.
    Fix: --location with another region, or request quota at https://aka.ms/antquotahelp")
  fi
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
  az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/${OBJ_ID}" \
    --headers "Content-Type=application/json" \
    --body "{\"spa\":{\"redirectUris\":[\"${REDIRECT}\"]}}" >/dev/null \
    || info "Could not set the redirect URI automatically. Add $REDIRECT as a
    single-page-application redirect URI on app registration $CLIENT_ID."
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
