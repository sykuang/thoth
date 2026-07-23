/**
 * App-wide ErrorBoundary.
 *
 * Catches uncaught render-time errors anywhere below it in the tree and shows
 * a graceful fallback UI instead of a white screen of death. Wrap the whole
 * navigation stack to keep errors from one page from killing the entire app.
 *
 * Why class component: React 18 + RN 0.76 still only allow ErrorBoundary via
 * componentDidCatch / getDerivedStateFromError, which exist only on classes.
 * There is no Hooks equivalent yet.
 *
 * Notes:
 *  - Only catches RENDER / LIFECYCLE errors. Async errors (Promise rejections,
 *    setTimeout, fetch failures) are NOT caught — those go to global handlers.
 *  - In dev (__DEV__) we still call console.error so RedBox shows up too.
 *  - 'Reset' rerenders by forcing a fresh key (the user can then retry the
 *    action that crashed without quitting the app).
 *
 * Phase 11 (W 2026-06-17 使用者指示): added as code-review item.
 */
import React, { type ErrorInfo, type ReactNode } from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';

type Props = {
  /** UI rendered when there is NO error (normal path). */
  children: ReactNode;
  /** Optional override fallback. If omitted, the default fallback UI is shown. */
  fallback?: (err: Error, reset: () => void) => ReactNode;
};

type State = {
  error: Error | null;
  /** Bump on reset() so children remount on next render. */
  resetKey: number;
};

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null, resetKey: 0 };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep dev-time RedBox; in prod this still logs to JS console / RN logbox.
    if (__DEV__) {
       
      console.error('[ErrorBoundary]', error, info.componentStack);
    }
    // Hook for future: send to Sentry / Crashlytics here.
  }

  reset = () => {
    this.setState((s) => ({ error: null, resetKey: s.resetKey + 1 }));
  };

  render() {
    const { error, resetKey } = this.state;
    if (error) {
      if (this.props.fallback) return this.props.fallback(error, this.reset);
      return <DefaultFallback error={error} onReset={this.reset} />;
    }
    return (
      <React.Fragment key={resetKey}>{this.props.children}</React.Fragment>
    );
  }
}

function DefaultFallback({ error, onReset }: { error: Error; onReset: () => void }) {
  return (
    <View className="flex-1 bg-white dark:bg-ink-950 items-center justify-center px-8">
      <View className="w-full max-w-md">
        <Text className="text-h2 font-bold text-ink-900 dark:text-ink-50 mb-3 text-center">
          發生未預期錯誤
        </Text>
        <Text className="text-small text-ink-500 dark:text-ink-400 mb-6 text-center">
          應用程式遇到問題。可嘗試重置畫面，若持續發生請回報以下訊息。
        </Text>

        <ScrollView
          className="bg-ink-50 dark:bg-ink-900 rounded-xl p-4 mb-6 max-h-40 border border-ink-200 dark:border-ink-700"
          showsVerticalScrollIndicator
        >
          <Text className="text-micro font-mono text-red-700 dark:text-red-400">
            {error.name}: {error.message}
          </Text>
          {error.stack && (
            <Text className="text-micro font-mono text-ink-500 dark:text-ink-400 mt-2">
              {error.stack.split('\n').slice(0, 8).join('\n')}
            </Text>
          )}
        </ScrollView>

        <Pressable
          onPress={onReset}
          className="bg-brand-600 active:bg-brand-500 py-3 rounded-xl"
          testID="error-boundary-reset"
        >
          <Text className="text-white text-small font-semibold text-center">
            重置畫面
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
