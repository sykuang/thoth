import { useLocalSearchParams } from 'expo-router';
import { Text, View } from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { SnapTradeHoldingsSection } from '@/components/SnapTradeSections';

export default function BrokerageHoldingsDetailPage() {
  const params = useLocalSearchParams<{ account_id?: string | string[] }>();
  const accountId = Array.isArray(params.account_id) ? params.account_id[0] : params.account_id;

  if (!accountId) {
    return (
      <View className="flex-1 items-center justify-center bg-ink-50 dark:bg-ink-950 px-6">
        <Text className="text-red-600 dark:text-red-400 text-body">券商帳戶識別碼無效</Text>
      </View>
    );
  }

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-4 py-4 max-w-[800px] w-full mx-auto">
        <SnapTradeHoldingsSection accountId={accountId} />
      </View>
    </KeyboardAwareScrollView>
  );
}