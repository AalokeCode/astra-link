import type { Metadata, Viewport } from 'next'

import ServiceWorkerRegistration from '@/components/service-worker-registration'

import './globals.css'

export const metadata: Metadata = {
  applicationName: 'ASTRA Link',
  title: 'ASTRA Link — Live Assistant',
  description: 'A private, installable Gemini Live assistant connected to your Mac.',
  appleWebApp: {
    capable: true,
    statusBarStyle: 'black-translucent',
    title: 'ASTRA Link',
  },
}

export const viewport: Viewport = {
  themeColor: '#080a0d',
  colorScheme: 'dark',
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        {children}
        <ServiceWorkerRegistration />
      </body>
    </html>
  )
}
