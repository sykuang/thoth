import { type Href, useRouter } from 'expo-router';
import { Pressable, Text } from 'react-native';

export function DeterministicBackButton({
  target,
  label,
}: {
  target: Href;
  label: string;
}) {
  const router = useRouter();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`返回${label}`}
      onPress={() => router.dismissTo(target)}
      className="py-2 pr-3"
      testID={`back-to-${label}`}
    >
      <Text className="text-white text-body">‹ {label}</Text>
    </Pressable>
  );
}
