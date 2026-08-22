// Photostore — resource module (resource-group scoped).
//
// Provisions everything needed to run Photostore, pulling PUBLIC prebuilt
// container images from GitHub Container Registry (ghcr.io). No build step, no
// private registry, no CLI required. This module is invoked by main.bicep,
// which creates the resource group at subscription scope and deploys this into
// it — so the "Deploy to Azure" button never asks the user to pick a group.
//
// Deploys (into the resource group created by main.bicep):
//   - a Storage account with blob containers (images/thumbnails/covers);
//     metadata tables are created automatically by the app on first use
//   - a Container Apps environment (+ Log Analytics workspace)
//   - backend, frontend, and worker container apps
//   - role assignments so the apps reach storage via managed identity
//
// Sign-in uses a single owner email + password that you set right here in the
// deploy form. Password recovery is emailed via Azure Communication Services
// (provisioned below, no DNS setup required).

@description('Base name used as a prefix for resources. Lowercase letters and numbers work best.')
@minLength(3)
@maxLength(17)
param appName string = 'photostore'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

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

@description('Public ipworker image. Only pulled/deployed when processingMode is "backend" or "both". Separate from backendImage (unlike the clustering `worker` role, which reuses it) because ipworker needs torch/open_clip/onnxruntime/opencv/mediapipe/tesseract -- multiple GB of extra weight that would slow every backend/worker cold start if bundled into their shared image. Same :latest-vs-pinned-tag guidance as backendImage applies when upgrading an existing deployment.')
param ipworkerImage string = 'ghcr.io/git4rajat/photostore-ipworker:latest'

@description('Secret used to sign login sessions. Leave blank to auto-generate a strong random value at deploy time.')
@secure()
param sessionSecretParam string = '${newGuid()}${newGuid()}'

var suffix = uniqueString(resourceGroup().id)
// Secret used to sign login sessions. High-entropy random value (two GUIDs,
// ~244 bits) generated once at deploy time — NOT derived from uniqueString,
// which is deterministic and predictable from public resource metadata.
var sessionSecret = sessionSecretParam
var storageAccountName = take(toLower(replace('${appName}${suffix}', '-', '')), 24)

var environmentName = '${appName}-env'
var backendAppName = '${appName}-backend'
var frontendAppName = '${appName}-frontend'
var workerAppName = '${appName}-worker'
var ipworkerAppName = '${appName}-ipworker'
// Only deploy ipworker (and grant it storage access) when the deployment
// actually needs it -- in 'browser' mode (the default) it would just sit
// scaled to zero forever, so skip provisioning it at all.
var deployIpworker = processingMode != 'browser'

// Built-in role definition IDs for storage data-plane access.
var roleBlobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleTableContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var roleQueueContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var roleBlobDelegator = 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
var storageRoleIds = [
  roleBlobContributor
  roleTableContributor
  roleQueueContributor
  roleBlobDelegator
]

// Frontend URL is predicted from the environment's default domain so the
// backend can reference it without a circular dependency.
var frontendUrl = 'https://${frontendAppName}.${managedEnvironment.properties.defaultDomain}'

// ---------------------------------------------------------------------------
// Azure Communication Services — email for password recovery.
// Uses an Azure-managed sender domain (donotreply@<generated>.azurecomm.net),
// so there is NO DNS to configure. The backend sends via the connection string.
// ---------------------------------------------------------------------------
resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: '${appName}-email'
  location: 'global'
  properties: {
    dataLocation: emailDataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: '${appName}-comm'
  location: 'global'
  properties: {
    dataLocation: emailDataLocation
    linkedDomains: [ emailDomain.id ]
  }
}

