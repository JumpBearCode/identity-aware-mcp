// =====================================================================
//  RBAC module (role assignments, RG scope)
// =====================================================================
//
// Wires the MCP app's managed identity to everything it must drive:
//   - Container Apps SandboxGroup Data Owner on BOTH sandbox groups (the role
//     that authorizes the ADC data plane: create/exec/file/delete sandboxes);
//   - Storage Blob Data Contributor on the workspace storage account;
//   - AcrPull on the registry (so the Container App can pull its own image).
//
// Worker SP Reader/Contributor live in main.bicep (subscription scope, the
// scope they need). FIC (worker SP <- group MI trust) lives in fic.bicep.

@description('Diagnose sandbox group name.')
param diagnoseGroupName string

@description('Action sandbox group name.')
param actionGroupName string

@description('Storage account name.')
param storageAccountName string

@description('Container registry name.')
param registryName string

@description('MCP app managed identity principalId.')
param mcpPrincipalId string

@description('Diagnose sandbox-group MI principalId (for BYO blob volume auth).')
param diagnoseMiPrincipalId string

@description('Action sandbox-group MI principalId (for BYO blob volume auth).')
param actionMiPrincipalId string

// Built-in role definition ids.
var sandboxGroupDataOwnerRoleId = 'c24cf47c-5077-412d-a19c-45202126392c'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource diagnoseGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' existing = {
  name: diagnoseGroupName
}

resource actionGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' existing = {
  name: actionGroupName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource dataOwnerDiagnose 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(diagnoseGroup.id, mcpPrincipalId, sandboxGroupDataOwnerRoleId)
  scope: diagnoseGroup
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource dataOwnerAction 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(actionGroup.id, mcpPrincipalId, sandboxGroupDataOwnerRoleId)
  scope: actionGroup
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sandboxGroupDataOwnerRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, mcpPrincipalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, mcpPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalType: 'ServicePrincipal'
  }
}

// Sandbox-group MIs read/write the BYO blob volume on the workspace account.
resource blobDiagnoseMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, diagnoseMiPrincipalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: diagnoseMiPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource blobActionMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, actionMiPrincipalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: actionMiPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

// AcrPull for the sandbox-group MIs so the data plane can build disk images
// from an ACR container ref using the group identity.
resource acrPullDiagnoseMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, diagnoseMiPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: diagnoseMiPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource acrPullActionMi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, actionMiPrincipalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: actionMiPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalType: 'ServicePrincipal'
  }
}
