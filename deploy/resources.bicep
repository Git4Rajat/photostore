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

@description('Public backend image. Override only if you publish your own fork\'s images.')
param backendImage string = 'ghcr.io/git4rajat/photostore-backend:latest'

@description('Public frontend image. Override only if you publish your own fork\'s images.')
param frontendImage string = 'ghcr.io/git4rajat/photostore-frontend:latest'

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
  { name: 'BROWSER_ONLY_PROCESSING', value: 'true' }
  { name: 'FACE_CLUSTER_EMBEDDING_VERSION', value: 'browser-hybrid-arcface-faceapi-v1' }
  { name: 'FACE_CLUSTER_EMBEDDING_DIMENSIONS', value: '640' }
  { name: 'PEOPLE_CLUSTER_PRESET', value: 'strictest' }
  { name: 'MAPS_ON_UPLOAD', value: 'false' }
  { name: 'MAPS_QUEUE_ON_UPLOAD', value: 'false' }
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
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(backendEnv, [
            { name: 'APP_ROLE', value: 'backend' }
            // Gunicorn workers are separate processes and the app is imported
            // AFTER fork (no --preload), so every worker loads its own copy of
            // numpy/scipy/scikit-learn/Pillow + the Azure SDKs (~250-350 MB
            // each) and primes its own vector-index cache. At 4 workers that
            // baseline alone crowds the 1 GiB limit, so a few concurrent
            // image/zip requests tipped the replica into an OOM kill. Two
            // workers keeps CPU-parallelism for image work while leaving real
            // headroom; concurrency inside a worker comes from GUNICORN_THREADS.
            { name: 'GUNICORN_WORKERS', value: '2' }
            { name: 'OWNER_PASSWORD', secretRef: 'owner-password' }
            { name: 'SESSION_SECRET', secretRef: 'session-secret' }
            { name: 'ACS_CONNECTION_STRING', secretRef: 'acs-connection-string' }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            // Default is 10 concurrent requests per replica, which fans out to
            // extra replicas on every burst of small API calls. Each replica
            // runs 2 gunicorn workers x 8 threads, so 50 concurrent requests
            // is still comfortably inside one replica's capacity.
            name: 'http-scaler'
            http: {
              metadata: {
                concurrentRequests: '50'
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
            { name: 'APP_CONFIG_FACE_API_MODEL_URL', value: '/models/face-api' }
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
    configuration: {}
    template: {
      containers: [
        {
          name: 'worker'
          image: backendImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(backendEnv, [
            { name: 'APP_ROLE', value: 'worker' }
            { name: 'CLUSTERING_WORKER_POLL_SECONDS', value: '2' }
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

@description('Open this URL in your browser to use Photostore.')
output appUrl string = frontendUrl

@description('Backend API URL.')
output apiUrl string = 'https://${backend.properties.configuration.ingress.fqdn}'
