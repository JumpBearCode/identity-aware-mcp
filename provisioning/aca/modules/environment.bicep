// =====================================================================
//  Container Apps environment + Log Analytics (RG scope)
// =====================================================================
//
// Shared home for the self-hosted redis Container App and the MCP server
// Container App. Internal apps reach each other over the environment's private
// network (e.g. redis at its internal FQDN).

@description('Resource prefix.')
param name string

@description('Region.')
param location string

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

output environmentId string = env.id
output environmentDefaultDomain string = env.properties.defaultDomain
output workspaceId string = logs.id
