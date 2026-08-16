import type { MetadataRoute } from 'next'

export const dynamic = 'force-static'

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'ASTRA Link',
    short_name: 'ASTRA',
    description: 'A private Gemini Live assistant connected to your Mac.',
    start_url: '/',
    display: 'standalone',
    background_color: '#080a0d',
    theme_color: '#080a0d',
    orientation: 'portrait',
    icons: [
      {
        src: '/icon.svg',
        sizes: 'any',
        type: 'image/svg+xml',
        purpose: 'any',
      },
      {
        src: '/icon.svg',
        sizes: 'any',
        type: 'image/svg+xml',
        purpose: 'maskable',
      },
    ],
  }
}