// Sender address on the auto-provisioned managed domain.
var acsSenderAddress = 'DoNotReply@${emailDomain.properties.fromSenderDomain}'
var acsConnectionString = communicationService.listKeys().primaryConnectionString

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    // The app authenticates to storage exclusively via managed identity and
    // user-delegation SAS, so account/shared keys are never needed. Disabling
    // them removes an entire class of credential-leak risk.
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // The browser uploads files directly to blob storage via a SAS URL, so the
    // blob endpoint must allow cross-origin PUT/OPTIONS from the frontend only.
    cors: {
      corsRules: [
        {
          allowedOrigins: [ frontendUrl ]
          allowedMethods: [ 'GET', 'HEAD', 'PUT', 'OPTIONS', 'POST' ]
          allowedHeaders: [ '*' ]
          exposedHeaders: [ '*' ]
          maxAgeInSeconds: 3600
        }
      ]
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in ['images', 'thumbnails', 'covers']: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {}
}

// Shared backend/worker environment variables.
var backendEnv = [
  { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'USE_MANAGED_IDENTITY', value: 'true' }
  // 'sas': browsers fetch media straight from blob storage via short-lived
  // read SAS URLs. Proxying media bytes through the backend was the largest
  // compute cost in the deployment.
  { name: 'MEDIA_URL_MODE', value: 'sas' }
  { name: 'MAX_UPLOAD_FILE_BYTES', value: '5368709120' }
  { name: 'BLOB_IMAGE_CONTAINER', value: 'images' }
  { name: 'BLOB_THUMBNAIL_CONTAINER', value: 'thumbnails' }
  { name: 'BLOB_COVER_CONTAINER', value: 'covers' }
  { name: 'METADATA_TABLE', value: 'photometadata' }
  { name: 'ALBUMS_TABLE', value: 'photoalbums' }
  { name: 'PUBLIC_ALBUM_ATTEMPTS_TABLE', value: 'publicalbumattempts' }
  { name: 'ALLOWED_ORIGINS', value: '*' }
  { name: 'AUTH_REQUIRED', value: 'true' }
  { name: 'AUTH_MODE', value: 'password' }
  { name: 'OWNER_EMAIL', value: adminEmail }
  { name: 'ACS_SENDER_ADDRESS', value: acsSenderAddress }
  { name: 'PUBLIC_APP_BASE_URL', value: frontendUrl }
  { name: 'SPA_BASE_URL', value: frontendUrl }
  { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
  // BROWSER_ONLY_PROCESSING no longer exists as its own setting -- app.py
  // derives it from PROCESSING_MODE (browser-only iff PROCESSING_MODE ==
  // 'browser') so the two can't drift out of sync the way a second
  // hand-edited literal here eventually would.
  { name: 'PROCESSING_MODE', value: processingMode }
  // v3 drops the 128-d face-api descriptor concatenated onto ArcFace's 512-d
  // output (it diluted ArcFace's signal) and 5-point-landmark-aligns the crop
  // fed to ArcFace instead of using a plain padded box. v2 stays in the
  // backend's allowed-versions set (FACE_CLUSTER_LEGACY_HYBRID_VERSION in
  // backend/app.py) so already-processed v2 faces keep clustering fine while
  // the library re-embeds via the self-healing stale-version re-embed.
  // -guarded (2026-07-31): same v3 pipeline, but cropFaceCanvas now rejects a
  // geometrically-implausible 5-point solve and falls back instead of
  // trusting it, plus each face is tagged with which alignment tier it got
  // (alignmentMethod). Old v3 stays allowed
  // (FACE_CLUSTER_LEGACY_ALIGNED_V3_VERSION) so it keeps clustering during
  // the transition.
  // -diag (2026-07-31, same day): real-world testing found alignmentMethod
  // ='none' on 100% of faces post-guard, with zero visibility into why,
  // because detectFaceLandmarks silently swallowed its own errors. Removed
  // that catch and added alignmentFailureReason to capture the real cause.
  // -fixed (2026-07-31, same day): -diag's captured error was
  // "faceapi_model_load_failed: No backend found in registry" --
  // loadFaceApiSession called tf.setBackend('cpu') without ever importing
  // '@tensorflow/tfjs-backend-cpu' (tfjs backends only self-register via that
  // import's side effect), silently relying on an unrelated init path (the
  // browser-AI tagging feature) to have already done it. Fixed by importing
  // it directly in loadFaceApiSession.
  // -fixed2 (2026-07-31, same day): -fixed got past that error but hit a new
  // one one layer deeper, again via alignmentFailureReason: "e.toFloat is
  // not a function". face-api.js's bundled code (built against
  // tfjs-core@1.7.0) calls legacy convenience cast methods removed from this
  // app's deduped tfjs-core@4.22.0 (only .cast(dtype) remains). Added a
  // one-time compat shim restoring toFloat/toInt/toBool as thin .cast()
  // wrappers (faceApiRuntime.ts).
  // -fixed3 (2026-07-31, same day): -fixed2's shim only covered casts; the
  // very next call in the same chain hit "e.as4D is not a function". Checked
  // directly: tfjs-core@4.22.0 removed essentially ALL chainable Tensor op
  // methods (257 of them), not just casts, in favor of top-level functional
  // calls. faceApiRuntime.ts now generically restores every tf.<op> as an
  // instance method forwarding to its top-level call, plus explicit mappings
  // for the few with no same-named equivalent (as1D..as5D -> tf.reshape,
  // resizeBilinear -> tf.image.resizeBilinear).
  // -fixed4 (2026-07-31, same day): -fixed3 was verified against a real
  // production crop before shipping, yet the real deploy still hit a 3rd
  // error: "Size(136) must match the product of shape" (an empty shape).
  // Isolated directly: as1D() is called with ZERO args in legacy usage
  // ("flatten to 1D, infer size"), unlike as2D..as5D which always take
  // explicit dims -- the generic shim forwarded the empty args array to
  // tf.reshape(this, []), targeting a scalar instead. Fixed by special-
  // casing as1D to reshape to [this.size].
  // -fixed5 (2026-07-31, same day): -fixed4 finally got real landmarks
  // end-to-end (no more crashes), but 24/25 faces landed on
  // alignmentMethod='landmark-2pt' rather than 'landmark-5pt'. The
  // ARC_FACE_MIN_SCALE/MAX_SCALE guard bounds (PhotoGallery.tsx) were
  // copied from the old 2-point path's different target-eye-distance
  // convention -- for the 5-point path's actual crop convention (padded
  // full face bbox, not tight eye-only), a geometrically correct solve
  // legitimately scores 0.08-0.25, so the guard rejected nearly every real
  // transform. Confirmed directly against 6 real production faces and
  // recalibrated to 0.03-0.6.
  // -fixed6 (2026-07-31, same day): real cross-photo testing on confirmed-
  // same-person faces showed the 2-point eye-only fallback actively hurts
  // matches -- mixing it with 5-point-aligned embeddings in the same
  // clustering pool scored near-zero similarity for genuinely identical
  // people, purely from the alignment-tier mismatch. Removed the 2-point
  // fallback from the browser pipeline entirely; backend
  // _face_embedding_allowed_for_clustering now also requires
  // alignmentMethod=='landmark-5pt'. One embedding quality tier in the
  // matching pool.
  // -fixed7 (2026-07-31, same day): -fixed6's guard (ARC_FACE_MAX_SCALE=0.6)
  // was too tight -- a confirmed-real, downward-tilted face measured 0.68
  // and was wrongly rejected. Raised to 0.9. REVERTED in -fixed8 below.
  // -fixed8 (2026-07-31, same day): -fixed7 was reverted. Real ArcFace
  // embedding testing (actual model inference via onnxruntime-node locally,
  // not just checking the transform's numbers look plausible) proved
  // 5-point alignment for that same downward-tilt case scored only
  // 0.19-0.28 same-person similarity -- WORSE than a plain unaligned crop
  // (0.56-0.68) or the 2-point eyes-only fallback (0.51-0.55). Forcing a
  // severely pitched face into a frontal template distorts it more than
  // leaving it alone. MAX_SCALE reverted to 0.6, and the 2-point fallback
  // (PhotoGallery.tsx cropFaceCanvas) is restored as a genuinely separate
  // quality tier, not a last resort. Backend
  // _face_embedding_allowed_for_clustering now accepts both landmark-5pt
  // and landmark-2pt; _build_people_recluster_plan clusters each tier in
  // its own DBSCAN pass with its own epsilon (PEOPLE_CLUSTER_EPS_2PT=0.60
  // for 2pt) and never compares the two tiers directly against each other
  // (cross-tier same-person comparisons measured 0.09-0.56 --
  // indistinguishable from noise). Still-open TODO: visually validate
  // ARC_FACE_5POINT_TEMPLATE's target coordinates against real aligned
  // crops.
  // -adaface1 (2026-07-31): swapped the embedding model (ArcFace resnet100
  // -> AdaFace IR-101/WebFace4M, MIT license). Even -fixed8's tier-aware
  // clustering can't fix a case where the pose gap is real, not an
  // alignment artifact: a confirmed same-person pair at a genuine
  // head-turn scored only 0.29 on ArcFace's best (5-point) tier.
  // Head-to-head on identical crops (onnxruntime-node harness): AdaFace
  // scored 0.68 on that pair vs ArcFace's 0.31, 0.82-0.85 on a
  // moderate-pose trio vs 0.56-0.67, while an easy frontal pair stayed
  // near ceiling for both (0.89 vs 0.86) -- the gain is concentrated in
  // hard-pose cases. Unlike every prior bump above, this is a different
  // model producing a different 512-d embedding space, not a different
  // alignment/guard behavior on the same one -- see the comment on
  // _face_embedding_allowed_versions() in backend/app.py for why no
  // ArcFace-family version carries forward this time (same dimension, but
  // not cosine-comparable). PEOPLE_CLUSTER_EPS/PEOPLE_MATCH_THRESHOLD below
  // are left as-is for now (untested on AdaFace's real distribution);
  // recalibrate from live data once the library has re-embedded, same as
  // every previous embedding-version bump.
  // -adaface1-fixed (2026-07-31, same day): -adaface1's rollout never took
  // effect. The browser reads the model URL from window.__APP_CONFIG__.
  // arcFaceModelUrl, injected at container start by docker-entrypoint.sh,
  // which falls back to the OLD arcface path unless
  // APP_CONFIG_ARC_FACE_MODEL_URL is set -- it wasn't pinned below, so the
  // browser kept loading the old ArcFace model despite the code/version
  // change. Confirmed directly: faces relabeled to -adaface1 post-deploy had
  // cosine similarities bit-identical (4 decimals, 4 pairs) to their
  // pre-swap ArcFace values. Fixed by adding the
  // APP_CONFIG_ARC_FACE_MODEL_URL pin on the frontend container app (below)
  // and correcting the Dockerfile/docker-entrypoint.sh hardcoded fallbacks.
  // Re-bumped rather than just fixing the config, since every face already
  // carries the -adaface1 label and the staleness check alone would never
  // trigger a real re-embed.
  { name: 'FACE_CLUSTER_EMBEDDING_VERSION', value: 'browser-adaface-ir101-v1-fixed' }
  { name: 'PEOPLE_CLUSTER_EPS_2PT', value: '0.60' }
  { name: 'FACE_CLUSTER_EMBEDDING_DIMENSIONS', value: '512' }
  { name: 'SUSPICIOUS_FACE_CONFIDENCE', value: '0.75' }
  // Lowered from 0.55 (2026-08-01): this was a hard reject floor calibrated for
  // the pre-YOLOv8n-face detector, never revisited after the swap. YOLO's own
  // detection threshold is 0.35 (yoloFaceDetectionRuntime.ts) and its real-world
  // confidence for genuine, visible faces was directly measured (onnxruntime
  // against actual uploaded bytes) at 0.43-0.57 for a batch of otherwise-normal
  // photos -- all of which this 0.55 floor was silently hard-rejecting into
  // face_status='no_data' with zero visibility. 0.55 also inverted the
  // function's own intended tiering: it sat ABOVE FACE_LOW_CONFIDENCE_REJECT_BELOW
  // (0.36), making the area/side-ratio "curtain false positive" soft-check in
  // _client_face_passes_quality_gate permanently unreachable dead code (the hard
  // floor always resolved the decision first). 0.30 restores min_store <
  // reject_below and comfortably accepts the measured real-face range.
  { name: 'FACE_MIN_STORE_CONFIDENCE', value: '0.30' }
  { name: 'FACE_LOW_CONFIDENCE_REJECT_BELOW', value: '0.36' }
  { name: 'FACE_LOW_CONFIDENCE_MAX_AREA_RATIO', value: '0.08' }
  { name: 'FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO', value: '0.42' }
  // Face clustering operating point — recalibrated 2026-07-31 against the real
  // browser-arcface-aligned-v3 (5-point aligned, ArcFace-only) similarity
  // distribution measured on live data after the v3 embedding rollout: the
  // different-people ceiling (same-photo pairs, p99) dropped from ~0.59 under
  // v2 to ~0.46 under v3, and the best genuine cross-day same-person
  // candidate found rose from 0.613 (v2) to 0.625 (v3) — a real widening of
  // the gap (~0.02 under v2 vs ~0.15-0.17 under v3). The previous pins
  // (eps=0.38/absMax=0.36/match=0.66/assign=0.68) were calibrated against the
  // tighter v2 gap and left that 0.625 candidate un-mergeable: it clears eps
  // but the TIGHTER absolute_max_pair_distance ceiling (the real bar for a
  // 2-point cluster, not eps) required sim>=0.64. Loosened below to use the
  // v3 headroom while keeping a comfortable margin over the new ~0.46 ceiling.
  // IMPORTANT: these explicit eps/threshold pins are read BEFORE the preset
  // table in _resolve_people_cluster_config (env var wins over preset
  // default), so PEOPLE_CLUSTER_PRESET alone does nothing unless the values
  // below are kept in sync with it. See README "Tuning how aggressively faces
  // get merged" for the full preset table.
  // Nudged one more notch looser 2026-07-31 after confirming the first retune
  // merged a real cross-day pair (0.625 sim) — user spotted a few more
  // faces that still weren't merging. MIN_AUTO_FACE_MERGE_SIMILARITY (0.60,
  // below) acts as a hard floor on PEOPLE_MATCH_THRESHOLD, so 0.60 is as low
  // as match can go without also raising that floor.
  { name: 'PEOPLE_CLUSTER_PRESET', value: 'loose' }
  { name: 'PEOPLE_CLUSTER_EPS', value: '0.42' }
  { name: 'PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE', value: '0.40' }
  { name: 'PEOPLE_CLUSTER_MAX_PAIR_DISTANCE', value: '0.40' }
  { name: 'PEOPLE_MATCH_THRESHOLD', value: '0.60' }
  { name: 'PEOPLE_CLUSTER_ASSIGN_THRESHOLD', value: '0.62' }
  // Master safety floor that previously clamped EVERY auto path (match / assign /
  // propagate) up to 0.98 and blocked all automatic merges once v2 embeddings
  // landed. Lowered so the preset thresholds above actually govern.
  { name: 'MIN_AUTO_FACE_MERGE_SIMILARITY', value: '0.60' }
  // Identity propagation: use a named person's rep embedding to pull their faces
  // out of unnamed clusters (auto-move above AUTO, review queue above REVIEW).
  // This — NOT PEOPLE_CLUSTER_EPS/PEOPLE_MATCH_THRESHOLD above — is the lever
  // for "named cluster absorbs an unnamed one": recluster deliberately excludes
  // named people's own faces from its clustering pool (protects named clusters
  // from being scattered), so a named person can only grow via "Find more
  // faces" (this threshold), never via a plain recluster. REVIEW lowered
  // 2026-07-31 (0.62->0.58) after measuring a real named/unnamed pair at 0.602
  // that should have surfaced as a review suggestion but didn't.
  { name: 'PEOPLE_PROPAGATE_AUTO_THRESHOLD', value: '0.74' }
  { name: 'PEOPLE_PROPAGATE_REVIEW_THRESHOLD', value: '0.58' }
  { name: 'MAPS_ON_UPLOAD', value: 'false' }
  { name: 'MAPS_QUEUE_ON_UPLOAD', value: 'false' }
  // Minimum interval between automatic full-recluster (DBSCAN) maintenance
  // passes per user -- new faces are assigned synchronously in-process via
  // _assign_faces_to_people_incrementally (no worker involved) as they
  // arrive, so this pass only needs to run occasionally to merge fragmented
  // unnamed-person clusters. Without this gate, a sustained multi-hour
  // backfill kept re-firing it every ~2 minutes, keeping ownphotostore-worker
  // alive continuously doing a full re-cluster of the whole library.
  { name: 'PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS', value: '1800' }
]

resource backend 'Microsoft.App/containerApps@2024-03-01' = {
  name: backendAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 5000
        transport: 'auto'
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
      secrets: [
        { name: 'owner-password', value: adminPassword }
        { name: 'session-secret', value: sessionSecret }
        { name: 'acs-connection-string', value: acsConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          resources: {
            cpu: json('1.25')
            memory: '2.5Gi'
          }
          env: concat(backendEnv, [
            { name: 'APP_ROLE', value: 'backend' }
            // Gunicorn workers are separate processes and the app is imported
            // AFTER fork (no --preload), so every worker loads its own copy of
            // numpy/scipy/scikit-learn/Pillow + the Azure SDKs (~250-350 MB
            // each) and primes its own vector-index cache. Keep process
            // fan-out at 1 and use threads (below) for concurrency instead --
            // each additional worker duplicates that baseline RSS, while
            // threads share it.
            { name: 'GUNICORN_WORKERS', value: '1' }
            // 2.5Gi/1.25vCPU sized to comfortably hold 4 gthread threads (each
            // able to run a media/zip request) on top of the 1-worker
            // scientific-stack baseline above. 2026-08-10: live was manually
            // dropped to 0.25vCPU/0.5Gi while this stayed at 4, causing a
            // continuous OOM crash loop (exit 137) that broke uploads --
            // don't shrink cpu/memory here without shrinking this to match,
            // and don't shrink live without also shrinking this file.
            { name: 'GUNICORN_THREADS', value: '4' }
            // Prime vector indexes lazily on demand; eager startup priming can
            // inflate idle RSS and duplicate index memory across workers.
            { name: 'VECTOR_INDEX_PRIME_ON_STARTUP', value: 'false' }
            { name: 'OWNER_PASSWORD', secretRef: 'owner-password' }
            { name: 'SESSION_SECRET', secretRef: 'session-secret' }
            { name: 'ACS_CONNECTION_STRING', secretRef: 'acs-connection-string' }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        // Was 3. Raised to 5 after a real HAR capture showed the client-side
        // upload-parallelism fix (effectiveRtt cross-check, see
        // browserAiShared.ts) unlocked bursts of 16-20 concurrent files --
        // real throughput went up ~7x, but 3 replicas x 4 threads = 12
        // threads couldn't absorb that peak (replica count was even observed
        // dipping to 1 mid-burst), so per-request latency on
        // finalize/client-processing got worse even as overall wall-clock
        // time improved. Only raises the ceiling under load -- minReplicas
        // stays 0, idle scale-to-zero is unaffected.
        maxReplicas: 5
        rules: [
          {
            // Was 15 (raised from KEDA's default 10 to avoid fanning out to
            // extra replicas on every burst of small API calls). But each
            // replica only runs 1 worker x 4 threads -- waiting for 15
            // concurrent requests before adding capacity means 11 of them are
            // already queued behind those 4 threads first. Confirmed live via
            // HAR capture during a real upload burst: /upload/init, finalize,
            // and even trivial /health calls were all taking 7-60s, almost
            // entirely queueing time, not real work -- a handful of RAW-file
            // requests (since fixed to no longer block synchronously, see
            // finalize_uploaded_file) were holding all 4 threads for up to a
            // minute each with no relief arriving. 8 (2x thread count) scales
            // out sooner under real load while still absorbing a small burst
            // of quick polling calls without fanning out for it.
            name: 'http-scaler'
            http: {
              metadata: {
                concurrentRequests: '8'
              }
            }
          }
        ]
      }
    }
  }
}

resource frontend 'Microsoft.App/containerApps@2024-03-01' = {
  name: frontendAppName
  location: location
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'APP_CONFIG_API_BASE_URL', value: 'https://${backend.properties.configuration.ingress.fqdn}' }
            { name: 'APP_CONFIG_UPLOAD_BASE_URL', value: 'https://${backend.properties.configuration.ingress.fqdn}' }
            { name: 'APP_CONFIG_SPA_BASE_URL', value: frontendUrl }
            { name: 'APP_CONFIG_AUTH_MODE', value: 'password' }
            // Without this, the frontend's docker-entrypoint.sh defaults
            // processingMode to 'browser' regardless of what this deployment
            // was actually configured with -- caught live: a 'backend'-mode
            // deployment still served env.js with processingMode: "browser",
            // so the browser would have attempted full client-side AI anyway,
            // defeating the whole point of the mode. deployIpworker/PROCESSING_MODE
            // (backend container, above) were wired correctly; this was the
            // missing piece on the frontend side.
            { name: 'APP_CONFIG_PROCESSING_MODE', value: processingMode }
            { name: 'APP_CONFIG_FACE_API_MODEL_URL', value: '/models/face-api' }
            // Explicit pin, added with the AdaFace swap: docker-entrypoint.sh
            // generates window.__APP_CONFIG__.arcFaceModelUrl at container start
            // and falls back to the OLD arcface path if this isn't set. The
            // adaface1 deploy shipped without this pin, so the browser kept
            // loading the old ArcFace model via that fallback even after the
            // code/version-string change -- embeddingVersion got relabeled to
            // adaface1 but the actual embeddings computed were unchanged
            // (confirmed: identical cosine similarities to pre-swap values).
            { name: 'APP_CONFIG_ARC_FACE_MODEL_URL', value: '/models/browser-ai/models/adaface/model.onnx' }
          ]
        }
      ]
      scale: {
        // Scale-to-zero: nginx serving the SPA cold-starts in a couple of
        // seconds, and an idle deployment should cost nothing.
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
}

