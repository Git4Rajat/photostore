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

@description('Upgrade an existing deployment in place; login email, email data location and processing mode are taken from it.')
param upgradeExisting bool = false
@description('Your login email. Used to sign in and to receive password-reset emails.')
param adminEmail string = ''

@description('Your login password (at least 8 characters). You can change it later inside the app.')
@secure()
param adminPassword string = ''

@description('Where Azure Communication Services stores email data. Pick the option closest to you.')
@allowed([
  'United States'
  'Europe'
  'Australia'
  'United Kingdom'
])
param emailDataLocation string = 'Europe'

@description('Where OCR/face/vision/geo processing runs. "browser": entirely client-side -- no extra cost, works everywhere. "backend" (default): the browser skips this work entirely and a new vision container processes every upload server-side instead -- better for low-power/mobile clients, and enables bulk background reprocessing of an existing library, at the cost of running vision (which needs meaningfully more CPU/memory than the rest of this deployment). "both": the browser and vision both attempt it and whichever finishes first for a given photo wins -- doubles compute cost per step, useful mainly for comparing the two paths.')
@allowed([
  'browser'
  'backend'
  'both'
])
param processingMode string = 'backend'

@description('Public core API image. Defaults to an immutable @sha256 digest (rewritten by the publish workflow after each qualified release), never :latest, so installs never auto-upgrade. To upgrade an EXISTING deployment, override with the new release digest.')
param coreImage string = 'ghcr.io/git4rajat/photostore-backend@sha256:4e10c835d0914d09b9830819707df1f075272fc6fb1666431dcdceb76e559fac'

@description('Public web (frontend) image. Defaults to an immutable @sha256 digest, never :latest, same as coreImage above.')
param webImage string = 'ghcr.io/git4rajat/photostore-frontend@sha256:2edfd567e4dcd6d20d6ecd9580fbe5a034e83c0ef63380463b5c9193a4f174d0'

@description('Public vision (server-side image processing) image. Only pulled/deployed when processingMode is "backend" or "both". Same digest-pinning guidance as coreImage applies.')
param visionImage string = 'ghcr.io/git4rajat/photostore-ipworker@sha256:ba5ff4dd63a78120a950313713982eef96f59403d4b8b306dfef034a61357b3f'

@description('Quota in GiB for the dedicated clustering FAISS checkpoint SMB share. Local SQLite/work files remain on ephemeral storage.')
@minValue(1)
@maxValue(5120)
param clusterFileShareQuotaGiB int = 100

@description('Clustering replica floor. Must remain zero; queued and in-flight work keep the worker active. Checkpoints support recovery after idle scale-down.')
@minValue(0)
@maxValue(0)
param clusterMinReplicas int = 0

@description('Required: use a dedicated index-maintenance consumer. Cluster publishing is no longer supported.')
@allowed([true])
param dedicatedIndexWorker bool = true

@description('Run maintenance in its own queue-scaled app instead of the Tools API sidecar.')
param separateIndexWorker bool = true



@description('Enable qualified wave execution after certificate seeding; fresh installs use deploy/fresh_wave.py.')
param enableWaveExecution bool = true
@description('Enroll newly created empty libraries only. Existing libraries never change ownership.')
param enableNewWaveLibraries bool = true
@description('Exact-image capability ID. Fresh wave activation requires the seeded release certificate; never leave empty.')
param waveCapabilityId string = '78f400065ec0981098cb6f1824a29cb7218c8ee331c9d977908b7a07c16e4732'
@description('Earlier release certificates this release may replace in place (set by the release pin job).')
param waveCompatibleCapabilities string = '78df247682077525618a0354c6e2c0a0c68c4fa49059deccfb58e81d9e78a8a0,1c83f729139f2626ea4801c096b9358c0030354530ceb6b9afbf0695fadbebbe,710b1ab8295761b3e6080d82ac0e50883d1970af82466e14f3599ed116996006,a9a601efeb06a57542fd20763657c7b7b1c998aa186e5d1ffc2e2625227cf274,7bb2fec7c65ed9c17a08571d1289d9e383c8434bce96c899bf24e00366724ddc,85880f3c39543dd1573c7dea5c2ea0f61fe433bee6134111d68d1f913e92e86a,9c4360b4215489d56e32958ab636130a40710fb9757a46b74e39bcaa452a30f2,1f6068c0f15f009a41fe634e0c02e2a594e700c1da16e6717cda82c899cbea5f'
@description('Source commit of the pinned release (set by the release pin job).')
param waveSourceCommit string = '70c0132e08e4ffc012afb03c1576a89c09457168'
param waveBootstrapPending bool = false

