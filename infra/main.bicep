// CloudLens — everything the app needs, in one deployment.
//
// Scoped to the subscription rather than to a resource group, for one reason: the app reads
// cost with its managed identity, and the roles that allow that (Reader, Cost Management
// Reader) are assigned at subscription scope. A resource-group deployment cannot create them,
// so the one thing that makes the product work would be left as a manual step — which is
// exactly the step people miss, producing an app that starts, looks fine, and shows nothing.
//
// What this creates:
//   * A Linux App Service plan and web app running the FastAPI application
//   * An Azure AI Foundry (Cognitive Services) account and a model deployment, because the
//     app refuses to start without PROJECT_ENDPOINT
//   * A storage account for the daily archive
//   * The role assignments that let the app read cost and call the model
//
// Everything is named from a hash of the resource group id, so a second deployment into a
// different group cannot collide with the first, and re-running is idempotent.

targetScope = 'subscription'

@description('Name for the resource group to create or reuse.')
param resourceGroupName string = 'rg-cloudlens'

@description('Region for the app and storage. Must have App Service quota — many subscriptions have none in many regions, so if deployment fails on quota, try another.')
param location string = deployment().location

@description('Region for the AI account. Deliberately separate from `location`: App Service quota and model quota are independent constraints, and the regions that have one frequently lack the other. Leave as-is unless you need the model in a particular geography.')
@allowed(['eastus', 'eastus2', 'westus', 'westus3', 'swedencentral', 'westeurope', 'northeurope', 'uksouth', 'australiaeast', 'japaneast'])
param aiLocation string = 'eastus'

@description('Base name for resources. Lowercase letters and numbers only.')
@minLength(3)
@maxLength(12)
param appName string = 'cloudlens'

@description('App Service plan size. B1 is the smallest with Always On, which scheduled loads need.')
@allowed(['B1', 'B2', 'B3', 'S1', 'P0v3', 'P1v3'])
param sku string = 'B1'

@description('Model to deploy. gpt-4.1-mini is the cheapest current model with wide regional quota. Avoid the gpt-5 reasoning models: the agent sends temperature=0.1, which they reject.')
@allowed(['gpt-4.1-mini', 'gpt-4.1', 'gpt-4o', 'gpt-4.1-nano'])
param modelName string = 'gpt-4.1-mini'

@description('Tokens-per-minute quota for the model, in thousands.')
@minValue(1)
@maxValue(1000)
param modelCapacity int = 20

@description('Model deployment SKU. GlobalStandard has the widest quota but is not offered in every region — if deployment fails on SKU, try Standard, or pick a different region.')
@allowed(['GlobalStandard', 'Standard', 'DataZoneStandard'])
param modelSku string = 'GlobalStandard'

@description('Email addresses allowed to trigger a refresh and run first-time setup.')
param adminEmails string = ''

@description('Entra app registration (client) id for SSO. Leave empty to use a local admin account.')
param entraClientId string = ''

@description('Entra tenant id for SSO. Defaults to the tenant this subscription belongs to.')
param entraTenantId string = ''

@description('Client secret, only if the app registration uses a Web redirect URI.')
@secure()
param entraClientSecret string = ''

@description('Git repository to deploy the application code from. Leave empty when deploying the code separately — the scripts do a zip deploy from your local clone, which works whether or not this repository is public.')
param repositoryUrl string = ''

@description('Branch to deploy.')
param branch string = 'main'

@description('Passphrase required to deploy. Ask whoever gave you this template.')
@secure()
param deploymentPassphrase string = ''

// ---------------------------------------------------------------- the gate
//
// A deliberate speed bump, and worth being straight about what it is and is not.
//
// It is not a security boundary. This template is public, so anyone can fork it, delete these
// few lines and deploy. Nor is there much to defend: a deployment goes into *their*
// subscription, on *their* bill, reading *their* cost data. Nothing of yours is at risk.
//
// What it does do is make deployment deliberate rather than accidental — a button on a public
// repository is otherwise one curious click away from a resource group somebody did not mean
// to create and will be billed for. The passphrase turns that into an intentional act by
// someone who was given it.
//
// The passphrase itself is not here: `uniqueString` is one-way, so a reader of this file
// learns only that a passphrase exists. It is not a cryptographic KDF and would not survive a
// determined offline attack — which is the same point again. A gate, not a wall.
//
// The real protection is on the deployed app, which requires Entra sign-in or a generated
// password before it shows anybody a single figure.
var passphraseHash = 'bwsqa5clihtbg'

// Bicep has no `assert` outside experimental builds, so a wrong passphrase is turned into a
// template-evaluation failure by indexing an empty array. It is built with `json()` rather
// than written as a literal because Bicep folds a literal empty array at compile time and
// refuses the index — this defers the same failure to deployment, which is where it belongs.
//
// ARM reports the name of the *variable being evaluated*, so that name is the error message:
// a deployment without the passphrase stops with "The template variable
// 'DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT' is not valid", which tells someone what to
// do. Anything else here produces an out-of-bounds index error and leaves them guessing.
var noPassphrase = json('[]')
var DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT = uniqueString(deploymentPassphrase) == passphraseHash
  ? 'accepted'
  : string(noPassphrase[0])

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    application: 'CloudLens'
    // Referencing the guard here is what forces ARM to evaluate it. An unused variable is
    // optimised away, and a gate that is optimised away is not a gate.
    deployment: DEPLOYMENT_PASSPHRASE_IS_MISSING_OR_INCORRECT
  }
}

module resources 'modules/resources.bicep' = {
  name: 'cloudlens-resources'
  scope: rg
  params: {
    location: location
    aiLocation: aiLocation
    appName: appName
    sku: sku
    modelName: modelName
    modelCapacity: modelCapacity
    modelSku: modelSku
    adminEmails: adminEmails
    entraClientId: entraClientId
    // An empty tenant means "the one this subscription is in", which is the answer for
    // essentially every single-tenant deployment and saves asking for a GUID people have to
    // go and look up.
    entraTenantId: empty(entraTenantId) ? subscription().tenantId : entraTenantId
    entraClientSecret: entraClientSecret
    repositoryUrl: repositoryUrl
    branch: branch
  }
}

// The roles that make the app able to read anything. Assigned here, at subscription scope,
// because that is where cost lives — and a deployment that skipped them would produce an app
// that runs perfectly and reports an empty estate.
module rbac 'modules/rbac.bicep' = {
  name: 'cloudlens-rbac'
  params: {
    principalId: resources.outputs.principalId
  }
}

output appUrl string = 'https://${resources.outputs.defaultHostName}'
output appName string = resources.outputs.webAppName
output resourceGroup string = rg.name
output principalId string = resources.outputs.principalId
output projectEndpoint string = resources.outputs.projectEndpoint
output storageAccount string = resources.outputs.storageAccount
output redirectUri string = 'https://${resources.outputs.defaultHostName}/auth/callback'