// The worker scales to ZERO and is woken by KEDA when the clustering queue has
// messages (2025-01-01 API for managed-identity scale rules; the queue itself
// is created by the backend at startup). An always-on worker polling an almost
// always-empty queue was the single largest fixed cost in the deployment.
resource worker 'Microsoft.App/containerApps@2025-01-01' = {
  name: workerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      secrets: [
        { name: 'acs-connection-string', value: acsConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: backendImage
          // 2026-08-10: live was manually bumped from 0.5vCPU/1Gi to 2/4Gi
          // (same incident/session as the backend cpu/memory fix above) --
          // unprojected full-table scans in worker job handlers (see the
          // library_clean OOM writeup) can pull large partitions into memory
          // in one pass. Keep this in sync with live; don't let it drift back
          // down to the old undersized default.
          resources: {
            cpu: json('2.0')
            memory: '4Gi'
          }
          env: concat(backendEnv, [
            { name: 'APP_ROLE', value: 'worker' }
            { name: 'CLUSTERING_WORKER_POLL_SECONDS', value: '2' }
            // receive_messages() with no visibility_timeout defaults to
            // Azure's 30s -- a full DBSCAN pass over a large library can
            // exceed that, causing Azure to redeliver the same message
            // before the finally-block delete runs (duplicate processing).
            // Long window is safe only because this app is maxReplicas=1
            // (only ever one consumer, so this just prevents self-redelivery).
            { name: 'CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS', value: '1800' }
            // The worker sends the "cleanup complete" email once a library_clean
            // job finishes; without this it silently no-ops (is_configured()
            // false) since ACS_CONNECTION_STRING lived only on the backend's env.
            { name: 'ACS_CONNECTION_STRING', secretRef: 'acs-connection-string' }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'clustering-queue'
            custom: {
              type: 'azure-queue'
              metadata: {
                accountName: storageAccountName
                queueName: 'photostore-clustering'
                queueLength: '1'
              }
              identity: 'system'
            }
          }
        ]
      }
    }
  }
}

