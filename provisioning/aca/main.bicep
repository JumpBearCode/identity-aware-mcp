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

@description('azd environment name — tags the RG so `azd deploy` can locate resources. Defaults to name for raw `az deployment` use.')
param environmentName string = name

@description('Whether the MCP Container App already exists. azd sets SERVICE_MCP_RESOURCE_EXISTS; true keeps the deployed image instead of the placeholder.')
param mcpAppExists bool = false

// --- Optional app-behavior overrides (all azd-settable via main.parameters.json) ---
// Empty string -> NOT injected into the container, so the server uses its in-code
// default. Each maps to the container env var of the same UPPER_SNAKE name.
param sandboxDistributedLock string = ''
param mcpExecTimeout string = ''
param maxOutputBytes string = ''
param mcpSessionTtl string = ''
param mcpProxyEnabled string = ''
param sandboxAutoSuspendSeconds string = ''
param sandboxAutoDeleteSeconds string = ''
param sandboxReaperInterval string = ''
param sandboxReaperLease string = ''
param sandboxLockTtl string = ''
param sandboxLockWait string = ''
param sandboxCreateTimeout string = ''
param sandboxCpu string = ''
param sandboxMemory string = ''
param sandboxDisk string = ''
param sandboxDiskId string = ''
param blobMountpoint string = ''
param auditTimeout string = ''
param auditUaIncludeOid string = ''

var envOverrides = {
  SANDBOX_DISTRIBUTED_LOCK: sandboxDistributedLock
  MCP_EXEC_TIMEOUT: mcpExecTimeout
  MAX_OUTPUT_BYTES: maxOutputBytes
  MCP_SESSION_TTL: mcpSessionTtl
  MCPPROXY_ENABLED: mcpProxyEnabled
  SANDBOX_AUTO_SUSPEND_SECONDS: sandboxAutoSuspendSeconds
  SANDBOX_AUTO_DELETE_SECONDS: sandboxAutoDeleteSeconds
  SANDBOX_REAPER_INTERVAL: sandboxReaperInterval
  SANDBOX_REAPER_LEASE: sandboxReaperLease
  SANDBOX_LOCK_TTL: sandboxLockTtl
  SANDBOX_LOCK_WAIT: sandboxLockWait
  SANDBOX_CREATE_TIMEOUT: sandboxCreateTimeout
  SANDBOX_CPU: sandboxCpu
  SANDBOX_MEMORY: sandboxMemory
  SANDBOX_DISK: sandboxDisk
  SANDBOX_DISK_ID: sandboxDiskId
  BLOB_MOUNTPOINT: blobMountpoint
  AUDIT_TIMEOUT: auditTimeout
  AUDIT_UA_INCLUDE_OID: auditUaIncludeOid
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
  }
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
    mcpIdentifierUri: identity.outputs.mcpIdentifierUri
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
    // Deterministic sandbox image ref: ACR login server is known at provision
    // time; the image itself is pushed by the postprovision hook before the
    // manager ever pulls it. Removes the old post-deploy `--set-env-vars`.
    sandboxImage: empty(sandboxImage) ? '${registry.outputs.loginServer}/mcp-sandbox:latest' : sandboxImage
    mcpAppExists: mcpAppExists
    envOverrides: envOverrides
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

// --- FIC moved into identity.bicep (co-located with the worker SP apps, same
//     compilation unit) so ARM no longer preflight-validates an `existing` SP
//     reference before the SPs exist. See deployment-gotchas §1. ---

// --- Worker SP Azure RBAC (subscription scope) ---
// diagnose-sp -> Reader (read-only investigation); action-sp -> Contributor.
module workerRbac 'modules/worker-rbac.bicep' = {
  name: 'worker-rbac'
  params: {
    diagnoseSpObjectId: identity.outputs.diagnoseSpObjectId
    actionSpObjectId: identity.outputs.actionSpObjectId
  }
}

// --- Outputs (captured into the azd env; also read by e2e_deployed.py) ---
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output RESOURCE_GROUP string = rg.name
output LOCATION string = location
output MCP_APP_ID string = identity.outputs.mcpAppId
output MCP_IDENTIFIER_URI string = identity.outputs.mcpIdentifierUri
output CLI_CLIENT_APP_ID string = identity.outputs.cliClientAppId
output DIAGNOSE_GROUP_ID string = identity.outputs.diagnoseGroupId
output ACTION_GROUP_ID string = identity.outputs.actionGroupId
output DIAGNOSE_SP_APP_ID string = identity.outputs.diagnoseSpAppId
output ACTION_SP_APP_ID string = identity.outputs.actionSpAppId
// Sandbox-group MI principalIds — the postprovision hook uses them as the FIC subject.
output DIAGNOSE_MI_PRINCIPAL_ID string = sandboxGroups.outputs.diagnoseMiPrincipalId
output ACTION_MI_PRINCIPAL_ID string = sandboxGroups.outputs.actionMiPrincipalId
output DIAGNOSE_SANDBOX_GROUP string = sandboxGroups.outputs.diagnoseGroupName
output ACTION_SANDBOX_GROUP string = sandboxGroups.outputs.actionGroupName
output REDIS_HOST string = redis.outputs.redisHost
output STORAGE_ACCOUNT string = storage.outputs.storageAccountName
output BLOB_CONTAINER string = storage.outputs.blobContainerName
output BLOB_CONTAINER_RESOURCE_ID string = storage.outputs.blobContainerResourceId
output REGISTRY_LOGIN_SERVER string = registry.outputs.loginServer
output REGISTRY_NAME string = registry.outputs.registryName
// azd's containerapp deploy target reads this to know which ACR to push to.
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output MCP_APP_NAME string = mcpApp.outputs.mcpAppName
output MCP_FQDN string = mcpApp.outputs.mcpFqdn
output MCP_PRINCIPAL_ID string = mcpApp.outputs.mcpPrincipalId
