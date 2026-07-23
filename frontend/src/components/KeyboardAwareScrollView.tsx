/**
 * KeyboardAwareScrollView — iOS 鍵盤自動 inset 處理 + 點空白收鍵盤.
 *
 * 用 RN 0.73+ 內建 `automaticallyAdjustKeyboardInsets={true}`,
 * iOS 鍵盤彈出時 ScrollView contentInset 自動推上來 — 不需要 KeyboardAvoidingView
 * 那套老 hack (KAV 在 RN 0.73+ 已 deprecated approach).
 *
 * `keyboardShouldPersistTaps="handled"`: user 點別的地方鍵盤收起來,但第一次 tap
 * 到按鈕仍會觸發 (避免「按 button 沒反應,要按兩下」反 UX).
 *
 * `keyboardDismissMode="interactive"` (iOS): 向下滑動時鍵盤跟著縮 (Apple HIG style).
 *
 * Web / Android 都不影響 (這幾個 props 在那裡是 no-op / fallback 預設).
 *
 * 用法: 把所有 form / list page 的最外層 ScrollView 換成這個就行.
 *
 * 起源: 2026-06-16 使用者「iOS 輸入密碼的時候 鍵盤會遮住密碼」.
 * 詳 wiki [[ios-keyboard-hide-input-rn-scrollview-fix]].
 */
import { Platform, ScrollView, type ScrollViewProps } from 'react-native';

export function KeyboardAwareScrollView(props: ScrollViewProps) {
  return (
    <ScrollView
      automaticallyAdjustKeyboardInsets
      keyboardShouldPersistTaps="handled"
      keyboardDismissMode={Platform.OS === 'ios' ? 'interactive' : 'on-drag'}
      {...props}
    />
  );
}
