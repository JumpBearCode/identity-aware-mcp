// =====================================================================
//  Sandbox groups module (Microsoft.App/sandboxGroups, RG scope)
// =====================================================================
//
// Two sandbox groups — one per read/write boundary. Each carries a
// SystemAssigned managed identity; that MI is what the sandbox sees from the
// inside (ACA "inception"), and what each worker SP's Federated Identity
// Credential trusts. egress / lifecycle / per-sandbox login identity are all
// set later on the data plane at runtime, not here.

@description('Resource prefix.')
param name string

@description('Region (must support Microsoft.App/sandboxGroups).')
param location string

resource diagnoseGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: '${name}-diagnose'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource actionGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: '${name}-action'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

output diagnoseGroupName string = diagnoseGroup.name
output actionGroupName string = actionGroup.name
output diagnoseGroupId string = diagnoseGroup.id
output actionGroupId string = actionGroup.id
// Sandbox-group MI principalIds — the subject each worker SP's FIC must trust,
// and (for the MCP app MI) the principal granted SandboxGroup Data Owner.
output diagnoseMiPrincipalId string = diagnoseGroup.identity.principalId
output actionMiPrincipalId string = actionGroup.identity.principalId
