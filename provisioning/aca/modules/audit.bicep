// =====================================================================
//  Audit facility (RG scope) — layer 1 of docs/oid-log-tracking
// =====================================================================
//
// A Data Collection Rule that the MCP server posts per-tool-call audit rows to
// via the Logs Ingestion API. `kind: Direct` gives the DCR its own ingestion
// endpoint, so NO separate Data Collection Endpoint (DCE) is needed. The custom
// table MCPAudit_CL itself is created on the workspace in environment.bicep.
//
// The Monitoring Metrics Publisher role (the role the Logs Ingestion API needs)
// is granted to the MCP app's MI in rbac.bicep, to avoid a module cycle
// (DCR must exist before the role; the MI principalId comes from mcp-app).

@description('Resource prefix.')
param name string

@description('Region.')
param location string

@description('Log Analytics workspace resource id (the audit destination).')
param workspaceId string

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

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${name}-audit-dcr'
  location: location
  kind: 'Direct' // built-in logsIngestion endpoint; no DCE required
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
