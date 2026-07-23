// Dependency-free regression probe for notification route normalization.
// It runs against the TypeScript source text so we do not need a frontend test runner.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const nativeIntent = fs.readFileSync(path.join(root, 'src/app/+native-intent.tsx'), 'utf8');
const layout = fs.readFileSync(path.join(root, 'src/app/_layout.tsx'), 'utf8');
const pushRoutes = fs.readFileSync(path.join(root, 'src/lib/pushRoutes.ts'), 'utf8');
const syncRunner = fs.readFileSync(path.join(root, '../backend/server/sync_runner.py'), 'utf8');

assert.match(nativeIntent, /path === 'thoth:\/\/\/'/);
assert.match(nativeIntent, /return '\/'/);
assert.match(layout, /routeFromNotificationData\(data\)/);
assert.doesNotMatch(layout, /router\.push\(deepLink as never\)/);
assert.match(pushRoutes, /case 'payment_reminder':[\s\S]*return '\/\(tabs\)\/cards';/);
assert.match(pushRoutes, /explicit !== '\/'/);
assert.doesNotMatch(syncRunner, /"deep_link": f?"\/(sync|cards\?)/);
assert.match(syncRunner, /CARDS_TAB_ROUTE = "\/\(tabs\)\/cards"/);

console.log('push route regression probe passed');
