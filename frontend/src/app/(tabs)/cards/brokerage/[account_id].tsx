import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { Pressable, Text, View } from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';
import { SnapTradeHoldingsSection } from '@/components/SnapTradeSections';

export default function BrokerageHoldingsDetailPage() {
  const router = useRouter();
  const params = useLocalSearchParams<{ account_id?: string | string[] }>();
  const accountId = Array.isArray(params.account_id) ? params.account_id[0] : params.account_id;

  const backHeader = (
    <Stack.Screen
      options={{
        headerBackVisible: false,
        headerLeft: () => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="返回帳戶"
            onPress={() => router.dismissTo('/(tabs)/cards')}
            className="py-2 pr-3"
            testID="brokerage-back-to-accounts"
          >
            <Text className="text-white text-body">‹ 帳戶</Text>
          </Pressable>
        ),
      }}
    />
  );

  if (!accountId) {
    return (
      <>
        {backHeader}
        <View className="flex-1 items-center justify-center bg-ink-50 dark:bg-ink-950 px-6">
          <Text className="text-red-600 dark:text-red-400 text-body">券商帳戶識別碼無效</Text>
        </View>
      </>
    );
  }

  return (
    <>
      {backHeader}
      <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
        <View className="px-4 py-4 max-w-[800px] w-full mx-auto">
          <SnapTradeHoldingsSection accountId={accountId} />
        </View>
      </KeyboardAwareScrollView>
    </>
  );
}