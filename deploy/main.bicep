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
param coreImage string = 'ghcr.io/git4rajat/photostore-backend@sha256:692a7e5c93c0807963b5a810e338d67fa123c0927f856d6f08e53de43acc4d19'

@description('Public web (frontend) image. Defaults to an immutable @sha256 digest, never :latest, same as coreImage above.')
param webImage string = 'ghcr.io/git4rajat/photostore-frontend@sha256:0576e4927e1df85e1cfba21d392c74d44eb4ba67f8a7db68b6845181a0798943'

@description('Public vision (server-side image processing) image. Only pulled/deployed when processingMode is "backend" or "both". Same digest-pinning guidance as coreImage applies.')
param visionImage string = 'ghcr.io/git4rajat/photostore-ipworker@sha256:4f96c81fec8426e17d5bb189048216bd6f9bd5827dfb375322e21ca2284a1b88'

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
param waveCapabilityId string = '0d4d01364100e7016c8ad10bbdad6e3683dfa1ffbbac9adbf3d89c3a55be364b'
@description('Earlier release certificates this release may replace in place (set by the release pin job).')
param waveCompatibleCapabilities string = 'fc135e2712caf71260ebd754871a1534943a06a86194d567d33b846ac6801688,da4a516435a2b6fe8996a153220b2a3ec68f38964d721da18b4e871f2ce05ad8,4f1fa76c93f8654e48ca70e2a58e89fa3799f33cefdc0e5e363c7740cf4026d1,ce9a8419f1b7060ca767c25594fb20b77822ae51c0489d1bcbbee005478f3c50,2d53d9e7877373a890f55cb054fd8b163581c235480a5e0231ff11424a671b7a,1250601265cb502414af0f98378fa244c41e3788589a9fbac7089f5a88f92bda,e78c3f2147cfb7471553d75c51c435238ad2457ce503b7a6db61dc0fec442e99,5a21e2362c9d5bd5333f380418fee4631de8364205481e382389c3ceb1711118'
@description('Source commit of the pinned release (set by the release pin job).')
param waveSourceCommit string = 'c4f6a0ecb504d42d988ea437e8c3e4f15b87cf69'
param waveBootstrapPending bool = false

@description('Provision managed WebSocket notifications when library indexes are published.')
param enableIndexPush bool = true

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
