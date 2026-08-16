import type { Metadata } from 'next';
import { Fira_Code, Inter } from 'next/font/google';
import Link from 'next/link';
import './globals.css';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
});

// Fira Code carries every number a clinician reads as a measurement. The
// point is that a risk score should look like instrument output, not like
// body copy.
const firaCode = Fira_Code({
  subsets: ['latin'],
  variable: '--font-fira-code',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Traycer',
  description:
    'Passive behavioral biomarkers for early detection of psychiatric relapse. Clinician dashboard and sensing SDK.',
  robots: { index: true, follow: true },
};

const NAV = [
  { href: '/dashboard', label: 'Panel' },
  { href: '/patients', label: 'Patients' },
  { href: '/alerts', label: 'Alerts' },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${firaCode.variable}`}>
      <body className="min-h-screen bg-void font-sans text-text-primary antialiased">
        <div className="flex min-h-screen flex-col">
          <header className="border-b border-line">
            <div className="mx-auto flex h-12 w-full max-w-[1400px] items-center justify-between px-6">
              <div className="flex items-center gap-8">
                <Link
                  href="/"
                  className="font-mono text-sm tracking-[0.2em] text-text-primary hover:text-indigo"
                >
                  TRAYCER
                </Link>
                <nav className="flex items-center gap-6">
                  {NAV.map((item) => (
                    <Link
                      key={item.href}
                      href={item.href}
                      className="text-[13px] text-text-secondary transition-colors hover:text-text-primary"
                    >
                      {item.label}
                    </Link>
                  ))}
                </nav>
              </div>

              <Link
                href="/login"
                className="font-mono text-[11px] uppercase tracking-[0.14em] text-text-muted hover:text-text-primary"
              >
                Sign in with EHR
              </Link>
            </div>
          </header>

          <main className="flex-1">{children}</main>

          <footer className="border-t border-line">
            <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-1 px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-[11px] text-text-muted">
                Traycer is a research and monitoring tool. It does not diagnose, and it does not
                replace clinical judgement.
              </p>
              <a
                href="https://github.com/Henrycarto/traycer"
                className="font-mono text-[11px] text-text-muted hover:text-text-secondary"
              >
                github.com/Henrycarto/traycer
              </a>
            </div>
          </footer>
        </div>
      </body>
    </html>
  );
}
