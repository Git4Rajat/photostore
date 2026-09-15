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

@description('Your login email. Used to sign in and to receive password-reset emails.')
param adminEmail string

@description('Your login password (at least 8 characters). You can change it later inside the app.')
@minLength(8)
@secure()
param adminPassword string

@description('Where Azure Communication Services stores email data. Pick the option closest to you.')
@allowed([
  'United States'
  'Europe'
  'Australia'
  'United Kingdom'
])
param emailDataLocation string = 'United States'

@description('Where OCR/face/vision/geo processing runs. "browser" (default): entirely client-side, same as today -- no extra cost, works everywhere. "backend": the browser skips this work entirely and a new ipworker container processes every upload server-side instead -- better for low-power/mobile clients, and enables bulk background reprocessing of an existing library, at the cost of running ipworker (which needs meaningfully more CPU/memory than the rest of this deployment). "both": the browser and ipworker both attempt it and whichever finishes first for a given photo wins -- doubles compute cost per step, useful mainly for comparing the two paths.')
@allowed([
  'browser'
  'backend'
  'both'
])
param processingMode string = 'browser'

@description('Public backend image. Defaults to :latest for one-click "Deploy to Azure" installs. When upgrading an EXISTING deployment, override with the immutable date-time tag from the publish workflow run (e.g. :20260806-153045) instead of :latest, so scale-to-zero cold-start restarts keep pulling the exact image you tested rather than whatever :latest has drifted to.')
param backendImage string = 'ghcr.io/git4rajat/photostore-backend:latest'

@description('Public frontend image. Defaults to :latest for one-click "Deploy to Azure" installs. When upgrading an EXISTING deployment, override with the immutable date-time tag from the publish workflow run instead of :latest, for the same reason as backendImage above.')
param frontendImage string = 'ghcr.io/git4rajat/photostore-frontend:latest'

@description('Public ipworker image. Only pulled/deployed when processingMode is "backend" or "both". Same :latest-vs-pinned-tag guidance as backendImage applies when upgrading an existing deployment.')
param ipworkerImage string = 'ghcr.io/git4rajat/photostore-ipworker:latest'

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
    adminEmail: adminEmail
    adminPassword: adminPassword
    emailDataLocation: emailDataLocation
    processingMode: processingMode
    backendImage: backendImage
    frontendImage: frontendImage
    ipworkerImage: ipworkerImage
  }
}

@description('URL of the deployed Photostore web app.')
output appUrl string = app.outputs.appUrl

@description('URL of the backend API.')
output apiUrl string = app.outputs.apiUrl
