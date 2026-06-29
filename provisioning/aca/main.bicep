// =====================================================================
//  ACA Sandboxes provisioning — cloud execution base (EXECUTOR=aca)
// =====================================================================
//
// Subscription-scoped so it can create the resource group, then fans out to
// modules for identity (Graph), sandbox groups, storage, redis, the MCP
// Container App, and RBAC wiring. See provisioning/aca/README.md.
//
// Phase 2 lands this shell (RG only). Phase 2b adds the modules and wires
// principal ids between them (sandbox-group MI -> FIC + Data Owner role).

targetScope = 'subscription'

@description('Display-name / resource prefix (e.g. "dataops-mcp")')
param name string = 'dataops-mcp'

@description('Azure region for the cloud footprint. Must support Microsoft.App/sandboxGroups.')
param location string = 'westus2'

@description('Resource group to create/use for the ACA stack.')
param resourceGroupName string = '${name}-aca-rg'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

// Phase 2b wires the modules here:
//   module identity      'modules/identity.bicep'       (tenant / Graph)
//   module sandboxGroups 'modules/sandbox-groups.bicep' (scope: rg)
//   module storage       'modules/storage.bicep'        (scope: rg)
//   module redis         'modules/redis.bicep'          (scope: rg)
//   module mcpApp        'modules/mcp-app.bicep'        (scope: rg)
//   module rbac          'modules/rbac.bicep'           (FIC + role assignments)

output RESOURCE_GROUP string = rg.name
output LOCATION string = location
