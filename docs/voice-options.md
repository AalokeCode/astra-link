# ASTRA Link transport options

ASTRA Link carries live audio as encrypted web traffic. There is no telephone
number or per-minute telephony bill.

| Transport | Cost | Public URL | Best use |
|---|---:|---|---|
| Localhost/LAN | Free | No | Development on the Mac or trusted LAN |
| Cloudflare named tunnel | Free plan available | Stable custom hostname | Recommended daily remote access |
| Cloudflare quick tunnel | Free | Temporary random hostname | Short tests only |
| Tailscale Serve | Free personal plan available | Tailnet-only hostname | Most private access across your own devices |
| Tailscale Funnel | Free personal plan available | Public `ts.net` hostname | Simple public alternative with bandwidth limits |
| ngrok free | Free but metered | Public hostname | Debugging; its free transfer cap is poor for continuous audio |

The gateway is tunnel-provider-neutral: any service that preserves HTTPS and
WebSocket upgrades can forward to `http://127.0.0.1:8080`. Cloudflare is the
default because its connector is outbound-only, handles WebSockets, and avoids
opening a router port. Tailscale Serve is preferable when every client can join
the same tailnet and public access is unnecessary.

Whatever transport you choose, keep origin allowlisting and the ASTRA Link
session token enabled. A tunnel provides reachability and TLS; it does not by
itself authorize an assistant session.

Official references:

- [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/)
- [Cloudflare WebSockets](https://developers.cloudflare.com/network/websockets/)
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
- [ngrok free limits](https://ngrok.com/docs/pricing-limits/free-plan-limits)
