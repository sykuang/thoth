// thoth backend — low-cost Azure Container Apps deployment
//
// Phase 11 (2026-06-15) — 升級用 Azure Key Vault 集中管 secret。
//
// Architecture:
//   - Default Azure network CAE, avoiding customer-VNet LB/public-IP charges
//   - Public TLS PostgreSQL with default-deny firewall managed outside this template
//   - Direct ACA secrets so startup does not require a Key Vault Private Endpoint
//   - Existing Key Vault remains a locked backup copy, opened only during deploy
//
// 為何 Key Vault：
//   - 集中 audit secret access (Log Analytics)
//   - 從 ACA secret store 拔出來，避免 deploy 流程 echo (e.g. deploy.sh stdout)
//   - 可獨立 rotate (改 Key Vault secret value → ACA 自動拉新值，不必重 deploy)
//   - 給開源 self-host user 的「安全升級路徑」(用 docker-compose 走 env 模式)
//
// Wiki: [[azure-container-apps-pg-flexible-thoth-deploy]]
//       [[azure-keyvault-aca-secret-integration]]

@description('Resource name prefix (lowercase, no special chars except hyphens).')
@minLength(3)
@maxLength(14)
param namePrefix string = 'thoth'

@description('Azure region. Use eastasia for Taiwan latency.')
param location string = 'eastasia'

@description('Pre-created ACR login server (e.g. thothacr<hash>.azurecr.io).')
param acrLoginServer string

@description('Container image full ref including registry.')
param containerImage string

@description('JWT signing secret. Generate locally with: openssl rand -hex 32')
@secure()
param jwtSecret string

@description('Fernet key for bank credentials encryption. MUST be preserved across deploys — losing this bricks ALL stored bank credentials.')
@secure()
param serverFernetKey string

@description('API key required to call any endpoint (defense-in-depth on top of JWT).')
@secure()
param serverApiKey string

@description('Separate API key for privileged admin-only operations.')
@secure()
param adminApiKey string

@description('PostgreSQL admin password. Generate: openssl rand -hex 24')
@secure()
param pgAdminPassword string

@description('Optional SnapTrade client ID. Leave empty to disable brokerage integration.')
@secure()
param snapTradeClientId string = ''

@description('Optional SnapTrade consumer key. Leave empty to disable brokerage integration.')
@secure()
param snapTradeConsumerKey string = ''

@description('Object ID of the deploying principal (az ad signed-in-user show --query id -o tsv). Granted Key Vault Secrets Officer to seed secrets during deploy.')
param deployerObjectId string

@description('CORS allow origins. Comma-separated exact origin list. 預設自動加入 Container App FQDN + Tauri WebView origins + 桌機 web localhost:8081。要客製化請傳完整逗號分隔的 origin 列表（會完全覆蓋預設）。')
param corsOrigins string = ''

@description('Allow Key Vault public network access. Default false: KV only reachable via the Private Endpoint. Set true temporarily during deploy.sh when seeding/rotating secrets, then flip back to false.')
param kvPublicAccess bool = false

@description('Optional IP address (CIDR ok) allowed to reach Key Vault from the public internet. Only honoured when kvPublicAccess=true. Leave empty to skip the IP allow list.')
param kvDeployerIpCidr string = ''

@description('Start only HTTP ingress so outbound IP firewall rules can be bootstrapped before any DB access.')
param bootstrapNetworkOnly bool = true


// -------- naming (everything derived from namePrefix) --------
var lawName = '${namePrefix}-law'
// Network type is immutable. Use blue/green names so the default-network
// stack can be created beside the current VNet stack and cut over safely.
var caeName = '${namePrefix}-cae-public'
var miName = '${namePrefix}-mi'
var appName = '${namePrefix}-backend-public'
var scheduledJobName = '${namePrefix}-sync-scheduled'
var queuedJobName = '${namePrefix}-sync-queued'
var reminderJobName = '${namePrefix}-payment-reminders'
var kvName = '${namePrefix}-kv-${take(uniqueString(resourceGroup().id), 6)}'
var pgServerName = '${namePrefix}-pg-public-${take(uniqueString(resourceGroup().id), 6)}'
var pgAdminUser = 'thothadmin'
var pgDbName = 'thoth'

