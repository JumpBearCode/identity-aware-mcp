// =====================================================================
//  Worker SP RBAC (role assignments, subscription scope)
// =====================================================================
//
// diagnose-sp -> Reader, action-sp -> Contributor, at subscription scope so
// read-only investigation / scoped writes actually resolve resources. Kept in
// its own module because a roleAssignment name must be computable at the start
// of its deployment — passing the SP object ids in as params satisfies that
// (module outputs referenced inline in the parent would not).
//
// Tighten the scope/role for production; this is the demo's default.

targetScope = 'subscription'

@description('diagnose-sp service principal object id.')
param diagnoseSpObjectId string

@description('action-sp service principal object id.')
param actionSpObjectId string

var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource readerDiagnose 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, diagnoseSpObjectId, readerRoleId)
  properties: {
    principalId: diagnoseSpObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource contributorAction 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, actionSpObjectId, contributorRoleId)
  properties: {
    principalId: actionSpObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
    principalType: 'ServicePrincipal'
  }
}
