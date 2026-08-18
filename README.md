# ASTRA Link

ASTRA Link is the web-first edition of ASTRA. The assistant and Gemini API key
stay on your Mac; an installable PWA streams microphone audio over one private
WebSocket. A tunnel makes that same app available from a phone, tablet, or
another computer on any network.

This repository intentionally has no Android application, SIP server, carrier
integration, public phone number, or watch calling surface. The original ASTRA
repository is independent and unchanged.

## Architecture

```text
Installed PWA / browser
  microphone (PCM16 16 kHz) + text
             │ encrypted WSS
             ▼
Tailscale Serve (preferred private path) or Cloudflare Tunnel
             │ localhost HTTP/WS
             ▼
ASTRA Link gateway :8080
  ├─ static Next.js export
  ├─ first-message session authentication
  ├─ conservative daily/concurrent quota guard
  ├─ adaptive PCM transport + live link telemetry
  ├─ Gemini Live ↔ ASTRA tools and memory
  └─ authenticated Kitty / Claude Code / Codex workspace API
```

The PWA and WebSocket share one origin. No inbound router port, static Mac IP,
or API key in browser code is required.

## First-time setup

Requirements: Python 3.12+, Node.js/npm, and a Gemini API key.

```bash
./scripts/astra-link setup
openssl rand -hex 32
```

Open `.env`, set `GEMINI_API_KEY` and paste the generated value into
`LINK_SESSION_TOKEN`. The defaults allow one session at a time, ten minutes per
session, and sixty live minutes per day. Then build and run:

```bash
./scripts/astra-link build
./scripts/astra-link serve
```

Open <http://127.0.0.1:8080>, enter the session token in Settings, and start a
conversation. During local web development, run `npm --prefix web run dev` and
keep the gateway on port 8080.

## Reach it from anywhere

For your own phone, Tailscale Serve is the recommended transport. It keeps the
app private to your tailnet and can use a direct WireGuard path instead of
detouring through a public tunnel edge:

```bash
brew install --cask tailscale
./scripts/astra-link tailscale
```

Install/sign in to Tailscale on the phone, then open the HTTPS hostname printed
by the command. Put that origin in `LINK_PUBLIC_URL` (or
`LINK_ALLOWED_ORIGINS`) and restart the gateway. Use
`./scripts/astra-link tailscale-off` to remove the Serve route.

Cloudflare Tunnel remains the recommended public/fallback transport because it
supports WebSockets and the connector makes only outbound connections from the
Mac.

For a temporary URL, keep `serve` running and use a second terminal:

```bash
brew install cloudflared
./scripts/astra-link quick-tunnel
```

Set the printed HTTPS URL as `LINK_PUBLIC_URL` and add it to
`LINK_ALLOWED_ORIGINS`, restart the gateway, then open that URL. Quick tunnels
are for testing: their hostname changes and they have no uptime guarantee.

For a stable hostname:

```bash
cloudflared tunnel login
cloudflared tunnel create astra-link
cloudflared tunnel route dns astra-link astra.example.com
cp config/cloudflared.example.yml config/cloudflared.yml
```

Put the returned tunnel UUID, credential path, and hostname into the ignored
`config/cloudflared.yml`. Set the same `https://astra.example.com` origin in
`.env`, then run these in separate terminals:

```bash
./scripts/astra-link serve
./scripts/astra-link tunnel
```

Install the PWA from the browser's Add to Home Screen/Install action. The Mac
must remain awake, online, and running both processes. For private access among
your own devices, prefer Tailscale Serve; use the named Cloudflare route when a
client cannot join the tailnet.

The Voice surface now uses 40 ms capture frames, adaptive 180–420 ms playback
buffering, bounded send backlog, a screen wake lock, and visible RTT/underrun
telemetry. Audio worklets are network-first so an installed PWA does not keep a
stale audio engine after an update.

## Agent workspace

Choose **Agents** in the same web app to launch visible Claude Code or Codex
tabs in Kitty, inspect their real terminal, prompt or steer them, pause/focus a
tab, and shut down ASTRA's dedicated workspace. Agent routes use the same Link
token and origin allowlist; provider credentials and MCP configuration remain
on the Mac. See [agent workspace](docs/agent-workspace.md).

## Security and quota controls

- `.env`, Cloudflare credentials, local state, build output, and signing files
  are gitignored.
- The Gemini key never enters the PWA. The link token is sent only after WSS is
  established, not in a URL or access log.
- Browser origins are allowlisted with `LINK_ALLOWED_ORIGINS`.
- High-risk ASTRA tools still require confirmation and fail closed without an
  interactive terminal. Consider disabling shell tools for unattended public
  use.
- Rotate `LINK_SESSION_TOKEN` if a device is lost or a token is exposed.
- The `/health` endpoint exposes quota counters but no credentials.

## Commands

```bash
./scripts/astra-link setup
./scripts/astra-link build
./scripts/astra-link serve
./scripts/astra-link quick-tunnel
./scripts/astra-link tunnel
./scripts/astra-link tailscale
./scripts/astra-link tailscale-off
./scripts/astra-link status
```

Run verification with:

```bash
.venv/bin/pytest
npm --prefix web run lint
npm --prefix web run build
```

See [transport options](docs/voice-options.md) for the network tradeoffs.
For complete removal from Android, Chrome, macOS, Cloudflare, and GitHub, see
[uninstalling ASTRA Link](docs/uninstall.md).
