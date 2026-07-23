/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{js,jsx,ts,tsx}'],
  presets: [require('nativewind/preset')],
  // dark mode 由 nativewind useColorScheme() 控制 → class strategy 兼容 web + native
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 主色：紫色 (fintech-feel, 跟銀行 generic blue 拉開距離)
        brand: {
          50: '#faf5ff',
          100: '#f3e8ff',
          200: '#e9d5ff',
          300: '#d8b4fe',
          400: '#c084fc',
          500: '#a855f7',
          600: '#9333ea',   // primary
          700: '#7e22ce',   // primary-active
          800: '#6b21a8',
          900: '#581c87',
          950: '#3b0764',
        },
        // accent: 用 emerald 表示「成功 / has_creds」, amber 警示
        accent: {
          500: '#10b981',
          600: '#059669',
        },
        // 中性色階, 自己定義避免 fluctuating Tailwind defaults
        ink: {
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#020617',
        },
      },
      fontSize: {
        // 字級 hierarchy 拉開, 中文字級偏大讓 weight=600 還能看
        'display': ['32px', { lineHeight: '40px', fontWeight: '700' }],
        'h1':      ['24px', { lineHeight: '32px', fontWeight: '700' }],
        'h2':      ['18px', { lineHeight: '26px', fontWeight: '600' }],
        'h3':      ['15px', { lineHeight: '22px', fontWeight: '600' }],
        'body':    ['14px', { lineHeight: '20px' }],
        'small':   ['12px', { lineHeight: '17px' }],
        'micro':   ['11px', { lineHeight: '15px' }],
      },
      // 陰影系統 — fintech 風常用 colored shadow
      boxShadow: {
        'soft':   '0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04)',
        'card':   '0 4px 12px rgba(15, 23, 42, 0.06)',
        'pop':    '0 12px 32px rgba(15, 23, 42, 0.10)',
        'brand':  '0 8px 24px rgba(147, 51, 234, 0.25)',
      },
    },
  },
  plugins: [],
};
