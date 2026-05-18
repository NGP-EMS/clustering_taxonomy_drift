/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        obs: {
          void:     '#02050a',
          deep:     '#03080f',
          navy:     '#060d1a',
          surface:  '#0a1628',
          elevated: '#0f1f38',
          card:     '#111d33',
          border:   '#1a2d4a',
          muted:    '#1e3450',
        },
        cyan:    { DEFAULT: '#00d4ff', dim: '#0099bb' },
        violet:  { DEFAULT: '#7c3aed', bright: '#a855f7' },
        magenta: { DEFAULT: '#e879f9', dim: '#c026d3' },
        emerald: { DEFAULT: '#10b981' },
        anomaly: { DEFAULT: '#ef4444', warm: '#f97316' },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"Cascadia Code"', '"Fira Code"', 'Consolas', 'monospace'],
      },
      boxShadow: {
        'glow-cyan':    '0 0 20px rgba(0,212,255,0.3), 0 0 60px rgba(0,212,255,0.1)',
        'glow-violet':  '0 0 20px rgba(168,85,247,0.3), 0 0 60px rgba(168,85,247,0.1)',
        'glow-anomaly': '0 0 20px rgba(239,68,68,0.4)',
        'obs-card':     '0 4px 24px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.04)',
      },
      animation: {
        'pulse-glow': 'pulseGlow 2s ease-in-out infinite',
        'float':      'float 6s ease-in-out infinite',
        'fade-up':    'fadeUp 0.35s ease-out',
      },
      keyframes: {
        pulseGlow: {
          '0%,100%': { opacity: '0.5' },
          '50%':     { opacity: '1'   },
        },
        float: {
          '0%,100%': { transform: 'translateY(0px)'  },
          '50%':     { transform: 'translateY(-5px)' },
        },
        fadeUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}
