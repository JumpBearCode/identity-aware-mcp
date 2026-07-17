// =====================================================================
//  MCP observability (RG scope) — Layer 1 authoritative audit infrastructure
// =====================================================================
//
// Infrastructure for LAYER 1 of the identity-aware attribution design
// (docs/oid-log-tracking): the authoritative MCPAudit_CL table that holds one row
// per MCP tool call (real user / real IP / command / correlation id), plus the
// Direct Data Collection Rule the MCP server posts those rows to via the Logs
// Ingestion API. `kind: Direct` gives the DCR its own ingestion endpoint, so NO
// separate Data Collection Endpoint (DCE) is needed.
//
// The table lives on the shared Log Analytics workspace created in
// environment.bicep (referenced here as existing). The Monitoring Metrics
// Publisher role the Logs Ingestion API requires is granted to the MCP app's MI in
// rbac.bicep — kept there to avoid a module cycle (the DCR must exist before the
// role; the MI principalId comes from mcp-app).

@description('Resource prefix.')
param name string

@description('Region.')
param location string

@description('Log Analytics workspace resource id (holds the table; also the DCR destination).')
param workspaceId string

var streamName = 'Custom-MCPAudit_CL'
// Single source of truth for the schema, shared by the table and the DCR stream.
var columns = [
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

// The shared workspace (created in environment.bicep) — parent of the audit table.
resource ws 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: last(split(workspaceId, '/'))
}

// Layer 1 authoritative table: one row per MCP tool call.
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
  name: '${name}-audit-dcr'
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
          workspaceResourceId: workspaceId
        }
      ]
    }
    dataFlows: [
      {
        streams: [ streamName ]
        destinations: [ 'auditWs' ]
        outputStream: streamName // land in the custom table as-is
      }
    ]
  }
}

output dcrName string = dcr.name
output dcrImmutableId string = dcr.properties.immutableId
output dcrEndpoint string = dcr.properties.endpoints.logsIngestion
output streamName string = streamName
