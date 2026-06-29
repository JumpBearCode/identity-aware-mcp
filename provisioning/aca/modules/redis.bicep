// =====================================================================
//  Redis module (self-hosted Container App, RG scope)
// =====================================================================
//
// A redis:7-alpine Container App with internal-only TCP ingress on 6379.
// We only store the session->sandbox map (30-min TTL) and a user profile
// cache; both self-heal if lost, so no persistence / HA is needed — this is
// markedly cheaper than Azure Cache for Redis. See §4.1 of the migration doc.
//
// Constraint: redis must NOT scale to zero (connections would drop and data
// would vanish), so minReplicas = maxReplicas = 1. It is resident but cheap.

@description('Resource prefix.')
param name string

@description('Region.')
param location string

@description('Container Apps environment id.')
param environmentId string

resource redis 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-redis'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 6379
        exposedPort: 6379
      }
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: 'redis:7-alpine'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output redisHost string = redis.properties.configuration.ingress.fqdn
output redisPort int = 6379
