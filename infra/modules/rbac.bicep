// Subscription-scope role assignments: the two that decide whether the app can see anything.
//
// The app's managed identity is the ceiling on what *every* user sees. A person with Owner on
// a subscription the identity has no Reader on still sees nothing from it — so these are not
// hardening, they are the feature.

targetScope = 'subscription'

@description('Object id of the web app managed identity.')
param principalId string

// Reader: enumerate subscriptions and resources, which is how the estate is discovered at all.
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

// Cost Management Reader: query cost. Reader alone does not include it — the cost APIs are a
// separate data plane, and this is the role people most often forget.
var costReader = '72fafb9e-0641-4937-9268-a91bfd8191a3'

resource readerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, principalId, reader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource costAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, principalId, costReader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', costReader)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
