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
// Authentication is OFF by default, so the app runs immediately. See the
// README for how to add Microsoft Entra sign-in later.

@description('Base name used as a prefix for resources. Lowercase letters and numbers work best.')
@minLength(3)
@maxLength(17)
param appName string = 'photostore'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Public backend image. Override only if you publish your own fork\'s images.')
param backendImage string = 'ghcr.io/git4rajat/photostore-backend:latest'

@description('Public frontend image. Override only if you publish your own fork\'s images.')
param frontendImage string = 'ghcr.io/git4rajat/photostore-frontend:latest'

extension microsoftGraphV1

var suffix = uniqueString(resourceGroup().id)
var storageAccountName = take(toLower(replace('${appName}${suffix}', '-', '')), 24)

var logWorkspaceName = '${appName}-logs'
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
// Microsoft Entra sign-in. The template creates the backend API + frontend SPA
// app registrations, wires the SPA's redirect URI to the app URL, and
// admin-consents the frontend->backend permission — all in this one deployment.
// Requires the deploying identity to be able to register apps and grant consent.
// ---------------------------------------------------------------------------
var apiScopeId = guid(resourceGroup().id, 'api-writer-scope')

resource backendApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${appName}-backend-api-${suffix}'
  displayName: '${appName}-backend-api'
  signInAudience: 'AzureADMyOrg'
  api: {
    oauth2PermissionScopes: [
      {
        id: apiScopeId
        value: 'API.Writer'
        type: 'User'
        isEnabled: true
        adminConsentDisplayName: 'Access the Photostore API'
        adminConsentDescription: 'Allow the app to access the Photostore API on behalf of the signed-in user.'
        userConsentDisplayName: 'Access the Photostore API'
        userConsentDescription: 'Allow the app to access the Photostore API on your behalf.'
      }
    ]
  }
}

resource backendSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: backendApp.appId
}

resource frontendApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${appName}-frontend-spa-${suffix}'
  displayName: '${appName}-frontend-spa'
  signInAudience: 'AzureADMyOrg'
  spa: {
    redirectUris: [ frontendUrl ]
  }
  requiredResourceAccess: [
    {
      resourceAppId: backendApp.appId
      resourceAccess: [ { id: apiScopeId, type: 'Scope' } ]
    }
  ]
}

resource frontendSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: frontendApp.appId
}

resource apiGrant 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: frontendSp.id
  resourceId: backendSp.id
  consentType: 'AllPrincipals'
  scope: 'API.Writer'
}

// Auth values fed into the container apps.
var authTenantId = tenant().tenantId
var authBackendClientId = backendApp.appId
var authFrontendClientId = frontendApp.appId
var authApiScope = '${backendApp.appId}/.default'

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

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspace.properties.customerId
        sharedKey: logWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

// Shared backend/worker environment variables.
var backendEnv = [
  { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'USE_MANAGED_IDENTITY', value: 'true' }
  { name: 'MEDIA_URL_MODE', value: 'proxy' }
  { name: 'MAX_UPLOAD_FILE_BYTES', value: '5368709120' }
  { name: 'BLOB_IMAGE_CONTAINER', value: 'images' }
  { name: 'BLOB_THUMBNAIL_CONTAINER', value: 'thumbnails' }
  { name: 'BLOB_COVER_CONTAINER', value: 'covers' }
  { name: 'METADATA_TABLE', value: 'photometadata' }
  { name: 'ALBUMS_TABLE', value: 'photoalbums' }
  { name: 'PUBLIC_ALBUM_ATTEMPTS_TABLE', value: 'publicalbumattempts' }
  { name: 'ALLOWED_ORIGINS', value: '*' }
  { name: 'AUTH_REQUIRED', value: 'true' }
  { name: 'AUTH_MODE', value: 'entra' }
  { name: 'AZURE_AD_TENANT_ID', value: authTenantId }
  { name: 'AZURE_AD_CLIENT_ID', value: authBackendClientId }
  { name: 'AZURE_AD_API_AUDIENCE', value: authBackendClientId }
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
            { name: 'GUNICORN_WORKERS', value: '4' }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
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
            { name: 'APP_CONFIG_AUTH_MODE', value: 'entra' }
            { name: 'APP_CONFIG_AZURE_AD_TENANT_ID', value: authTenantId }
            { name: 'APP_CONFIG_AZURE_AD_CLIENT_ID', value: authFrontendClientId }
            { name: 'APP_CONFIG_AZURE_AD_API_SCOPE', value: authApiScope }
            { name: 'APP_CONFIG_FACE_API_MODEL_URL', value: '/models/face-api' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
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
        minReplicas: 1
        maxReplicas: 1
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
