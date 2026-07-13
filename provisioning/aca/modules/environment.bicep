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

// Custom table for the MCP per-tool-call audit rows (layer 1; docs/oid-log-tracking).
// The DCR that feeds it lives in modules/audit.bicep.
resource auditTable 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = {
  parent: logs
  name: 'MCPAudit_CL'
  properties: {
    schema: {
      name: 'MCPAudit_CL'
      columns: [
        { name: 'TimeGenerated', type: 'datetime' }
        { name: 'correlation_id', type: 'string' }
        { name: 'tool', type: 'string' }
        { name: 'group', type: 'string' } // note: KQL keyword; query as ['group']
        { name: 'user_oid', type: 'string' }
        { name: 'user_upn', type: 'string' }
        { name: 'client_ip', type: 'string' }
        { name: 'session_id', type: 'string' }
        { name: 'conversation_id', type: 'string' }
        { name: 'command', type: 'string' }
        { name: 'explanation', type: 'string' }
        { name: 'sp_appid', type: 'string' }
        { name: 'exit_code', type: 'int' }
      ]
    }
    retentionInDays: 30
    totalRetentionInDays: 30
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
