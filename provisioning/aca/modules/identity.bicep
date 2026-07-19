// =====================================================================
//  Identity module (Microsoft Graph, tenant scope)
// =====================================================================
//
// Same Entra footprint as the local path: the MCP server app (delegated
// user_impersonation + OBO admin consent), two AD groups, and the
// diagnose-sp / action-sp worker app registrations.
//
// Also includes the Federated Identity Credentials (worker SP <- sandbox-group MI).
// They need the sandbox-group MI principalId (passed in as a param from
// sandbox-groups.bicep) and are co-located with the worker SP apps HERE on purpose:
// a standalone module referencing the SPs via `existing` hit a Graph preflight race
// (validated before the SPs were created) that failed the first deploy —
// see docs/{en,zh}/azd-migration deployment-gotchas §1.

// RG-scoped (not tenant): the Microsoft.Graph extension routes apps/groups/
// grants to Graph regardless of the ARM deployment scope, and an RG-scoped
// module only needs RG-write — tenant-scoped modules would require
// Microsoft.Resources/deployments/write at the tenant root, which nobody has by
// default. The Graph objects created are still tenant-wide.
targetScope = 'resourceGroup'

extension microsoftGraphV1

@description('Display-name / uniqueName prefix.')
param name string

@description('Client ID of VS Code (well-known). Override for a different MCP client.')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

@description('Loopback redirect URIs for the shared CLI public client (Claude Code / opencode), RFC 8252.')
param cliClientRedirectUris array = [
  'http://localhost:8080/callback'
]

@description('Diagnose sandbox-group MI principalId (FIC subject). From sandbox-groups.bicep.')
param diagnoseMiPrincipalId string

@description('Action sandbox-group MI principalId (FIC subject). From sandbox-groups.bicep.')
param actionMiPrincipalId string

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
  // The Application ID URI clients use to "name" this API when requesting a
  // token. Bicep can't reference this app's own generated appId at create time,
  // so we declare a static friendly URI and make the server advertise THIS as its
  // OAuth scope prefix (mcpIdentifierUri output -> MCP_IDENTIFIER_URI env ->
  // AzureJWTVerifier / mcpproxy). Bicep is the single source of truth; no
  // post-deploy `az ad app update --identifier-uris api://<appId>` patch is needed
  // (that overwrite was the AADSTS500011 drift — see docs Bug剖析-...500011).
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
    // Only VS Code is pre-authorized here. The CLI client is deliberately NOT added:
    // mcpServerApp.preAuth -> cliClientApp.appId while cliClientApp.requiredResourceAccess
    // -> mcpServerApp.appId is a reference cycle (same class as the mcp-app circular dep).
    // The CLI client instead relies on standard first-sign-in user consent — the tenant
    // allows user consent (authorizationPolicy). Being validated; see deployment-gotchas §4.
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

// --- Shared CLI public client (Claude Code / opencode) ---
// Public client: PKCE, no secret, loopback redirect (RFC 8252). Requests the MCP
// server's user_impersonation delegated scope; consent is granted per-user (or by
// an admin) at first sign-in since it is not pre-authorized above.
// KNOWN LIMITATION (2026-07): the interactive OAuth flow from Claude Code / opencode
// is currently blocked by AADSTS9010010 (RFC 8707 `resource` vs Entra v2 `scope`).
// The app registration itself is valid IaC; see
// docs/multi-client-implementation/Bug剖析-AADSTS9010010-MCP的resource参数撞上Entra-v2.md.
resource cliClientApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-cli-client'
  displayName: 'DataOps MCP - CLI Client (shared)'
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: true
  publicClient: {
    redirectUris: cliClientRedirectUris
  }
  requiredResourceAccess: [
    {
      resourceAppId: mcpServerApp.appId
      resourceAccess: [
        { id: userImpersonationScopeId, type: 'Scope' }
      ]
    }
  ]
}

resource cliClientSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: cliClientApp.appId
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

// --- Federated Identity Credentials: each worker SP trusts its sandbox-group MI ---
// Co-located with the worker SP apps above (same module / compilation unit) so there
// is NO cross-module `existing` reference — the standalone fic.bicep used `existing`
// to find these SPs and ARM validated it at preflight, before the SPs were created,
// which failed the first deploy (deployment-gotchas §1). Inside a sandbox the group
// MI token (aud api://AzureADTokenExchange) is exchanged for the worker SP via
// `az login --federated-token` — no secret anywhere in the cloud.
var ficIssuer = 'https://login.microsoftonline.com/${tenant().tenantId}/v2.0'
var ficAudiences = [ 'api://AzureADTokenExchange' ]

resource diagnoseFic 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${diagnoseSpApp.uniqueName}/diagnose-sandbox-mi'
  description: 'Trust the diagnose sandbox group managed identity'
  issuer: ficIssuer
  subject: diagnoseMiPrincipalId
  audiences: ficAudiences
}

resource actionFic 'Microsoft.Graph/applications/federatedIdentityCredentials@v1.0' = {
  name: '${actionSpApp.uniqueName}/action-sandbox-mi'
  description: 'Trust the action sandbox group managed identity'
  issuer: ficIssuer
  subject: actionMiPrincipalId
  audiences: ficAudiences
}

output mcpAppId string = mcpServerApp.appId
// Application ID URI the server advertises as its OAuth scope prefix
// (MCP_IDENTIFIER_URI). Clients request <this>/user_impersonation; the token's
// aud is still the appId GUID. Matches identifierUris above — the friendly-name
// source of truth that removes the AADSTS500011 post-deploy patch.
output mcpIdentifierUri string = 'api://${name}-mcp-server'
// Shared CLI public client appId — put into .mcp.json (Claude Code) and opencode.json.
output cliClientAppId string = cliClientApp.appId
output diagnoseGroupId string = diagnoseGroup.id
output actionGroupId string = actionGroup.id
output diagnoseSpAppId string = diagnoseSpApp.appId
output actionSpAppId string = actionSpApp.appId
// SP object ids — used as the principalId for Azure RBAC role assignments.
output diagnoseSpObjectId string = diagnoseSp.id
output actionSpObjectId string = actionSp.id
