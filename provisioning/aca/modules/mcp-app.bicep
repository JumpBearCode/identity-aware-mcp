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

var redisUrl = 'redis://${redisHost}:${redisPort}'

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
