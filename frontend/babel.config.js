module.exports = function (api) {
  api.cache(true);
  return {
    presets: [
      // NativeWind v4.2.5 把 babel preset 移到 react-native-css-interop。
      // jsxImportSource 也跟著改成 react-native-css-interop (不是 nativewind)。
      ['babel-preset-expo', { jsxImportSource: 'react-native-css-interop' }],
      'react-native-css-interop/babel',
    ],
  };
};
