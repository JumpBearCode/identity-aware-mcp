// =====================================================================
//  Targeted deploy of JUST the audit facility onto the already-live stack.
// =====================================================================
//
// Safe by construction: references the EXISTING workspace and adds only three
// things — the MCPAudit_CL custom table, a Direct DCR (its own ingestion
// endpoint, so NO DCE), and the Monitoring Metrics Publisher role for the MCP
// app's managed identity. It does NOT touch identity / FIC / sandbox groups /
// the container image, so it can't disturb the running stack.
//
// The durable equivalent is main.bicep + modules/{environment,audit,rbac}.bicep;
// this file just lets us apply the audit pieces without a full sub deployment.
//
//   az deployment group create -g dataops-aca-rg -n audit-standalone \
//     -f audit-standalone.bicep --parameters mcpPrincipalId=<mcp MI principalId>

targetScope = 'resourceGroup'

@description('Existing Log Analytics workspace name.')
param workspaceName string = 'dataops-aca-logs'

@description('DCR region.')
param location string = 'westus2'

@description('DCR name.')
param dcrName string = 'dataops-aca-audit-dcr'

@description('MCP container app system-assigned MI principalId (gets Monitoring Metrics Publisher on the DCR).')
param mcpPrincipalId string

var streamName = 'Custom-MCPAudit_CL'
var columns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'correlation_id', type: 'string' }
  { name: 'tool', type: 'string' }
  { name: 'group', type: 'string' }
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
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource ws 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource auditTable 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = {
  parent: ws
  name: 'MCPAudit_CL'
  properties: {
    schema: {
      name: 'MCPAudit_CL'
      columns: columns
    }
    retentionInDays: 30
    totalRetentionInDays: 30
  }
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: dcrName
  location: location
  kind: 'Direct' // built-in logsIngestion endpoint; no DCE required
  dependsOn: [ auditTable ] // the output table must exist before the DCR references it
  properties: {
    streamDeclarations: {
      '${streamName}': {
        columns: columns
      }
    }
    destinations: {
      logAnalytics: [
        {
          name: 'auditWs'
          workspaceResourceId: ws.id
        }
      ]
    }
    dataFlows: [
      {
        streams: [ streamName ]
        destinations: [ 'auditWs' ]
        outputStream: streamName
      }
    ]
  }
}

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dcr.id, mcpPrincipalId, monitoringMetricsPublisherRoleId)
  scope: dcr
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalType: 'ServicePrincipal'
  }
}

output dcrEndpoint string = dcr.properties.endpoints.logsIngestion
output dcrImmutableId string = dcr.properties.immutableId
output streamName string = streamName