// ACR is pre-created by deploy.sh
var acrName = split(split(acrLoginServer, '.')[0], '/')[0]
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// -------- Log Analytics --------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// -------- PostgreSQL Flexible Server (Burstable B1ms ~US$13/mo) --------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgServerName
  location: location
  sku: {
    name: 'Standard_B1ms'  // 1 vCPU, 2GB RAM
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: pgDbName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// -------- Managed Identity (ACR pull) --------
resource mi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: miName
  location: location
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, mi.id, acrPullRoleId)
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// -------- Key Vault --------
// RBAC mode (not access policies). Soft delete enabled by default (90d).
// Default: publicNetworkAccess Disabled. ACA receives direct secrets and does
// not need network access to this backup copy.
// During deploy.sh runs that need to write/rotate secrets from the deployer's
// laptop, set kvPublicAccess=true and pass kvDeployerIpCidr=<your-ip>/32; the
// firewall opens long enough for Bicep secret writes, then deploy.sh flips
// kvPublicAccess back to false in the cleanup step.
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true  // 不能停 — 防誤刪後 90 天回收期內 purge
    publicNetworkAccess: kvPublicAccess ? 'Enabled' : 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: kvPublicAccess && !empty(kvDeployerIpCidr) ? [
        {
          value: kvDeployerIpCidr
        }
      ] : []
      virtualNetworkRules: []
    }
  }
}


// Grant deployer (e.g. az signed-in user) "Key Vault Secrets Officer" so Bicep
// can write secrets during deploy. Role 必須在 secret 寫入之前生效。
var secretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, deployerObjectId, secretsOfficerRoleId)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsOfficerRoleId)
  }
}

// -------- Seed secrets into Key Vault --------
// DATABASE_URL 在 Bicep 內拼出真實 connection string（含 pgAdminPassword）。
// pgAdminPassword 是 @secure() param，內插後仍保 secure flag 直到寫入 KV secret value。
// **注意**：不能拿 databaseUrl 當 Bicep output (會 fail at deploy time，secure value 不能輸出)。
var databaseUrl = format('postgresql://{0}:{1}@{2}:5432/{3}?sslmode=require', pgAdminUser, pgAdminPassword, pg.properties.fullyQualifiedDomainName, pgDbName)
var snapTradeConfigured = !empty(snapTradeClientId) && !empty(snapTradeConsumerKey)

resource kvSecretJwt 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess) {
  parent: kv
  name: 'jwt-secret'
  properties: {
    value: jwtSecret
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer  // ensure RBAC settled before write
  ]
}

