// =====================================================================
//  MCP server Container App (RG scope)
// =====================================================================
//
// Hosts the FastMCP server with a SystemAssigned managed identity. That MI is
// the principal that drives the ACA data plane (granted SandboxGroup Data Owner
// on both groups in rbac.bicep) and reads/writes the workspace blob container.
//
// The image defaults to a public placeholder so the template deploys before the
// real image exists; the deploy script pushes the MCP image to ACR, grants the
// MI AcrPull, then `az containerapp update --image ...`.

@description('Resource prefix.')
param name string

@description('Region.')
param location string

@description('Container Apps environment id.')
param environmentId string

@description('Container Apps environment default domain (for the public FQDN).')
param environmentDefaultDomain string

@description('MCP server container image. Placeholder until the real image is pushed to ACR.')
param mcpImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('MCP server client secret (for OBO). Set during deploy after credential reset.')
@secure()
param mcpClientSecret string = ''

// --- identity / auth ---
param tenantId string
param mcpAppId string
param diagnoseGroupId string
param actionGroupId string

// --- ACA execution context (consumed by SandboxManager) ---
param subscriptionId string
param resourceGroupName string
param acaRegion string
param diagnoseSandboxGroup string
param actionSandboxGroup string
param diagnoseSpAppId string
param actionSpAppId string

// --- redis / storage / blob ---
param redisHost string
param redisPort int
param storageAccount string
param blobContainer string
param blobContainerResourceId string

// --- sandbox image the manager boots microVMs from ---
@description('Container image ref for the sandbox disk image (in ACR). Empty -> manager falls back to the public "ubuntu" disk.')
param sandboxImage string = ''

// --- audit (layer 1; docs/oid-log-tracking). Empty -> server falls back to stdout audit sink. ---
param auditDcrEndpoint string = ''
param auditDcrImmutableId string = ''
param auditStreamName string = 'Custom-MCPAudit_CL'

@description('Redis-backed distributed sandbox lock: "1" on, "0" off. Matches the live default.')
param sandboxDistributedLock string = '0'

var redisUrl = 'redis://${redisHost}:${redisPort}'
// Deterministic public FQDN (matches ingress.fqdn) so the OAuth Protected
// Resource Metadata advertises the real https URL, not localhost.
var publicBaseUrl = 'https://${name}-mcp.${environmentDefaultDomain}'

// Attach the ACR (with the app's system identity) only for a private azurecr.io
// image. The cold-deploy placeholder is public, so this stays empty then and the
// app can bootstrap before AcrPull (granted later in rbac.bicep). For an ACR image
// it must be declared, else a redeploy drops the pull config (observed drift).
var useAcrRegistry = contains(mcpImage, 'azurecr.io')
var acrLoginServer = split(mcpImage, '/')[0]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-mcp'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: useAcrRegistry ? [
        {
          identity: 'system'
          server: acrLoginServer
        }
      ] : []
      secrets: [
        {
          name: 'mcp-client-secret'
          // Container Apps rejects empty secret values; seed a placeholder and
          // overwrite it post-deploy via `az containerapp secret set`.
          value: empty(mcpClientSecret) ? 'placeholder-set-via-secret-set' : mcpClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp-server'
          image: mcpImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'EXECUTOR', value: 'aca' }
            { name: 'MCP_SERVER_BASE_URL', value: publicBaseUrl }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'MCP_APP_ID', value: mcpAppId }
            { name: 'MCP_CLIENT_SECRET', secretRef: 'mcp-client-secret' }
            { name: 'DIAGNOSE_GROUP_ID', value: diagnoseGroupId }
            { name: 'ACTION_GROUP_ID', value: actionGroupId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'ACA_RESOURCE_GROUP', value: resourceGroupName }
            { name: 'ACA_REGION', value: acaRegion }
            { name: 'DIAGNOSE_SANDBOX_GROUP', value: diagnoseSandboxGroup }
            { name: 'ACTION_SANDBOX_GROUP', value: actionSandboxGroup }
            { name: 'DIAGNOSE_SP_APP_ID', value: diagnoseSpAppId }
            { name: 'ACTION_SP_APP_ID', value: actionSpAppId }
            { name: 'REDIS_URL', value: redisUrl }
            { name: 'STORAGE_ACCOUNT', value: storageAccount }
            { name: 'BLOB_CONTAINER', value: blobContainer }
            { name: 'BLOB_CONTAINER_RESOURCE_ID', value: blobContainerResourceId }
            { name: 'SANDBOX_DISK_IMAGE', value: sandboxImage }
            { name: 'SANDBOX_DISTRIBUTED_LOCK', value: sandboxDistributedLock }
            { name: 'AUDIT_DCR_ENDPOINT', value: auditDcrEndpoint }
            { name: 'AUDIT_DCR_RULE_ID', value: auditDcrImmutableId }
            { name: 'AUDIT_STREAM_NAME', value: auditStreamName }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output mcpPrincipalId string = app.identity.principalId
output mcpFqdn string = app.properties.configuration.ingress.fqdn
output mcpAppName string = app.name
