// The resources themselves, inside the resource group.

@description('Region for the app and storage.')
param location string

@description('Region for the AI account, which may differ from the app.')
param aiLocation string

@description('Base name for resources.')
param appName string

@description('App Service plan size.')
param sku string

@description('Model to deploy.')
param modelName string

@description('Tokens-per-minute quota, in thousands.')
param modelCapacity int

@description('Model deployment SKU.')
param modelSku string

@description('Admin email addresses, comma separated.')
param adminEmails string

@description('Entra client id, or empty for local sign-in.')
param entraClientId string

@description('Entra tenant id.')
param entraTenantId string

@secure()
@description('Entra client secret, if the registration needs one.')
param entraClientSecret string

@description('Repository to deploy from.')
param repositoryUrl string

@description('Branch to deploy.')
param branch string

// Names have to be globally unique for the web app and the storage account, and stable across
// re-runs so a second deployment updates rather than duplicates. A hash of the resource group
// id gives both — it does not change when you redeploy, and it differs per group.
var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var webAppName = '${appName}-${suffix}'
var storageName = toLower('${take(replace(appName, '-', ''), 11)}${suffix}')
var foundryName = '${appName}-ai-${suffix}'
var planName = 'asp-${appName}-${suffix}'
var projectName = 'cloudlens'

// ---------------------------------------------------------------- storage
// Holds the daily archive: a dated snapshot of each refresh, which is what the History tab
// compares to show what Azure restated overnight.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // The app authenticates with its managed identity, so shared keys are never needed.
    // Turning them off means a leaked connection string cannot exist to be leaked.
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'cloudlens'
  properties: {
    publicAccess: 'None'
  }
}

// ---------------------------------------------------------------- AI Foundry
// Not optional. The application raises on startup without PROJECT_ENDPOINT, so a deployment
// without a model is a deployment that does not run — which is why this is provisioned here
// rather than left as a prerequisite for someone to discover.
resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: foundryName
  location: aiLocation
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: foundryName
    publicNetworkAccess: 'Enabled'
    // Required before a project can be created underneath this account. Older AIServices
    // accounts have it on implicitly — the one this repository was first developed against
    // does — but Azure now refuses the project with "Project can only created under AIServices
    // Kind account with allowProjectManagement set to true" unless it is asked for here. The
    // app needs the project: PROJECT_ENDPOINT is what it talks to, and it will not start
    // without one.
    allowProjectManagement: true
    // Keys off: the app calls this with its managed identity and a bearer token. Local auth
    // would be a second, weaker way in that nothing uses.
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: foundry
  name: projectName
  location: aiLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'CloudLens'
    description: 'Cost analysis agent'
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundry
  name: modelName
  sku: {
    name: modelSku
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
    }
  }
  // A project and a deployment on the same account cannot be created in parallel; ARM returns
  // a conflict. The dependency serialises them.
  dependsOn: [
    project
  ]
}

// ---------------------------------------------------------------- compute
// B1 rather than Free, deliberately. Free has no Always On, so the app cold-starts on every
// request and a long ingest is killed mid-run — and regional VNet integration, which a
// private-endpoint storage account needs, requires Basic or above.
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: {
    name: sku
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      // One worker, one instance. Sessions and the header snapshot live in process memory, so
      // a second worker would send a signed-in person to a process that has never heard of
      // them. Scaling out means moving sessions to Redis first.
      numberOfWorkers: 1
      appCommandLine: 'python -m uvicorn app.main:app --host 0.0.0.0 --port 8000'
      appSettings: [
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'PROJECT_ENDPOINT'
          value: '${foundry.properties.endpoint}api/projects/${projectName}'
        }
        {
          name: 'MODEL_DEPLOYMENT_NAME'
          value: modelName
        }
        // Both of these must live outside wwwroot. A deploy replaces /home/site/wwwroot, so a
        // warehouse or an account file at the default path is destroyed by the next deploy and
        // the app returns empty with no explanation. /home is persistent Azure Files.
        {
          name: 'COST_DB'
          value: '/home/data/costs.duckdb'
        }
        {
          name: 'AUTH_DATA_DIR'
          value: '/home/data'
        }
        {
          name: 'ARCHIVE_ACCOUNT'
          value: storageName
        }
        {
          name: 'ARCHIVE_CONTAINER'
          value: 'cloudlens'
        }
        {
          name: 'ARCHIVE_ACCOUNT_ID'
          value: storage.id
        }
        {
          name: 'AUTH_ADMINS'
          value: adminEmails
        }
        {
          name: 'AUTH_CLIENT_ID'
          value: entraClientId
        }
        {
          name: 'AUTH_TENANT_ID'
          value: empty(entraClientId) ? '' : entraTenantId
        }
        {
          name: 'AUTH_CLIENT_SECRET'
          value: entraClientSecret
        }
        {
          name: 'AUTH_REDIRECT_URI'
          value: 'https://${webAppName}.azurewebsites.net/auth/callback'
        }
        // Behind App Service the app only ever sees HTTPS, so a secure cookie is safe and
        // stops the session travelling in clear if anyone ever fronts this with HTTP.
        {
          name: 'AUTH_COOKIE_SECURE'
          value: 'true'
        }
        // Cost Management throttles by *client type*. An unnamed caller shares a default
        // bucket with everything else in the tenant; naming ourselves gets our own allowance
        // and stops a refresh dissolving into 429s.
        {
          name: 'COST_CLIENT_TYPE'
          value: 'CloudLens'
        }
      ]
    }
  }
}

// Pulls the application code straight from the repository, which is what makes the portal
// button a genuine one-click rather than "now go and deploy the code yourself".
//
// Conditional, because App Service clones anonymously: it cannot authenticate to a private
// repository, and pointing it at one produces a deployment that succeeds and an app that
// serves nothing. The scripts leave this empty and zip-deploy from the local clone instead,
// which works either way.
resource source 'Microsoft.Web/sites/sourcecontrols@2023-12-01' = if (!empty(repositoryUrl)) {
  parent: web
  name: 'web'
  properties: {
    repoUrl: repositoryUrl
    branch: branch
    // Manual integration: no webhook back to the repo, so this works with a repository the
    // deploying user has no admin rights on — including a fork, or this one.
    isManualIntegration: true
  }
}

// The app writes its archive here with its managed identity. Scoped to the storage account
// rather than the group: it needs to write blobs in one place, not to hold a role over
// everything that happens to sit beside it.
var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, web.id, blobContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
    principalId: web.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Reader on the subscription does not reach the Foundry data plane. Without this the dashboard
// loads perfectly and every question comes back 401, which reads as a broken agent rather than
// a missing role.
var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource foundryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, web.id, openAiUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUser)
    principalId: web.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output principalId string = web.identity.principalId
output defaultHostName string = web.properties.defaultHostName
output webAppName string = webAppName
output projectEndpoint string = '${foundry.properties.endpoint}api/projects/${projectName}'
output storageAccount string = storageName
