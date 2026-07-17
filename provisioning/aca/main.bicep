// =====================================================================
//  ACA Sandboxes provisioning — cloud execution base (EXECUTOR=aca)
// =====================================================================
//
// One `az deployment sub create` stands up the whole cloud footprint:
//   identity (Graph) · sandbox groups · storage · registry · ACA env ·
//   self-hosted redis · the MCP Container App · RBAC · FIC.
//
// Scope is subscription so we can create the resource group; Graph modules run
// at tenant scope, the rest at RG scope. Principal ids flow between modules:
// sandbox-group MI -> (FIC subject, SandboxGroup Data Owner); MCP app MI ->
// (Data Owner, Blob Data Contributor, AcrPull). See provisioning/aca/README.md.

targetScope = 'subscription'

extension microsoftGraphV1

@description('Display-name / resource prefix.')
param name string = 'dataops-mcp'

@description('Region. Must support Microsoft.App/sandboxGroups (e.g. westus2, eastus2).')
param location string = 'westus2'

@description('Resource group to create for the ACA stack.')
param resourceGroupName string = '${name}-aca-rg'

@description('MCP server image. Placeholder until the real image is pushed to ACR post-deploy.')
param mcpImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Sandbox disk image container ref (in ACR). Empty -> manager uses the public "ubuntu" disk.')
param sandboxImage string = ''

@description('MCP server OBO client secret. Set during deploy after credential reset.')
@secure()
param mcpClientSecret string = ''

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

// --- Entra identity (Graph, tenant scope) ---
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: name
  }
}

// --- Sandbox groups (each with a SystemAssigned MI) ---
module sandboxGroups 'modules/sandbox-groups.bicep' = {
  name: 'sandbox-groups'
  scope: rg
  params: {
    name: name
    location: location
  }
}

// --- Storage (workspace blob container) ---
module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    name: name
    location: location
    workspaceId: environment.outputs.workspaceId
  }
}

// --- Container registry ---
module registry 'modules/registry.bicep' = {
  name: 'registry'
  scope: rg
  params: {
    name: name
    location: location
  }
}

// --- Container Apps environment + Log Analytics ---
module environment 'modules/environment.bicep' = {
  name: 'environment'
  scope: rg
  params: {
    name: name
    location: location
  }
}

// --- MCP observability (layer 1): MCPAudit_CL table + its Direct DCR ---
module observability 'modules/mcp-observability.bicep' = {
  name: 'observability'
  scope: rg
  params: {
    name: name
    location: location
    workspaceId: environment.outputs.workspaceId
  }
}

// --- Self-hosted redis ---
module redis 'modules/redis.bicep' = {
  name: 'redis'
  scope: rg
  params: {
    name: name
    location: location
    environmentId: environment.outputs.environmentId
  }
}

// --- MCP server Container App ---
module mcpApp 'modules/mcp-app.bicep' = {
  name: 'mcp-app'
  scope: rg
  params: {
    name: name
    location: location
    environmentId: environment.outputs.environmentId
    environmentDefaultDomain: environment.outputs.environmentDefaultDomain
    mcpImage: mcpImage
    mcpClientSecret: mcpClientSecret
    tenantId: tenant().tenantId
    mcpAppId: identity.outputs.mcpAppId
    diagnoseGroupId: identity.outputs.diagnoseGroupId
    actionGroupId: identity.outputs.actionGroupId
    subscriptionId: subscription().subscriptionId
    resourceGroupName: resourceGroupName
    acaRegion: location
    diagnoseSandboxGroup: sandboxGroups.outputs.diagnoseGroupName
    actionSandboxGroup: sandboxGroups.outputs.actionGroupName
    diagnoseSpAppId: identity.outputs.diagnoseSpAppId
    actionSpAppId: identity.outputs.actionSpAppId
    redisHost: redis.outputs.redisHost
    redisPort: redis.outputs.redisPort
    storageAccount: storage.outputs.storageAccountName
    blobContainer: storage.outputs.blobContainerName
    blobContainerResourceId: storage.outputs.blobContainerResourceId
    sandboxImage: sandboxImage
    auditDcrEndpoint: observability.outputs.dcrEndpoint
    auditDcrImmutableId: observability.outputs.dcrImmutableId
    auditStreamName: observability.outputs.streamName
  }
}

// --- RBAC: MCP MI -> Data Owner (both groups) + Blob Data Contributor + AcrPull
module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  scope: rg
  params: {
    diagnoseGroupName: sandboxGroups.outputs.diagnoseGroupName
    actionGroupName: sandboxGroups.outputs.actionGroupName
    storageAccountName: storage.outputs.storageAccountName
    registryName: registry.outputs.registryName
    mcpPrincipalId: mcpApp.outputs.mcpPrincipalId
    diagnoseMiPrincipalId: sandboxGroups.outputs.diagnoseMiPrincipalId
    actionMiPrincipalId: sandboxGroups.outputs.actionMiPrincipalId
    auditDcrName: observability.outputs.dcrName
  }
}

// --- FIC: worker SP <- sandbox-group MI trust (Graph, tenant scope) ---
module fic 'modules/fic.bicep' = {
  name: 'fic'
  scope: rg
  // Explicit: FIC references the worker SP apps by uniqueName (existing), which
  // must already be created by the identity module before fic preflights.
  dependsOn: [
    identity
  ]
  params: {
    name: name
    tenantId: tenant().tenantId
    diagnoseMiPrincipalId: sandboxGroups.outputs.diagnoseMiPrincipalId
    actionMiPrincipalId: sandboxGroups.outputs.actionMiPrincipalId
  }
}

// --- Worker SP Azure RBAC (subscription scope) ---
// diagnose-sp -> Reader (read-only investigation); action-sp -> Contributor.
module workerRbac 'modules/worker-rbac.bicep' = {
  name: 'worker-rbac'
  params: {
    diagnoseSpObjectId: identity.outputs.diagnoseSpObjectId
    actionSpObjectId: identity.outputs.actionSpObjectId
  }
}

// --- Outputs (consumed by write-env.sh) ---
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output RESOURCE_GROUP string = rg.name
output LOCATION string = location
output MCP_APP_ID string = identity.outputs.mcpAppId
output CLI_CLIENT_APP_ID string = identity.outputs.cliClientAppId
output DIAGNOSE_GROUP_ID string = identity.outputs.diagnoseGroupId
output ACTION_GROUP_ID string = identity.outputs.actionGroupId
output DIAGNOSE_SP_APP_ID string = identity.outputs.diagnoseSpAppId
output ACTION_SP_APP_ID string = identity.outputs.actionSpAppId
output DIAGNOSE_SANDBOX_GROUP string = sandboxGroups.outputs.diagnoseGroupName
output ACTION_SANDBOX_GROUP string = sandboxGroups.outputs.actionGroupName
output REDIS_HOST string = redis.outputs.redisHost
output STORAGE_ACCOUNT string = storage.outputs.storageAccountName
output BLOB_CONTAINER string = storage.outputs.blobContainerName
output BLOB_CONTAINER_RESOURCE_ID string = storage.outputs.blobContainerResourceId
output REGISTRY_LOGIN_SERVER string = registry.outputs.loginServer
output REGISTRY_NAME string = registry.outputs.registryName
output MCP_APP_NAME string = mcpApp.outputs.mcpAppName
output MCP_FQDN string = mcpApp.outputs.mcpFqdn
output MCP_PRINCIPAL_ID string = mcpApp.outputs.mcpPrincipalId
