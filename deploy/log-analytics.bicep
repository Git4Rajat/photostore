@description('Log Analytics workspace name.')
param workspaceName string

param location string = resourceGroup().location

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

output workspaceName string = workspace.name
output workspaceId string = workspace.id
