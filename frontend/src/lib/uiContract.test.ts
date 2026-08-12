import ts from 'typescript';

function source(path: string): string {
  const text = ts.sys.readFile(path);
  if (!text) throw new Error(`cannot read ${path}`);
  return text;
}

function includes(haystack: string, needle: string, message: string): void {
  if (!haystack.includes(needle)) throw new Error(message);
}

function excludes(haystack: string, needle: string, message: string): void {
  if (haystack.includes(needle)) throw new Error(message);
}

const tabs = source('src/app/(tabs)/_layout.tsx');
const dashboard = source('src/app/(tabs)/dashboard.tsx');
const transactions = source('src/app/(tabs)/transactions.tsx');
const accounts = source('src/app/(tabs)/cards/index.tsx');
const settings = source('src/app/(tabs)/settings/index.tsx');
const transactionDetail = source('src/components/transactions/TxnDetailModal.tsx');
const brokerageTransaction = source('src/components/transactions/BrokerageTxnRow.tsx');
const brokerageAccounts = source('src/components/SnapTradeSections.tsx');
const login = source('src/app/login.tsx');
const biometric = source('src/lib/biometric.ts');
const api = source('src/lib/api.ts');
const push = source('src/lib/push.ts');
const pushNative = source('src/lib/pushNative.ts');

excludes(transactions, 'transactions-fab', 'transaction screen must not expose a placeholder FAB');
excludes(transactions, "console.log('FAB tapped')", 'transaction screen must not retain a dead FAB handler');

for (const [name, text] of [['login', login], ['biometric', biometric], ['api', api]] as const) {
  excludes(text, '銀行爬蟲', `${name} still exposes the retired product name`);
}

includes(tabs, 'headerShown: false', 'root tab screens must not duplicate their in-page titles');
includes(tabs, 'useSafeAreaInsets()', 'headerless root tabs must read the native safe area');
includes(tabs, 'paddingTop: isDesktop ? 0 : insets.top', 'headerless native root tabs must clear the status bar');
includes(tabs, "tabBarPosition: isDesktop ? 'left' : 'bottom'", 'web desktop must use a rail instead of stretched bottom tabs');
includes(tabs, "tabBarVariant: isDesktop ? 'material' : 'uikit'", 'desktop below-icon rail requires the material variant');

excludes(push, "from 'expo-notifications'", 'web-safe push wrapper must not statically import expo-notifications');
includes(push, "Platform.OS === 'web'", 'web-safe push wrapper must have an explicit web no-op boundary');
includes(push, 'return await native.getPushPermissionStatusNative()', 'permission probe must catch native async rejection');
includes(pushNative, 'export async function registerForPushNotificationsNative', 'native passive registration is missing');
includes(pushNative, 'export async function requestPushNotificationsNative', 'native opt-in registration is missing');
includes(pushNative, 'setNotificationChannelAsync', 'Android opt-in must create a notification channel');
includes(pushNative, "return token ? 'registered' : 'registration_failed'", 'push opt-in must report registration failure');
const passiveRegistration = pushNative.slice(
  pushNative.indexOf('export async function registerForPushNotificationsNative'),
  pushNative.indexOf('export async function requestPushNotificationsNative'),
);
excludes(passiveRegistration, 'requestPermissionsAsync', 'app launch must not request notification permission');
includes(settings, 'settings-push-enable', 'notification permission must have an explicit Settings opt-in');
includes(settings, "AppState.addEventListener('change'", 'Settings must refresh permission after system settings');
includes(settings, 'accessibilityState={{ disabled }}', 'push control must expose disabled accessibility state');

for (const glyph of ['☁️', '💳', '⚙️', '✏️', '👁️', '🙈']) {
  excludes(accounts, glyph, `accounts actions still use OS emoji glyph ${glyph}`);
}
includes(accounts, 'MoreHorizontal', 'low-frequency bank actions must be grouped behind one overflow control');
includes(accounts, 'contentInsetAdjustmentBehavior="automatic"', 'accounts scroll view must respect native insets');
includes(accounts, 'contentContainerStyle={{ paddingBottom: 32 }}', 'accounts list must retain scrollable bottom spacing');
excludes(accounts, 'formatSignedCurrency', 'account-tab amounts must use color rather than signs');
includes(accounts, "'text-red-600 dark:text-red-400'", 'account-tab liabilities must use the red amount tone');
includes(accounts, "'text-emerald-600 dark:text-emerald-400'", 'account-tab assets must use the green amount tone');
includes(brokerageAccounts, 'formatAbsoluteDecimalCurrency', 'brokerage account totals must use color rather than signs');
excludes(accounts, 'borderLeftWidth: 4', 'normal bank groups must not use warning-style side stripes');
excludes(brokerageTransaction, 'w-1 bg-brand-500', 'normal brokerage transactions must not use status-style side stripes');
includes(accounts, "flexBasis: '48%', flexGrow: 0", 'desktop bank groups must keep stable two-column width');

includes(settings, 'accessibilityState={{ expanded }}', 'settings disclosures must expose expanded accessibility state');
includes(dashboard, 'showPortfolioCard &&', 'dashboard must not reserve an empty portfolio column');
includes(dashboard, 'showKpiCard &&', 'dashboard must not reserve an empty KPI column');
includes(login, '敏感資料加密保存', 'login value copy must stay user-facing');
includes(settings, '管理主分類、子分類與自訂標籤', 'settings copy must stay user-facing');
includes(transactionDetail, "formatSignedCurrency(txn.balance, 'TWD')", 'account balances must preserve their sign');

for (const [name, text] of [
  ['dashboard', dashboard],
  ['transactions', transactions],
  ['accounts', accounts],
] as const) {
  excludes(text, 'function fmtTWD', `${name} still owns a local TWD formatter`);
  excludes(text, 'function fmtNTD', `${name} still owns a local NTD formatter`);
  excludes(text, 'function formatTwdAmount', `${name} still owns a local TWD formatter`);
}

console.log('UI contract tests passed');
