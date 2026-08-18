# ASTRA Link transport options

ASTRA Link carries live audio as encrypted web traffic. There is no telephone
number or per-minute telephony bill.

| Transport | Cost | Public URL | Best use |
|---|---:|---|---|
| Localhost/LAN | Free | No | Development on the Mac or trusted LAN |
| Cloudflare named tunnel | Free plan available | Stable custom hostname | Public/fallback remote access |
| Cloudflare quick tunnel | Free | Temporary random hostname | Short tests only |
| Tailscale Serve | Free personal plan available | Tailnet-only hostname | Recommended for your own phone and Mac |
| Tailscale Funnel | Free personal plan available | Public `ts.net` hostname | Simple public alternative with bandwidth limits |
| ngrok free | Free but metered | Public hostname | Debugging; its free transfer cap is poor for continuous audio |

The gateway is tunnel-provider-neutral: any service that preserves HTTPS and
WebSocket upgrades can forward to `http://127.0.0.1:8080`. Tailscale Serve is
the default recommendation for Aaloke's own phone because it can establish a
direct encrypted peer-to-peer route. Cloudflare remains the better fallback
when a device cannot join the tailnet or the URL must be public.

The v2 audio transport uses 40 ms input frames, disables WebSocket compression
for raw PCM, bounds browser send backlog, keeps the Android screen awake during
voice, and adapts the playback buffer from 180–420 ms when tunnel jitter causes
an underrun. The Voice page reports round-trip time, target buffer, recovered
gaps, and whether input frames were dropped to avoid multi-second lag.

This is jitter protection, not packet recovery: WebSockets are ordered and
reliable, so the audible blocks were generally late playback frames rather
than missing UDP packets. If Tailscale reports a relayed connection instead of
a direct one, Cloudflare can be comparable; test both from the actual mobile
network.

Whatever transport you choose, keep origin allowlisting and the ASTRA Link
session token enabled. A tunnel provides reachability and TLS; it does not by
itself authorize an assistant session.

Official references:

- [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/)
- [Cloudflare WebSockets](https://developers.cloudflare.com/network/websockets/)
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
- [ngrok free limits](https://ngrok.com/docs/pricing-limits/free-plan-limits)
