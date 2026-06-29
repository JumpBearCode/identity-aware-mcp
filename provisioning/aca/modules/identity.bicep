// =====================================================================
//  Identity module (Microsoft Graph, tenant scope)
// =====================================================================
//
// Same Entra footprint as the local path: the MCP server app (delegated
// user_impersonation + OBO admin consent), two AD groups, and the
// diagnose-sp / action-sp worker app registrations.
//
// What's NOT here: the Federated Identity Credentials that let each worker SP
// be assumed by its sandbox-group managed identity. Those need the sandbox-group
// MI principalId, which only exists after sandbox-groups.bicep runs — so they
// live in fic.bicep, wired last.

targetScope = 'tenant'

extension microsoftGraphV1

@description('Display-name / uniqueName prefix.')
param name string

@description('Client ID of VS Code (well-known). Override for a different MCP client.')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

var graphAppId = '00000003-0000-0000-c000-000000000000'

// Delegated Graph permission IDs the MCP server requests.
var graphUserRead = 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
var graphEmail = '64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0'
var graphOfflineAccess = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'
var graphOpenid = '37f7f235-527c-4136-accd-4a02d197296e'
var graphProfile = '14dad69e-099b-42c9-810b-d002981feec1'

var graphOboScope = 'User.Read email offline_access openid profile'
var userImpersonationScopeId = guid('${name}-mcp-server-user_impersonation')

// --- MCP Server app (the OAuth Protected Resource) ---
resource mcpServerApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-mcp-server'
  displayName: 'DataOps MCP Server (ACA)'
  signInAudience: 'AzureADMyOrg'
  identifierUris: [
    'api://${name}-mcp-server'
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

resource graphSp 'Microsoft.Graph/servicePrincipals@v1.0' existing = {
  appId: graphAppId
}

// Tenant-wide OBO admin consent so sign-in needs no per-user consent screen.
resource oboGrant 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: mcpServerSp.id
  consentType: 'AllPrincipals'
  resourceId: graphSp.id
  scope: graphOboScope
}

// --- AD groups that gate tool access ---
resource diagnoseGroup 'Microsoft.Graph/groups@v1.0' = {
  uniqueName: '${name}-diagnose-users'
  displayName: 'mcp-diagnose-users (ACA)'
  mailNickname: '${name}-diagnose-users'
  securityEnabled: true
  mailEnabled: false
}

resource actionGroup 'Microsoft.Graph/groups@v1.0' = {
  uniqueName: '${name}-action-admins'
  displayName: 'mcp-action-admins (ACA)'
  mailNickname: '${name}-action-admins'
  securityEnabled: true
  mailEnabled: false
}

// --- Worker SPs (assumed passwordlessly by their sandbox-group MI via FIC) ---
resource diagnoseSpApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-diagnose-sp'
  displayName: 'dataops-diagnose-sp (ACA)'
  signInAudience: 'AzureADMyOrg'
}

resource diagnoseSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: diagnoseSpApp.appId
}

resource actionSpApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-action-sp'
  displayName: 'dataops-action-sp (ACA)'
  signInAudience: 'AzureADMyOrg'
}

resource actionSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: actionSpApp.appId
}

output mcpAppId string = mcpServerApp.appId
output diagnoseGroupId string = diagnoseGroup.id
output actionGroupId string = actionGroup.id
output diagnoseSpAppId string = diagnoseSpApp.appId
output actionSpAppId string = actionSpApp.appId
// SP object ids — used as the principalId for Azure RBAC role assignments.
output diagnoseSpObjectId string = diagnoseSp.id
output actionSpObjectId string = actionSp.id
