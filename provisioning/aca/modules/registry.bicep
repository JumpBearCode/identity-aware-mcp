// =====================================================================
//  Container registry module (ACR, RG scope)
// =====================================================================
//
// Holds two images:
//   - the MCP server image the Container App runs;
//   - the sandbox image the SandboxManager turns into a disk image and boots
//     each microVM from (az + python + jq + FIC bootstrap).
// Admin user is enabled so the deploy script can docker login / push simply.

@description('Resource prefix.')
param name string

@description('Region.')
param location string

var registryName = take('${toLower(replace(name, '-', ''))}acr${uniqueString(resourceGroup().id)}', 50)

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  // Standard enables anonymous pull, which the ADC disk-image build needs to
  // pull the sandbox image without credentials (it does not use the group MI's
  // AcrPull). The image is just az + jq + bootstrap, so public pull is fine; for
  // a private registry instead, pass registryCredentials to create_disk_image.
  sku: {
    name: 'Standard'
  }
  properties: {
    adminUserEnabled: true
    anonymousPullEnabled: true
  }
}

output registryName string = acr.name
output registryId string = acr.id
output loginServer string = acr.properties.loginServer
