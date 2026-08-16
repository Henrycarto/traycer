import type { Config } from 'tailwindcss';

/**
 * The palette is fixed and small. Five state colors, two surfaces, one accent.
 * A monitoring interface that a clinician reads in three seconds cannot
 * afford a colour that means nothing, so there are no decorative shades here.
 */
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        void: '#060910',
        surface: '#0C1220',
        // One step up from surface, for rows and inner panels. Not a shadow,
        // not a card. Just a slightly lighter plane.
        raised: '#111A2E',
        line: '#1B2436',
        indigo: {
          DEFAULT: '#6366F1',
          dim: '#4B4ECF',
          faint: 'rgba(99, 102, 241, 0.14)',
        },
        alert: {
          high: '#DC2626',
          medium: '#D97706',
        },
        stable: '#4ADE80',
        inactive: '#374151',
        text: {
          primary: '#E5E9F0',
          secondary: '#94A3B8',
          muted: '#64748B',
        },
      },
      fontFamily: {
        // Inter for prose and labels. Fira Code for every number a clinician
        // reads as a measurement, because a monitoring readout should look
        // like a readout.
        sans: ['var(--font-inter)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-fira-code)', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        readout: ['3.25rem', { lineHeight: '1', letterSpacing: '-0.02em' }],
      },
      borderRadius: {
        none: '0',
        sm: '2px',
        DEFAULT: '2px',
        md: '3px',
      },
      spacing: {
        row: '2.75rem',
      },
    },
  },
  plugins: [],
};

export default config;
