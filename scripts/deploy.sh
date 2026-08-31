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
SUBSCRIPTION=""
REPO="https://github.com/samy6352/CloudLens-Deploy"
BRANCH="main"

usage() {
  cat <<'EOF'
CloudLens deployment

  --admin EMAIL         Who may refresh data and run first-time setup. Defaults to you.
  --resource-group NAME Resource group to create or reuse       (default: rg-cloudlens)
  --name NAME           Base name for resources                 (default: cloudlens)
  --location REGION     Region for the app. Must have App Service quota.
  --ai-location REGION  Region for the model                    (default: eastus)
  --sku SKU             App Service plan                        (default: B1)
  --model NAME          Model deployment                        (default: gpt-4.1-mini)
  --model-sku SKU       GlobalStandard | Standard               (default: GlobalStandard)
  --subscription ID     Subscription to deploy into             (default: current)
  --no-sso              Skip the app registration and use a local admin password
  -h, --help            This message

Examples:
  ./scripts/deploy.sh --admin me@contoso.com
  ./scripts/deploy.sh --admin me@contoso.com --location westeurope --ai-location swedencentral
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin) ADMIN="$2"; shift 2 ;;
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

# A region has to be chosen before anything is created, and the honest default is the one the
# subscription is already using rather than a guess that may have no quota.
if [[ -z "$LOCATION" ]]; then
  LOCATION=$(az group list --query "[0].location" -o tsv 2>/dev/null || echo "")
  [[ -z "$LOCATION" ]] && LOCATION="eastus"
  info "Region:       $LOCATION (override with --location)"
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
    CLIENT_ID=$(az ad app create \
      --display-name "$DISPLAY_NAME" \
      --sign-in-audience AzureADMyOrg \
      --query appId -o tsv) || fail "Could not register the application. Your tenant may
    restrict this to administrators — re-run with --no-sso to use a local password instead."
    info "Created registration: $CLIENT_ID"
    # A service principal in this tenant, so the app can be consented to and assigned.
    az ad sp create --id "$CLIENT_ID" >/dev/null 2>&1 || true
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
      repositoryUrl="" \
  -o none 2>&1)
RC=$?
set -e

if [[ $RC -ne 0 ]]; then
  echo "$OUTPUT" >&2
  # The three refusals that actually happen, each with a different fix. A raw ARM error names
  # none of them.
  if grep -qi "OverQuotaForSku\|quota" <<<"$OUTPUT"; then
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
