# Completely remove ASTRA Link

This removes the installed phone PWA/WebAPK, browser data, Mac gateway and
runtime state, tunnel, credentials, and optionally the source/GitHub
repository. It does not remove the separate original ASTRA project.

## 1. Remove it from Android

Long-press the **ASTRA** icon, open **App info**, and choose **Uninstall**. A
Chrome-installed PWA may have a generated package name beginning with
`org.chromium.webapk`; uninstall it through the icon instead of guessing the
package ID.

Then clear any retained site permissions and storage in Chrome:

1. Open **Chrome → Settings → Site settings → All sites**.
2. Select the ASTRA Link hostname (or `127.0.0.1` used during USB testing).
3. Choose **Clear & reset**.

If USB port forwarding was used for installation, remove only ASTRA Link's
mapping:

```bash
adb reverse --remove tcp:8081
```

Do not use `adb reverse --remove-all` if other development apps use reverse
ports.

## 2. Stop the Mac gateway and tunnel

Press `Ctrl-C` in the terminals running `./scripts/astra-link serve` and
`./scripts/astra-link tunnel` or `quick-tunnel`. Confirm the local listeners
are gone:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
lsof -nP -iTCP:8081 -sTCP:LISTEN
```

Inspect any remaining PID and its command before terminating it. Port 8080 may
belong to the separate original ASTRA project.

## 3. Delete a persistent public route

A quick tunnel disappears when `cloudflared` stops and has no persistent DNS
record. For a named Cloudflare Tunnel:

1. Delete its public hostname/CNAME in the Cloudflare dashboard. DNS records
   and tunnels are independent.
2. Confirm that no connector is active with `cloudflared tunnel info astra-link`.
3. Review the exact target with `cloudflared tunnel list`.
4. Delete it with `cloudflared tunnel delete astra-link`.
5. Remove this repository's ignored `config/cloudflared.yml` and only the
   matching tunnel credential JSON from `~/.cloudflared`.

If Tailscale was used instead, inspect `tailscale serve status`, then disable
the applicable route with `tailscale serve off` or `tailscale funnel off`.

## 4. Remove Mac data and source

Move `~/.astra-link` to the Trash. It contains ASTRA Link's conversations,
logs, quota state, and model cache.

Move the `astra-link` repository directory to the Trash. This also removes its
ignored `.env`, `.venv`, `node_modules`, exported PWA, tunnel configuration,
tests, and source. Do not remove the parent Projects directory or the original
ASTRA checkout.

Empty the Trash only after confirming that no memory or configuration needs to
be recovered.

## 5. Revoke credentials and remote copies

- Delete or rotate the Gemini and Groq keys formerly stored in `.env`.
- Rotate `LINK_SESSION_TOKEN` if the service will continue elsewhere.
- Remove any Cloudflare Access policy or API token created only for ASTRA Link.
- Optionally delete `AalokeCode/astra-link` from GitHub under
  **Settings → Danger Zone**. Deleting the Mac checkout does not delete GitHub.

## 6. Final verification

ASTRA Link is fully removed when its Android icon and Chrome site data are
gone, no ASTRA Link gateway/tunnel process is listening, `~/.astra-link` and
the checkout are absent, and its API keys, DNS entries, and tunnels have been
revoked or deleted.

Cloudflare's current tunnel deletion command is documented in its
[official command reference](https://developers.cloudflare.com/tunnel/advanced/local-management/tunnel-useful-commands/).
