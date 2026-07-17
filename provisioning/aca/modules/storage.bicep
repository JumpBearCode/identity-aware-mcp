// =====================================================================
//  Storage module (Storage Account + Blob container, RG scope)
// =====================================================================
//
// Holds the per-User / per-Session / per-Conversation workspace tree:
//   mcp-workspaces/{userid}/{sessionid_timestamp}/{conversationid}/...
// Mounted into sandboxes as a BYO blob volume (auth = sandbox-group MI), so
// files written by bash persist while the sandbox itself stays stateless.

@description('Resource prefix.')
param name string

@description('Region.')
param location string

@description('Blob container name for workspaces.')
param containerName string = 'mcp-workspaces'

@description('Log Analytics workspace id — blob data-plane logs go here so injected User-Agent (layer 2) is queryable.')
param workspaceId string = ''

var storageAccountName = take('${toLower(replace(name, '-', ''))}${uniqueString(resourceGroup().id)}', 24)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Ship blob data-plane logs to the workspace so the layer-2 correlation token
// (injected into User-Agent) is queryable in StorageBlobLogs. See docs/oid-log-tracking.
resource blobDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(workspaceId)) {
  name: 'to-dataops-logs'
  scope: blobService
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'StorageRead', enabled: true }
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
  }
}

output storageAccountName string = storage.name
output storageAccountId string = storage.id
output blobContainerName string = containerName
output blobEndpoint string = storage.properties.primaryEndpoints.blob
// ARM id of the container — used as the BYO blob volume's storageContainerResourceId.
output blobContainerResourceId string = container.id