resource kvSecretFernet 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess) {
  parent: kv
  name: 'fernet-key-v2'
  properties: {
    value: serverFernetKey
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

resource kvSecretApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess) {
  parent: kv
  name: 'api-key'
  properties: {
    value: serverApiKey
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

resource kvSecretAdminApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess) {
  parent: kv
  name: 'admin-api-key'
  properties: {
    value: adminApiKey
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

resource kvSecretDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess) {
  parent: kv
  name: 'database-url-public'
  properties: {
    value: databaseUrl
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

resource kvSecretSnapTradeClientId 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess && snapTradeConfigured) {
  parent: kv
  name: 'snaptrade-client-id'
  properties: {
    value: snapTradeClientId
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

resource kvSecretSnapTradeConsumerKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (kvPublicAccess && snapTradeConfigured) {
  parent: kv
  name: 'snaptrade-consumer-key'
  properties: {
    value: snapTradeConsumerKey
    attributes: {
      enabled: true
    }
  }
  dependsOn: [
    deployerSecretsOfficer
  ]
}

// -------- Container Apps Environment --------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// -------- Container App --------
// CORS origin 預設組成：
//   1. https://<container-app-fqdn>  — ACA 自身
//   2. tauri://localhost              — Tauri macOS WKWebView origin
//   3. https://tauri.localhost        — Tauri Linux/Windows WebView2 origin
//   4. http://localhost:8081          — Expo dev server
var defaultCorsOrigins = 'https://${appName}.${cae.properties.defaultDomain},tauri://localhost,https://tauri.localhost,http://localhost:8081'
var effectiveCorsOrigins = empty(corsOrigins) ? defaultCorsOrigins : corsOrigins
var workerSecrets = concat([
  { name: 'fernet-key-v2', value: serverFernetKey }
  { name: 'database-url-public', value: databaseUrl }
], snapTradeConfigured ? [
  { name: 'snaptrade-client-id', value: snapTradeClientId }
  { name: 'snaptrade-key', value: snapTradeConsumerKey }
] : [])
var workerEnv = concat([
  { name: 'SERVER_FERNET_KEY', secretRef: 'fernet-key-v2' }
  { name: 'DB_BACKEND', value: 'postgres' }
  { name: 'SYNC_EXECUTION_MODE', value: 'external' }
  { name: 'PUSH_PROVIDER', value: 'expo' }
  { name: 'DATABASE_URL', secretRef: 'database-url-public' }
  { name: 'PYTHONUNBUFFERED', value: '1' }
], snapTradeConfigured ? [
  { name: 'SNAPTRADE_CLIENT_ID', secretRef: 'snaptrade-client-id' }
  { name: 'SNAPTRADE_CONSUMER_KEY', secretRef: 'snaptrade-key' }
] : [])

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: concat([
        {
          name: 'jwt-secret'
          value: jwtSecret
        }
        {
          name: 'fernet-key-v2'
          value: serverFernetKey
        }
        {
          name: 'api-key'
          value: serverApiKey
        }
        {
          name: 'admin-api-key'
          value: adminApiKey
        }
        {
          name: 'database-url-public'
          value: databaseUrl
        }
      ], snapTradeConfigured ? [
        {
          name: 'snaptrade-client-id'
          value: snapTradeClientId
        }
        {
          name: 'snaptrade-key'
          value: snapTradeConsumerKey
        }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: concat([
            { name: 'JWT_SECRET', secretRef: 'jwt-secret' }
            { name: 'SERVER_FERNET_KEY', secretRef: 'fernet-key-v2' }
            { name: 'SERVER_API_KEY', secretRef: 'api-key' }
            { name: 'ADMIN_API_KEY', secretRef: 'admin-api-key' }
            { name: 'DB_BACKEND', value: 'postgres' }
            { name: 'SYNC_EXECUTION_MODE', value: 'external' }
            { name: 'THOTH_BOOTSTRAP_NETWORK_ONLY', value: bootstrapNetworkOnly ? '1' : '0' }
            // Production frontend registers Expo push tokens by default. Keep
            // backend provider aligned; otherwise scheduler/payment reminders
            // use NoOpNotifier and silently deliver 0 notifications.
            { name: 'PUSH_PROVIDER', value: 'expo' }
            { name: 'DATABASE_URL', secretRef: 'database-url-public' }
            { name: 'CORS_ORIGINS', value: effectiveCorsOrigins }
            { name: 'PYTHONUNBUFFERED', value: '1' }
          ], snapTradeConfigured ? [
            { name: 'SNAPTRADE_CLIENT_ID', secretRef: 'snaptrade-client-id' }
            { name: 'SNAPTRADE_CONSUMER_KEY', secretRef: 'snaptrade-key' }
          ] : [])
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              initialDelaySeconds: 15
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    pgDb
  ]
}

resource scheduledSyncJob 'Microsoft.App/jobs@2024-03-01' = {
  name: scheduledJobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 900
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: '0 2,4,10 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: workerSecrets
    }
    template: {
      containers: [
        {
          name: 'scheduled-sync'
          image: containerImage
          command: [
            'uv'
            'run'
            'python'
            '-m'
            'backend.server.sync_job_worker'
          ]
          args: [
            'scheduled'
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: workerEnv
        }
      ]
    }
  }
  dependsOn: [
    pgDb
  ]
}

resource queuedSyncJob 'Microsoft.App/jobs@2024-03-01' = {
  name: queuedJobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Event'
      replicaTimeout: 21600
      replicaRetryLimit: 0
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          pollingInterval: 30
          minExecutions: 0
          maxExecutions: 1
          rules: [
            {
              name: 'queued-sync-jobs'
              type: 'postgresql'
              metadata: {
                query: 'SELECT COUNT(*) FROM sync_jobs WHERE status=\'queued\' OR (status=\'running\' AND started_at IS NOT NULL AND started_at::timestamptz < NOW() - INTERVAL \'7 hours\')'
                targetQueryValue: '1'
              }
              auth: [
                {
                  triggerParameter: 'connection'
                  secretRef: 'database-url-public'
                }
              ]
            }
          ]
        }
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: workerSecrets
    }
    template: {
      containers: [
        {
          name: 'queued-sync'
          image: containerImage
          command: [
            'uv'
            'run'
            'python'
            '-m'
            'backend.server.sync_job_worker'
          ]
          args: [
            'queued'
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: workerEnv
        }
      ]
    }
  }
  dependsOn: [
    pgDb
  ]
}

resource paymentReminderJob 'Microsoft.App/jobs@2024-03-01' = {
  name: reminderJobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${mi.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 900
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: '0 1 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: mi.id
        }
      ]
      secrets: workerSecrets
    }
    template: {
      containers: [
        {
          name: 'payment-reminders'
          image: containerImage
          command: [
            'uv'
            'run'
            'python'
            '-m'
            'backend.server.sync_job_worker'
          ]
          args: [
            'reminders'
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: workerEnv
        }
      ]
    }
  }
  dependsOn: [
    pgDb
  ]
}

// -------- outputs --------
output appFqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output acrName string = acr.name
output resourceGroup string = resourceGroup().name
output pgServerName string = pg.name
output pgFqdn string = pg.properties.fullyQualifiedDomainName
output pgDatabase string = pgDbName
output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output managedIdentityClientId string = mi.properties.clientId
output managedIdentityPrincipalId string = mi.properties.principalId
