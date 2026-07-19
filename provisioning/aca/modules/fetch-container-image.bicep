// =====================================================================
//  Fetch the currently-deployed image of an existing Container App
// =====================================================================
//
// `azd deploy` swaps the real image into the MCP Container App out-of-band. On a
// later `azd provision` we must keep that image instead of resetting it to the
// placeholder. Reading it back with an `existing` resource that has the SAME name
// as the app it feeds would make ARM see a self-reference and fail with
// "Circular dependency detected". Isolating the read in its own module breaks that
// cycle (the standard azd container-apps pattern).

param name string
param exists bool

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

output image string = exists ? existingApp!.properties.template.containers[0].image : ''
