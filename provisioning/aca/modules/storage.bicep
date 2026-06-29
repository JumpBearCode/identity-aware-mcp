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

output storageAccountName string = storage.name
output storageAccountId string = storage.id
output blobContainerName string = containerName
output blobEndpoint string = storage.properties.primaryEndpoints.blob
// ARM id of the container — used as the BYO blob volume's storageContainerResourceId.
output blobContainerResourceId string = container.id
