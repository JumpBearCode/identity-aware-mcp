// Role assignments split into a module so it can target a different scope than main.bicep.

targetScope = 'subscription'

param diagnoseSpObjectId string
param actionSpObjectId string
param readerRoleId string
param contributorRoleId string
param scope_ string

resource diagnoseReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(scope_, diagnoseSpObjectId, readerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: diagnoseSpObjectId
    principalType: 'ServicePrincipal'
  }
}

resource actionContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(scope_, actionSpObjectId, contributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalId: actionSpObjectId
    principalType: 'ServicePrincipal'
  }
}
