import { useRouter } from 'expo-router';
import { Building2, WalletCards } from 'lucide-react-native';
import { Pressable, Text, View } from 'react-native';

import { KeyboardAwareScrollView } from '@/components/KeyboardAwareScrollView';


export default function AddAccountScreen() {
  const router = useRouter();

  return (
    <KeyboardAwareScrollView className="flex-1 bg-ink-50 dark:bg-ink-950">
      <View className="px-4 py-6 max-w-[720px] w-full mx-auto">

        <Text className="text-ink-900 dark:text-ink-50 text-h1 mb-1">新增帳戶</Text>
        <Text className="text-ink-500 dark:text-ink-400 text-small mb-5">
          選擇連結銀行，或建立自行維護的手動帳戶。
        </Text>

        <Pressable
          onPress={() => router.push('/(tabs)/cards/new')}
          accessibilityRole="button"
          accessibilityLabel="連結銀行帳號"
          className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card mb-4 active:opacity-80"
          testID="add-bank-account-option"
        >
          <View className="flex-row items-center gap-4">
            <View className="bg-brand-100 dark:bg-brand-950 rounded-xl p-3">
              <Building2 size={24} color="#9333ea" />
            </View>
            <View className="flex-1">
              <Text className="text-ink-900 dark:text-ink-50 text-h2">連結銀行帳號</Text>
              <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
                選擇銀行並設定登入資料以同步帳戶與信用卡
              </Text>
            </View>
          </View>
        </Pressable>

        <Pressable
          onPress={() => router.push('/(tabs)/cards/manual/new')}
          accessibilityRole="button"
          accessibilityLabel="新增手動帳戶"
          className="bg-white dark:bg-ink-900 rounded-2xl p-5 shadow-card active:opacity-80"
          testID="add-manual-account-option"
        >
          <View className="flex-row items-center gap-4">
            <View className="bg-brand-100 dark:bg-brand-950 rounded-xl p-3">
              <WalletCards size={24} color="#9333ea" />
            </View>
            <View className="flex-1">
              <Text className="text-ink-900 dark:text-ink-50 text-h2">新增手動帳戶</Text>
              <Text className="text-ink-500 dark:text-ink-400 text-small mt-1">
                自行維護存款、貸款或投資帳戶
              </Text>
            </View>
          </View>
        </Pressable>
      </View>
    </KeyboardAwareScrollView>
  );
}
