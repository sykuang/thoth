// thoth backend — Azure Container Apps deployment (Key Vault edition)
//
// Phase 11 (2026-06-15) — 升級用 Azure Key Vault 集中管 secret。
//
// Architecture:
//   - Bicep params 帶 secret 值 → 寫入 Key Vault（首次 deploy）
//   - Container App secrets 改成 keyVaultUrl reference，用 Managed Identity 拉
//   - DATABASE_URL 在 Bicep 內拼出真實值（含 pgAdminPassword），寫進 Key Vault
//     (舊版用 `***` placeholder 然後手動補洞，這次內建)
//   - 同一 Managed Identity 共用 ACR pull + Key Vault Secrets User
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

@description('PostgreSQL admin password. Generate: openssl rand -hex 24')
@secure()
param pgAdminPassword string

@description('Object ID of the deploying principal (az ad signed-in-user show --query id -o tsv). Granted Key Vault Secrets Officer to seed secrets during deploy.')
param deployerObjectId string

@description('CORS allow origins. Comma-separated exact origin list. 預設自動加入 Container App FQDN + Tauri WebView origins + 桌機 web localhost:8081。要客製化請傳完整逗號分隔的 origin 列表（會完全覆蓋預設）。')
param corsOrigins string = ''

@description('Allow Key Vault public network access. Default false: KV only reachable via the Private Endpoint. Set true temporarily during deploy.sh when seeding/rotating secrets, then flip back to false.')
param kvPublicAccess bool = false

@description('Optional IP address (CIDR ok) allowed to reach Key Vault from the public internet. Only honoured when kvPublicAccess=true. Leave empty to skip the IP allow list.')
param kvDeployerIpCidr string = ''



// -------- naming (everything derived from namePrefix) --------
var lawName = '${namePrefix}-law'
// VNet-enabled Container Apps Environment cannot be retrofitted onto the old
// default-network CAE. Use blue/green names so deploy can create the private
// stack beside the current public stack, then cut over intentionally.
var caeName = '${namePrefix}-cae-vnet'
var miName = '${namePrefix}-mi'
var appName = '${namePrefix}-backend-vnet'
var kvName = '${namePrefix}-kv-${take(uniqueString(resourceGroup().id), 6)}'
var pgServerName = '${namePrefix}-pg-vnet-${take(uniqueString(resourceGroup().id), 6)}'
var pgAdminUser = 'thothadmin'
var pgDbName = 'thoth'
var vnetName = '${namePrefix}-vnet'
var caeSubnetName = 'containerapps'
var privateEndpointSubnetName = 'private-endpoints'
var pgSubnetName = 'postgresql'
var pgPrivateDnsZoneName = '${namePrefix}.private.postgres.database.azure.com'
var kvPrivateEndpointName = '${namePrefix}-kv-pe'
var kvPrivateDnsZoneName = 'privatelink.vaultcore.azure.net'

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

// -------- Network (VNet for ACA egress + PG delegated subnet) --------
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: caeSubnetName
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: '10.42.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: pgSubnetName
        properties: {
          addressPrefix: '10.42.3.0/24'
          delegations: [
            {
              name: 'Microsoft.DBforPostgreSQL.flexibleServers'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource caeSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: caeSubnetName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}

resource pgSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: pgSubnetName
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
      delegatedSubnetResourceId: pgSubnet.id
      privateDnsZoneArmResourceId: pgPrivateDnsZone.id
      publicNetworkAccess: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
  }
  dependsOn: [
    pgPrivateDnsVnetLink
  ]
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: pgDbName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// -------- PostgreSQL Private access DNS --------
resource pgPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: pgPrivateDnsZoneName
  location: 'global'
}

resource pgPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: pgPrivateDnsZone
  name: '${namePrefix}-pg-vnet-dns-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

// -------- Managed Identity (ACR pull + Key Vault Secrets User) --------
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
// Default: publicNetworkAccess Disabled + Private Endpoint only — secrets are
// only reachable from inside the VNet (ACA control plane reaches KV via PE).
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

// -------- Key Vault Private DNS + Private Endpoint --------
resource kvPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: kvPrivateDnsZoneName
  location: 'global'
}

resource kvPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: kvPrivateDnsZone
  name: '${namePrefix}-kvdns-vnet-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource kvPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: kvPrivateEndpointName
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'kv-conn'
        properties: {
          privateLinkServiceId: kv.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
}

resource kvPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: kvPrivateEndpoint
  name: 'kv-dns-zg'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'kv-zone-config'
        properties: {
          privateDnsZoneId: kvPrivateDnsZone.id
        }
      }
    ]
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

// Grant Container App's MI "Key Vault Secrets User" so ACA can read secrets at runtime
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
resource miSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, mi.id, secretsUserRoleId)
  properties: {
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
  }
}

// -------- Seed secrets into Key Vault --------
// DATABASE_URL 在 Bicep 內拼出真實 connection string（含 pgAdminPassword）。
// pgAdminPassword 是 @secure() param，內插後仍保 secure flag 直到寫入 KV secret value。
// **注意**：不能拿 databaseUrl 當 Bicep output (會 fail at deploy time，secure value 不能輸出)。
var databaseUrl = format('postgresql://{0}:{1}@{2}:5432/{3}?sslmode=require', pgAdminUser, pgAdminPassword, pg.properties.fullyQualifiedDomainName, pgDbName)

resource kvSecretJwt 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
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

resource kvSecretFernet 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
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

resource kvSecretApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
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

resource kvSecretDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'database-url-vnet-v2'
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

// -------- Container Apps Environment --------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: caeSubnet.id
      internal: false
    }
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
      // Key Vault reference 模式：value 改成 keyVaultUrl + identity
      // ACA runtime 用指定的 MI 去 Key Vault 拉，注入成 env 給 container
      secrets: [
        {
          name: 'jwt-secret'
          keyVaultUrl: kvSecretJwt.properties.secretUri
          identity: mi.id
        }
        {
          name: 'fernet-key-v2'
          keyVaultUrl: kvSecretFernet.properties.secretUri
          identity: mi.id
        }
        {
          name: 'api-key'
          keyVaultUrl: kvSecretApiKey.properties.secretUri
          identity: mi.id
        }
        {
          name: 'database-url-vnet-v2'
          keyVaultUrl: kvSecretDatabaseUrl.properties.secretUri
          identity: mi.id
        }
      ]
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
          env: [
            { name: 'JWT_SECRET', secretRef: 'jwt-secret' }
            { name: 'SERVER_FERNET_KEY', secretRef: 'fernet-key-v2' }
            { name: 'SERVER_API_KEY', secretRef: 'api-key' }
            { name: 'DB_BACKEND', value: 'postgres' }
            // Production frontend registers Expo push tokens by default. Keep
            // backend provider aligned; otherwise scheduler/payment reminders
            // use NoOpNotifier and silently deliver 0 notifications.
            { name: 'PUSH_PROVIDER', value: 'expo' }
            { name: 'DATABASE_URL', secretRef: 'database-url-vnet-v2' }
            { name: 'CORS_ORIGINS', value: effectiveCorsOrigins }
            { name: 'PYTHONUNBUFFERED', value: '1' }
          ]
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
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    pgDb
    miSecretsUser  // MUST exist before ACA tries to read from Key Vault
    // kvSecret* dependencies are implicit via keyVaultUrl: kvSecretX.properties.secretUri
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
