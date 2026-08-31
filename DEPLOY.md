# Deploying CloudLens

Three ways in. They differ only in how much you want to type — all three end up with the same
resources.

| | Sign-in | Needs | Time |
|---|---|---|---|
| [Portal button](#1-the-portal-button) | Local password | Owner on a subscription | ~8 min |
| [One command](#2-one-command) | Your Azure account | Owner + app registration rights | ~8 min |
| [azd](#3-azd) | Your Azure account | Owner, azd installed | ~8 min |

## Before you start

**You need Owner on the subscription**, or Contributor *plus* User Access Administrator.
Not because CloudLens changes anything — it only ever reads — but because it reads cost with a
managed identity, and granting that identity **Cost Management Reader** is a role assignment.
Without the rights to create one, the app deploys perfectly and shows an empty estate.

**Two quotas decide whether this deploys at all**, and they are independent:

- **App Service.** Credit-based subscriptions start with *zero* compute in most regions.
  `az appservice list-locations` tells you what a region offers, not what you may create.
- **Model.** Per model and per region. `gpt-4.1-mini` is widely available; some regions offer
  only the expensive provisioned tiers.

This is why the app and the AI account are deployed to **different regions by default**. The
regions with App Service quota frequently lack model quota and vice versa, and tying them
together means one shortage blocks a deployment the other would have allowed.

---

## 1. The portal button

The whole thing, including the code, with nothing installed locally.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fsamy6352%2FCloudLens-Deploy%2Fmain%2Fazuredeploy.json)

> **This button only works if the repository is public.** The Azure portal fetches
> `azuredeploy.json` anonymously, and App Service clones the source anonymously too — neither
> can authenticate to a private repository. If this repo is private, the button returns "not
> found" and you want [option 2](#2-one-command), which does not depend on GitHub at all.

Fill in the region and an admin email, and press Create. When it finishes, the **Outputs** tab
of the deployment has the URL.

Sign-in is a **local admin account**, because a portal deployment cannot register an
application for you. The password is printed once into the log:

```bash
az webapp log tail -g rg-cloudlens -n <app-name>
```

To move to Azure sign-in afterwards, run [the script](#2-one-command) over the top — it is
idempotent and will add the registration without disturbing your data.

---

## 2. One command

Adds what the portal cannot do for you: an Entra app registration, so people sign in with
their Azure account and see exactly the subscriptions their own RBAC allows.

This path **works whether the repository is public or private**: it deploys the code as a zip
built from your own clone, so nothing has to be fetchable from GitHub by Azure.

```bash
git clone https://github.com/samy6352/CloudLens-Deploy
cd CloudLens-Deploy
./scripts/deploy.sh --admin you@yourcompany.com
```

```powershell
git clone https://github.com/samy6352/CloudLens-Deploy
cd CloudLens-Deploy
./scripts/deploy.ps1 -Admin you@yourcompany.com
```

Useful switches:

```bash
--location westeurope        # where the app runs; try another if quota fails
--ai-location swedencentral  # where the model lives
--model-sku Standard         # if GlobalStandard is not offered in your AI region
--no-sso                     # tenant does not let you register apps; use a password
--resource-group rg-costs    # somewhere other than rg-cloudlens
```

Both scripts are safe to re-run. Failed halfway? Run it again.

---

## 3. azd

```bash
azd auth login
azd up
```

---

## What gets created

| Resource | Why |
|---|---|
| App Service plan (B1) | B1, not Free: Free has no Always On, so the app cold-starts and a long data load gets killed mid-run |
| Web app (Linux, Python 3.13) | The application |
| AI Foundry account + model | The app **will not start** without one — the chat agent is not optional |
| Storage account | Daily archive, which is what the History tab compares |
| 4 role assignments | Reader + Cost Management Reader on the subscription; blob writer on the storage; model user on the AI account |

Roughly **$15–20/month** for B1 plus storage, and model usage on top — a few dollars a month
at normal use.

### Everything runs as one identity

The web app's managed identity is the **ceiling on what every user sees**. Someone with Owner
on a subscription the identity has no Reader on still sees nothing from it. The templates grant
those roles on the subscription you deploy into; for more, see [Adding
subscriptions](#adding-more-subscriptions).

---

## Signing in

### Your Azure account (recommended)

Set up by the scripts. Each person sees only what their own Azure RBAC allows, so two
colleagues open the same URL and legitimately see different estates.

The registration requests only `openid`, `profile` and `offline_access` — **no admin consent
required**, which means it works in a tenant you do not administer.

There is an optional upgrade, `AUTH_DELEGATED_ARM=true`, where each person's *own* token reads
cost. It is off by default deliberately: it needs ARM `user_impersonation`, which **requires a
tenant administrator** to consent. Turn it on only if you have one to hand.

### A local password

`--no-sso`, for tenants that do not let ordinary users register applications. The password is
printed once into the log at first start.

---

## First run

The first time you open CloudLens it will tell you there is no cost data and offer to set it
up. That runs with your own Azure access, and:

1. Looks for Cost Management exports Azure already writes — fastest, and carries tags
2. Creates a daily export if there are none, for tomorrow onwards
3. Loads three months of history through the Cost Details API

Takes a few minutes. **It is offered once** — after data exists, the offer is gone for good.

Afterwards, each sign-in quietly loads any subscription you can see that the warehouse has not
got yet, in the background.

---

## When it does not work

### "This subscription has no App Service quota in ..."

Real, common, and not about CloudLens. Credit subscriptions start with zero compute nearly
everywhere. Try another region:

```bash
./scripts/deploy.sh --admin you@company.com --location westus2
```

Or ask for quota at <https://aka.ms/antquotahelp>.

### "The specified SKU 'GlobalStandard' ... is not supported in this region"

That model is not offered on that tier there. Either:

```bash
--model-sku Standard        # a regional deployment instead of a global one
--ai-location eastus        # or a region with broader availability
```

To see what a region actually offers:

```bash
az cognitiveservices model list -l <region> \
  --query "[?model.format=='OpenAI'].{name:model.name, skus:join(',',model.skus[].name)}" -o table
```

### "You do not have rights to create role assignments"

You have Contributor but not User Access Administrator. Cost Management Reader cannot be
granted without it, and without that role the app can see nothing. Ask for Owner, or have an
administrator run the deployment.

### "Could not register the application"

Your tenant restricts app registration to administrators. Use `--no-sso` and a local password,
or ask an administrator to create the registration and pass it with `--entra-client-id`.

### The dashboard loads but every question comes back 401

The managed identity is missing its role on the AI account. Reader on the subscription does
**not** cover the Foundry data plane. The templates grant it; if you deployed by hand:

```bash
az role assignment create --assignee-object-id <principal-id> \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope <ai-account-resource-id>
```

### Refresh fails with 422

Your subscription is **WebDirect** — Visual Studio, pay-as-you-go or MSDN. Microsoft does not
allow those to use the Cost Details API at all. The live dashboard and the agent work normally;
for loaded history, point CloudLens at a Cost Management export instead (Cost exports tab).

### The app is empty and nothing explains why

Check the identity has its roles on the subscription you expect:

```bash
PRINCIPAL=$(az webapp identity show -g rg-cloudlens -n <app> --query principalId -o tsv)
az role assignment list --assignee $PRINCIPAL --all -o table
```

---

## Adding more subscriptions

The deployment grants roles on the subscription it ran in. For others:

```bash
PRINCIPAL=$(az webapp identity show -g rg-cloudlens -n <app> --query principalId -o tsv)

for SUB in <sub-id-1> <sub-id-2>; do
  az role assignment create --assignee-object-id $PRINCIPAL \
    --assignee-principal-type ServicePrincipal \
    --role "Reader" --scope "/subscriptions/$SUB"
  az role assignment create --assignee-object-id $PRINCIPAL \
    --assignee-principal-type ServicePrincipal \
    --role "Cost Management Reader" --scope "/subscriptions/$SUB"
done
```

They appear at the next sign-in and are loaded in the background. Role changes take a few
minutes to propagate — do not conclude it failed in the first thirty seconds.

---

## Updating

```bash
az webapp deployment source sync -g rg-cloudlens -n <app-name>
```

Your data lives in `/home`, which a deployment does not touch.

## Removing it

```bash
az group delete -n rg-cloudlens --yes
az ad app delete --id <client-id>   # if you used SSO
```

Role assignments on the subscription outlive the group; remove them with
`az role assignment delete --assignee <principal-id>`.
