// Declarative provisioning for the identity-aware DataOps MCP stack.
//
// Uses the Microsoft.Graph Bicep extension (public preview as of 2026) for
// app registrations, service principals, and security groups.
//
// What this file does NOT do:
//   - Create client secrets   (Bicep can't safely emit secrets — use write-env.sh after deploy)
//   - Add users to groups      (use `az ad group member add` per user)
//   - Grant any Azure RBAC     (worker SPs get NO resource access here — assign it yourself later)
//
// targetScope = tenant: only Graph (apps/SPs/groups/grants) resources live here now.

targetScope = 'tenant'

extension microsoftGraphV1

@description('Display-name prefix for created resources (e.g. "dataops-mcp")')
param name string = 'dataops-mcp'

@description('Client ID of VS Code (well-known). Override for tenants using a different MCP client.')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

// --- Well-known IDs for Microsoft Graph ---
var graphAppId = '00000003-0000-0000-c000-000000000000'

// Delegated Graph permission IDs the MCP server requests (declared so they show in portal).
var graphUserRead = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
var graphEmail = '64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0'
var graphOfflineAccess = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'
var graphOpenid = '37f7f235-527c-4136-accd-4a02d197296e'
var graphProfile = '14dad69e-099b-42c9-810b-d002981feec1'

// Scope string the MCP server is admin-consented for (OBO -> Graph).
var graphOboScope = 'User.Read email offline_access openid profile'

// --- Scope GUID for user_impersonation on the MCP server app ---
var userImpersonationScopeId = guid('${name}-mcp-server-user_impersonation')

// =====================================================================
//  MCP Server App (the OAuth Protected Resource)
// =====================================================================
resource mcpServerApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-mcp-server'
  displayName: 'DataOps MCP Server'
  signInAudience: 'AzureADMyOrg'
  identifierUris: [
    'api://${name}-mcp-server'  // Bicep can't reference appId of self; we set this declaratively
  ]
  requiredResourceAccess: [
    {
      resourceAppId: graphAppId
      resourceAccess: [
        { id: graphUserRead, type: 'Scope' }
        { id: graphEmail, type: 'Scope' }
        { id: graphOfflineAccess, type: 'Scope' }
        { id: graphOpenid, type: 'Scope' }
        { id: graphProfile, type: 'Scope' }
      ]
    }
  ]
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: [
      {
        id: userImpersonationScopeId
        adminConsentDescription: 'Access the DataOps MCP server as the signed-in user.'
        adminConsentDisplayName: 'Access DataOps MCP'
        userConsentDescription: 'Access the DataOps MCP server on your behalf.'
        userConsentDisplayName: 'Access DataOps MCP'
        value: 'user_impersonation'
        type: 'User'
        isEnabled: true
      }
    ]
    preAuthorizedApplications: [
      {
        appId: vscodeClientId
        delegatedPermissionIds: [ userImpersonationScopeId ]
      }
    ]
  }
}

resource mcpServerSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: mcpServerApp.appId
}

// Microsoft Graph's own SP in this tenant — needed as the grant's resource.
resource graphSp 'Microsoft.Graph/servicePrincipals@v1.0' existing = {
  appId: graphAppId
}

// Admin consent for OBO -> Graph (tenant-wide delegated grant), so OBO works
// with no per-user consent screen during MCP sign-in.
resource oboGrant 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: mcpServerSp.id
  consentType: 'AllPrincipals'
  resourceId: graphSp.id
  scope: graphOboScope
}

// =====================================================================
//  AD Groups
// =====================================================================
resource diagnoseGroup 'Microsoft.Graph/groups@v1.0' = {
  uniqueName: '${name}-diagnose-users'
  displayName: 'mcp-diagnose-users'
  mailNickname: 'mcp-diagnose-users'
  securityEnabled: true
  mailEnabled: false
}

resource actionGroup 'Microsoft.Graph/groups@v1.0' = {
  uniqueName: '${name}-action-admins'
  displayName: 'mcp-action-admins'
  mailNickname: 'mcp-action-admins'
  securityEnabled: true
  mailEnabled: false
}

// =====================================================================
//  Worker Service Principals
// =====================================================================
resource diagnoseSpApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-diagnose-sp'
  displayName: 'dataops-diagnose-sp'
  signInAudience: 'AzureADMyOrg'
}

resource diagnoseSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: diagnoseSpApp.appId
}

resource actionSpApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-action-sp'
  displayName: 'dataops-action-sp'
  signInAudience: 'AzureADMyOrg'
}

resource actionSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: actionSpApp.appId
}

// Worker SPs intentionally receive NO Azure RBAC here — grant access later.

// =====================================================================
//  Outputs (consumed by write-env.sh to assemble .env)
// =====================================================================
output AZURE_TENANT_ID string = tenant().tenantId
output MCP_APP_ID string = mcpServerApp.appId
output DIAGNOSE_GROUP_ID string = diagnoseGroup.id
output ACTION_GROUP_ID string = actionGroup.id
output DIAGNOSE_SP_CLIENT_ID string = diagnoseSpApp.appId
output ACTION_SP_CLIENT_ID string = actionSpApp.appId
// Secrets are NOT emitted. Run write-env.sh which calls `az ad app credential reset`.