@description('Provision managed WebSocket notifications when library indexes are published.')
param enableIndexPush bool = false

@description('Name of an existing Log Analytics workspace (in this resource group) to send Container Apps console/system logs to. Leave blank (the one-click-deploy default) for no log destination. Required on every redeploy of an EXISTING environment that already has this wired up -- the managedEnvironment resource replaces its properties wholesale, so omitting this on a redeploy silently disconnects logging even if it was set up out-of-band or by a previous deploy.')
param logAnalyticsWorkspaceName string = ''

@description('Create a Log Analytics workspace and send Container Apps logs to it (off by default). Ignored when logAnalyticsWorkspaceName names an existing workspace.')
param enableLogAnalytics bool = false

@description('Serve shared albums from a static snapshot on the storage account first (instant first paint while the app wakes). The worker switches the storage static website on by itself the first time a share is created; turn this off to skip that and use the web app URL directly.')
param enableShareSnapshot bool = true

// Create the resource group that will hold everything.
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

// Upgrade mode: read what the existing deployment already has instead of asking again.
module existing 'existing.bicep' = if (upgradeExisting) {
  name: 'photostore-existing'
  scope: rg
  params: {
    appName: appName
  }
}

// Deploy all Photostore resources into the group above.
module app 'resources.bicep' = {
  name: 'photostore-resources'
  scope: rg
  params: {
    appName: appName
    location: location
    upgradeExisting: upgradeExisting
    adminEmail: upgradeExisting ? existing!.outputs.ownerEmail : adminEmail
    adminPassword: adminPassword
    emailDataLocation: upgradeExisting ? any(existing!.outputs.emailDataLocation) : emailDataLocation
    processingMode: upgradeExisting ? any(existing!.outputs.processingMode) : processingMode
    coreImage: coreImage
    webImage: webImage
    visionImage: visionImage
    clusterFileShareQuotaGiB: clusterFileShareQuotaGiB
    clusterMinReplicas: clusterMinReplicas
    dedicatedIndexWorker: dedicatedIndexWorker
    separateIndexWorker: separateIndexWorker
    enableIndexPush: enableIndexPush
    enableWaveExecution: enableWaveExecution
    enableNewWaveLibraries: enableNewWaveLibraries
    waveCapabilityId: waveCapabilityId
    waveCompatibleCapabilities: waveCompatibleCapabilities
    waveSourceCommit: waveSourceCommit
    waveBootstrapPending: waveBootstrapPending
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    enableLogAnalytics: enableLogAnalytics
    enableShareSnapshot: enableShareSnapshot
  }
}

@description('URL of the deployed Photostore web app.')
output appUrl string = app.outputs.appUrl

@description('Address to give users: the static front door when share snapshots are enabled, otherwise the web app URL.')
output homeUrl string = app.outputs.homeUrl

@description('URL of the core API.')
output apiUrl string = app.outputs.apiUrl

@description('URL of the indexer (workbench action-history + derived-index builder) API.')
output indexerUrl string = app.outputs.indexerUrl

@description('URL of the upload API.')
output uploadUrl string = app.outputs.uploadUrl

@description('URL of the recovery (Tools/Workbench recovery actions) API.')
output recoveryUrl string = app.outputs.recoveryUrl

@description('URL of the archive (people/library/public) API.')
output archiveUrl string = app.outputs.archiveUrl