// ipworker mirrors the browser's OCR/face/vision/geo pipeline server-side
// (see backend/ipwork_*.py) -- only deployed when processingMode says the
// server should (also) do this work. Modeled directly on `worker` above:
// scales to zero, woken by KEDA when its own queue has messages, its own
// image (NOT backendImage -- see ipworkerImage's description for why).
resource ipworker 'Microsoft.App/containerApps@2025-01-01' = if (deployIpworker) {
  name: ipworkerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    template: {
      containers: [
        {
          name: 'ipworker'
          image: ipworkerImage
          // Same footprint as worker's 2vCPU/4Gi (see above): a single pass
          // runs YOLO face detection, MediaPipe landmarks, AdaFace embedding,
          // CLIP tagging, and tesseract OCR in sequence for one photo.
          resources: {
            cpu: json('2.0')
            memory: '4Gi'
          }
          env: concat(backendEnv, [
            { name: 'APP_ROLE', value: 'ipworker' }
            { name: 'IPWORKER_POLL_SECONDS', value: '2' }
            // receive_messages() with no visibility_timeout defaults to Azure's
            // 30s, which a single ipwork pass (YOLO + MediaPipe + AdaFace + CLIP
            // + tesseract OCR in sequence) can exceed -- see
            // IPWORKER_VISIBILITY_TIMEOUT_SECONDS in backend/app.py for why this
            // matches IPWORKER_LEASE_SECONDS rather than copying the clustering
            // worker's much longer window (that one is safe at 1800s only
            // because it's maxReplicas=1; this app is maxReplicas=4).
            { name: 'IPWORKER_VISIBILITY_TIMEOUT_SECONDS', value: '300' }
            // Default (20s, see backend/app.py) is tuned for interactive
            // backend requests. A single ipwork pass takes ~60-90s, so at the
            // default TTL the per-photo people/face partition scan
            // (_load_people_embedding_index, _load_user_face_summary_by_id)
            // is cold again before the next photo -- every photo re-scans
            // the full people/face tables instead of reusing the cache.
            // Backend/worker keep the 20s default; only ipworker's per-photo
            // cadence outlasts it.
            { name: 'PEOPLE_SCAN_CACHE_TTL_SECONDS', value: '120' }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        // 4, not 1: at 1 replica, jobs process strictly one at a time (each
        // runs YOLO + AdaFace + CLIP + OCR in sequence, so a large backlog
        // -- e.g. from a library backfill -- drains for a very long time
        // with only one replica). KEDA still scales to 0 when the queue is
        // empty, so this only costs more while there's actually a backlog.
        maxReplicas: 4
        rules: [
          {
            name: 'ipwork-queue'
            custom: {
              type: 'azure-queue'
              metadata: {
                accountName: storageAccountName
                queueName: 'photostore-ipwork'
                queueLength: '1'
                // Default strategy ('all') counts messages that are dequeued
                // but not yet deleted, not just genuinely unclaimed ones. In
                // 'both' processing mode, a message ipworker lost the
                // per-photo lease race on is deliberately left undeleted so
                // Azure redelivers and retries it later (see
                // IPWORK_LEASE_RETRY_LIMIT in backend/app.py) -- with the
                // default strategy that retry-pending message still counts
                // as backlog, scaling ipworker up to handle photos the
                // browser already finished. 'visibleonly' (falls back to
                // 'all' above 32 messages, so real large backlogs still scale
                // correctly) fixes that without touching genuine backlog
                // handling.
                queueLengthStrategy: 'visibleonly'
              }
              identity: 'system'
            }
          }
        ]
      }
    }
  }
}

// Grant the backend and worker managed identities data-plane access to storage.
resource backendStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in storageRoleIds: {
    name: guid(storage.id, backend.id, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: backend.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource workerStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in storageRoleIds: {
    name: guid(storage.id, worker.id, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: worker.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource ipworkerStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in storageRoleIds: if (deployIpworker) {
    name: guid(storage.id, ipworkerAppName, roleId)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      // Non-null assertion: this loop shares ipworker's own `deployIpworker`
      // condition, so ipworker is guaranteed to exist whenever this
      // evaluates -- the linter just can't correlate the two `if`s.
      principalId: ipworker!.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

@description('Open this URL in your browser to use Photostore.')
output appUrl string = frontendUrl

@description('Backend API URL.')
output apiUrl string = 'https://${backend.properties.configuration.ingress.fqdn}'
