// Photostore — one-click deployment entry point (subscription scoped).
//
// This template runs at SUBSCRIPTION scope so it can create the resource group
// itself. The portal "Deploy to Azure" button therefore only asks for a
// subscription + region (and a name) — it never makes the user pick or create
// a resource group first. All actual resources live in resources.bicep, which
// is deployed into the group created here.
//
// Pulls PUBLIC prebuilt container images from ghcr.io — no build step required.
// Authentication is OFF by default, so the app runs immediately.

targetScope = 'subscription'

@description('Base name used as a prefix for resources. Lowercase letters and numbers work best.')
@minLength(3)
@maxLength(17)
param appName string = 'photostore'

@description('Name of the resource group to create for all Photostore resources.')
param resourceGroupName string = '${appName}-rg'

@description('Azure region for the resource group and all resources.')
param location string = deployment().location

@description('Public backend image. Override only if you publish your own fork\'s images.')
param backendImage string = 'ghcr.io/git4rajat/photostore-backend:latest'

@description('Public frontend image. Override only if you publish your own fork\'s images.')
param frontendImage string = 'ghcr.io/git4rajat/photostore-frontend:latest'

// Create the resource group that will hold everything.
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

// Deploy all Photostore resources into the group above.
module app 'resources.bicep' = {
  name: 'photostore-resources'
  scope: rg
  params: {
    appName: appName
    location: location
    backendImage: backendImage
    frontendImage: frontendImage
  }
}

@description('URL of the deployed Photostore web app.')
output appUrl string = app.outputs.appUrl

@description('URL of the backend API.')
output apiUrl string = app.outputs.apiUrl
