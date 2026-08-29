const LEGACY_SERVER_URL = 'https://thoth-backend-vnet.kindrock-f20f04e5.eastasia.azurecontainerapps.io';
const PUBLIC_SERVER_URL = 'https://thoth-backend-public.orangeriver-75566f4a.eastasia.azurecontainerapps.io';

export function migrateServerUrl(serverUrl: string): string {
  return serverUrl.replace(/\/$/, '') === LEGACY_SERVER_URL ? PUBLIC_SERVER_URL : serverUrl;
}
