// Declarative provisioning for the identity-aware DataOps MCP stack.
//
// Uses the Microsoft.Graph Bicep extension (public preview as of 2026) for
// app registrations, service principals, and security groups.
//
// What this file does NOT do:
//   - Create client secrets   (Bicep can't safely emit secrets — use write-env.sh after deploy)
//   - Grant admin consent     (do it once in the Entra portal, or via `az ad app permission admin-consent`)
//   - Add users to groups     (use `az ad group member add` per user)
//
// targetScope must be subscription so we can create role assignments at sub level.

targetScope = 'subscription'

extension microsoftGraphV1

@description('Display-name prefix for created resources (e.g. "dataops-mcp")')
param name string = 'dataops-mcp'

@description('Scope where the worker SPs receive RBAC. Default: the entire subscription.')
param targetScope_ string = subscription().id

@description('Client ID of VS Code (well-known). Override for tenants using a different MCP client.')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

// --- Well-known role IDs ---
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

// --- Well-known IDs for Microsoft Graph ---
var graphAppId = '00000003-0000-0000-c000-000000000000'
var graphUserRead = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d' // delegated User.Read

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

// =====================================================================
//  RBAC at targetScope_
// =====================================================================
module rbac './rbac.bicep' = {
  name: 'rbac'
  scope: subscription()
  params: {
    diagnoseSpObjectId: diagnoseSp.id
    actionSpObjectId: actionSp.id
    readerRoleId: readerRoleId
    contributorRoleId: contributorRoleId
    scope_: targetScope_
  }
}

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
