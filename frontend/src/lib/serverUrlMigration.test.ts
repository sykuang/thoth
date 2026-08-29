import { migrateServerUrl } from './serverUrlMigration';

function assertEqual(actual: string, expected: string): void {
  if (actual !== expected) throw new Error(`expected ${expected}, got ${actual}`);
}

const legacy = 'https://thoth-backend-vnet.kindrock-f20f04e5.eastasia.azurecontainerapps.io';
const replacement = 'https://thoth-backend-public.orangeriver-75566f4a.eastasia.azurecontainerapps.io';

assertEqual(migrateServerUrl(legacy), replacement);
assertEqual(migrateServerUrl(`${legacy}/`), replacement);
assertEqual(migrateServerUrl('https://self-hosted.example'), 'https://self-hosted.example');
assertEqual(migrateServerUrl(''), '');

console.log('serverUrlMigration tests passed');
