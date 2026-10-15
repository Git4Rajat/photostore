// Separate entry point: create LAW before the existing resource module uses it.
targetScope = 'subscription'

@minLength(3)
@maxLength(17)
param appName string = 'photostore'

param resourceGroupName string = '${appName}-rg'
param location string = deployment().location
param adminEmail string

param emailDataLocation string = 'Europe'
param browserUploadPreparation bool = true

@minLength(8)
@secure()
param adminPassword string

// Require immutable images for a branch deployment rather than updating latest.
param coreImage string
param webImage string
param visionImage string

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

module logs 'log-analytics.bicep' = {
  name: '${appName}-logging'
  scope: rg
  params: {
    workspaceName: '${appName}-law'
    location: location
  }
}

module app 'resources.bicep' = {
  name: '${appName}-resources'
  scope: rg
  params: {
    appName: appName
    communicationServiceName: '${appName}-comm-${uniqueString(rg.id)}'
    location: location
    adminEmail: adminEmail
    emailDataLocation: emailDataLocation
    browserUploadPreparation: browserUploadPreparation
    adminPassword: adminPassword
    processingMode: 'backend'
    coreImage: coreImage
    webImage: webImage
    visionImage: visionImage
    logAnalyticsWorkspaceName: logs.outputs.workspaceName
  }
}

output appUrl string = app.outputs.appUrl
output apiUrl string = app.outputs.apiUrl
output uploadUrl string = app.outputs.uploadUrl
output logAnalyticsWorkspaceId string = logs.outputs.workspaceId
