import { useLayoutEffect, useRef, useState } from 'react';
import { Platform, Text, TextInput, View, useWindowDimensions, type TextInputProps } from 'react-native';

type Props = Pick<TextInputProps, 'value' | 'onChangeText' | 'className' | 'testID' | 'placeholder'>;

export function RegexPatternInput(props: Props) {
  const { fontScale, width } = useWindowDimensions();
  const [contentHeight, setContentHeight] = useState(0);
  const inputRef = useRef<TextInput>(null);
  // text-body line height; reserve vertical padding and both borders.
  const minHeight = 4 * 20 * fontScale + 22;
  const maxHeight = 8 * 20 * fontScale + 22;
  useLayoutEffect(() => {
    if (Platform.OS !== 'web' || !inputRef.current) return;
    const input = inputRef.current as unknown as HTMLTextAreaElement;
    // Measure unconstrained content after both edits and controlled-value resets.
    input.style.height = 'auto';
    input.style.height = `${Math.min(maxHeight, Math.max(minHeight, input.scrollHeight + 2))}px`;
  }, [props.value, minHeight, maxHeight, width]);
  return (
    <View>
      <Text className="text-ink-700 dark:text-ink-300 text-small font-semibold mb-2">
        比對規則（Regex）
      </Text>
      <TextInput
        {...props}
        ref={inputRef}
        accessibilityLabel="比對規則（Regex）"
        placeholderTextColor="#94a3b8"
        multiline
        style={{ height: Platform.OS === 'web' ? minHeight : Math.min(maxHeight, Math.max(minHeight, contentHeight)) }}
        onContentSizeChange={Platform.OS === 'web' ? undefined : (event) => setContentHeight(event.nativeEvent.contentSize.height + 2)}
        scrollEnabled={Platform.OS === 'web' || contentHeight > maxHeight}
        textAlignVertical="top"
        submitBehavior="newline"
        autoCapitalize="none"
        autoCorrect={false}
        spellCheck={false}
        smartInsertDelete={false}
      />
    </View>
  );
}
