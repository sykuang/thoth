import { Link, Stack } from 'expo-router';
import { Pressable, Text, View } from 'react-native';

import { ROUTE_PARENTS } from '@/lib/routeParents';

export default function NotFoundScreen() {
  return (
    <>
      <Stack.Screen options={{ title: '找不到頁面' }} />
      <View className="flex-1 items-center justify-center bg-ink-50 dark:bg-ink-950 px-6">
        <Text className="text-h1 text-ink-900 dark:text-ink-50 mb-2">找不到頁面</Text>
        <Text className="text-body text-ink-500 dark:text-ink-400 text-center mb-6">
          這個連結不存在或已經失效。
        </Text>
        <Link href={ROUTE_PARENTS['+not-found']} asChild>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="返回首頁"
            className="bg-brand-600 active:bg-brand-700 rounded-xl px-5 py-3"
          >
            <Text className="text-white text-h3">返回首頁</Text>
          </Pressable>
        </Link>
      </View>
    </>
  );
}
