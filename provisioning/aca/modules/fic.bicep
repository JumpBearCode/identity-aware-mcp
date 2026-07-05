// =====================================================================
//  Federated Identity Credentials (Microsoft Graph, tenant scope)
// =====================================================================
//
// The passwordless link. Each worker SP app gets a federated credential that
// trusts its sandbox group's managed identity ("configure an app to trust a
// managed identity", preview):
//
//   issuer   = https://login.microsoftonline.com/{tenantId}/v2.0
//   subject  = <sandbox-group MI principalId (object id)>
//   audience = api://AzureADTokenExchange
//
// Inside a sandbox, the group MI's token (audience api://AzureADTokenExchange)
// is exchanged via `az login --service-principal --federated-token` to become
// the worker SP — no secret anywhere in the cloud. Wired last because it needs
// the sandbox-group MI principalIds, which exist only after the groups deploy.

// RG-scoped (see identity.bicep) — the Graph extension still creates the FIC on
// the worker app at tenant level.
targetScope = 'resourceGroup'

extension microsoftGraphV1

@description('Resource prefix (must match identity.bicep).')
param name string

@description('Entra tenant id (FIC issuer).')
param tenantId string

@description('Diagnose sandbox-group MI principalId (FIC subject).')
param diagnoseMiPrincipalId string

@description('Action sandbox-group MI principalId (FIC subject).')
param actionMiPrincipalId string

var issuer = 'https://login.microsoftonline.com/${tenantId}/v2.0'
var audiences = [ 'api://AzureADTokenExchange' ]

resource diagnoseSpApp 'Microsoft.Graph/applications@v1.0' existing = {
  uniqueName: '${name}-diagnose-sp'
}

resource actionSpApp 'Microsoft.Graph/applications@v1.0' existing = {
  uniqueName: '${name}-action-sp'
}

resource diagnoseFic 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${diagnoseSpApp.uniqueName}/diagnose-sandbox-mi'
  description: 'Trust the diagnose sandbox group managed identity'
  issuer: issuer
  subject: diagnoseMiPrincipalId
  audiences: audiences
}

resource actionFic 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${actionSpApp.uniqueName}/action-sandbox-mi'
  description: 'Trust the action sandbox group managed identity'
  issuer: issuer
  subject: actionMiPrincipalId
  audiences: audiences
}
