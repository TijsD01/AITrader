// Rebuild src/web/static/tailwind.css after changing this file or adding classes (see docs/frontend.md).
module.exports = {
  content: ['./src/web/static/index.html', './src/web/static/app.js', './src/web/static/features.js'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"Inter"', 'system-ui', '"Segoe UI"', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace']
      },
      colors: {
        // Design tokens (see style.css :root). Every colour reads a CSS variable,
        // so existing class names follow the light/dark theme.
        accent: { DEFAULT: 'rgb(var(--rgb-accent) / <alpha-value>)', strong: 'rgb(var(--rgb-accent-strong) / <alpha-value>)', soft: 'rgb(var(--rgb-accent-soft) / <alpha-value>)' },
        gain: 'rgb(var(--rgb-gain) / <alpha-value>)',
        loss: 'rgb(var(--rgb-loss) / <alpha-value>)',
        warn: 'rgb(var(--rgb-warn) / <alpha-value>)',
        info: 'rgb(var(--rgb-info) / <alpha-value>)',
        violet: { 300: 'rgb(var(--rgb-violet) / <alpha-value>)' },
        pink: { 300: 'rgb(var(--rgb-pink) / <alpha-value>)' },
        darkBg: 'rgb(var(--rgb-bg) / <alpha-value>)',
        cardBg: 'rgb(var(--rgb-surface) / <alpha-value>)',
        borderCol: 'rgb(var(--rgb-line) / <alpha-value>)',
        slate: {
          50: 'rgb(var(--rgb-slate-50) / <alpha-value>)', 100: 'rgb(var(--rgb-slate-100) / <alpha-value>)', 200: 'rgb(var(--rgb-slate-200) / <alpha-value>)', 300: 'rgb(var(--rgb-slate-300) / <alpha-value>)', 400: 'rgb(var(--rgb-slate-400) / <alpha-value>)', 500: 'rgb(var(--rgb-slate-500) / <alpha-value>)', 600: 'rgb(var(--rgb-slate-600) / <alpha-value>)', 700: 'rgb(var(--rgb-slate-700) / <alpha-value>)', 800: 'rgb(var(--rgb-slate-800) / <alpha-value>)', 900: 'rgb(var(--rgb-slate-900) / <alpha-value>)', 950: 'rgb(var(--rgb-slate-950) / <alpha-value>)'
        }
      }
    }
  }
};
