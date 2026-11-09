// Upgrade mode lookup: the values a redeploy must keep, read from the resources already deployed under this app
// name. A separate module so it never references a resource this deployment also defines.
targetScope = 'resourceGroup'

param appName string

resource core 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: '${appName}-core'
}
resource email 'Microsoft.Communication/emailServices@2023-04-01' existing = {
  name: '${appName}-email'
}

var env = core.properties.template.containers[0].env
var ownerEmail = filter(env, e => e.name == 'OWNER_EMAIL')
var processing = filter(env, e => e.name == 'PROCESSING_MODE')

output ownerEmail string = empty(ownerEmail) ? '' : ownerEmail[0].?value ?? ''
output processingMode string = empty(processing) ? 'backend' : processing[0].?value ?? 'backend'
output emailDataLocation string = email.properties.dataLocation
