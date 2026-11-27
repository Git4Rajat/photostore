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
param coreImage string = 'ghcr.io/git4rajat/photostore-backend@sha256:679efe1069d499f01e3b83aff45e988138783fa9b7c3fe960d75e9c47681c91d'

@description('Public web (frontend) image. Defaults to an immutable @sha256 digest, never :latest, same as coreImage above.')
param webImage string = 'ghcr.io/git4rajat/photostore-frontend@sha256:971b5a93c637d5696b4ce5233577852ccf22645077ccdf21cc1dced40c2498a1'

@description('Public vision (server-side image processing) image. Only pulled/deployed when processingMode is "backend" or "both". Same digest-pinning guidance as coreImage applies.')
param visionImage string = 'ghcr.io/git4rajat/photostore-ipworker@sha256:492c1b91e6b4f61dd950d81805adab4f7ef1fad2d73a76e990137f221c6e79d4'

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



@description('Upload app: concurrent HTTP requests per replica before another replica starts. Live QA value after the 2026-11-22 portal change.')
@minValue(1)
@maxValue(256)
param uploadConcurrentRequests int = 32

@description('Upload app: maximum replicas. Live QA value after the 2026-11-22 portal change.')
@minValue(1)
@maxValue(30)
param uploadMaxReplicas int = 2

@description('Vision: intra-op threads per model call (IPWORK_MODEL_THREADS), also the OMP/OpenBLAS thread caps. Keep 1: measured 2026-11-26 at 2 vCPU with IPWORKER_CONCURRENCY=5, 2 threads cost about 22% of files per hour and 40% more CPU per file, because the cores are already about 84% busy across the concurrent source threads. Raise only together with more vCPU, after a live test.')
@minValue(1)
@maxValue(16)
param visionModelThreads int = 1

@description('Enable qualified wave execution after certificate seeding; fresh installs use deploy/fresh_wave.py.')
param enableWaveExecution bool = true
@description('Enroll newly created empty libraries only. Existing libraries never change ownership.')
param enableNewWaveLibraries bool = true
@description('Exact-image capability ID. Fresh wave activation requires the seeded release certificate; never leave empty.')
param waveCapabilityId string = '7cba50a69ee4630bde1ca6ff2884648d24945512544f1df76fc0b2f2ce9bfcb2'
@description('Earlier release certificates this release may replace in place (set by the release pin job).')
param waveCompatibleCapabilities string = '422055bd4b7e8a918342c255580baf369a3b32421c8b7444c7349cc0bd13ddf6,b111e2bfff409a536071bdbc89f0321c9249743c0dc4486e0009b1a0da6bba3a,b24701464e8a754d7a09b87e42f8f5a6f3e8e5504c727fdc97e370626b224006,d715bd1dfd346d8758cc94e858b187967f28ff1f69c048c5fa520293f07341ba,cbe86c3edb95a7ae4cf253c19ad8cecc6d3fbcc1bc022556606c4e9d0f2c27d5,e06541a00d63da1033a5eeaa163ad01f8c96436c4c63566477731ce580397b42,46a97224e1c89ab36103a0e450265e648118d4e9d6e8bc26682305ea50aa4458,4ad5fc0c0d53c75c063868846e002a900962745edaad0eae13beb05f001d4ccd'
@description('Source commit of the pinned release (set by the release pin job).')
param waveSourceCommit string = '50ed41c41324ea32884c90d093923c4e4815bde8'
param waveBootstrapPending bool = false
param semanticTextEmbeddingEnabled bool = false

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
    uploadConcurrentRequests: uploadConcurrentRequests
    visionModelThreads: visionModelThreads
    uploadMaxReplicas: uploadMaxReplicas
    enableWaveExecution: enableWaveExecution
    enableNewWaveLibraries: enableNewWaveLibraries
    waveCapabilityId: waveCapabilityId
    waveCompatibleCapabilities: waveCompatibleCapabilities
    waveSourceCommit: waveSourceCommit
    waveBootstrapPending: waveBootstrapPending
    semanticTextEmbeddingEnabled: semanticTextEmbeddingEnabled
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
