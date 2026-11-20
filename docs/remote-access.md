# Viewing the dashboards from elsewhere

Out of the box, Grafana listens on `127.0.0.1:3300` of the monitoring machine and
nothing else is reachable. This page lists ways to open it up and what to watch out for.
Pick one way to **reach** Grafana and one way to **log in**; the two are independent.

## Before you open anything: what is on the page

* Every user's name, what they run (job command lines) and how much CPU, RAM and GPU
  they use, over the whole retention period (2 years by default).
* The agent masks secret-looking command-line arguments (`--token X`, `API_KEY=…`,
  `user:password@` URLs, well-known key formats; `SECRET_RES` in `servmon/agent.py`).
  Everything else is shown as is: paths, project and dataset names, hostnames.
* Anyone who can view a dashboard can run *any* query against the data source through
  Grafana's API. Hiding a panel or a dashboard does not hide the data, so treat "can log
  in" as "can see everything".

Decide who should see this and tell the people being monitored.

## Reaching Grafana

| Option | Exposure | You need | Notes |
|---|---|---|---|
| **SSH port forward** | none | SSH access to the monitoring machine | `ssh -L 3300:127.0.0.1:3300 <monitoring-machine>`, then open <http://localhost:3300>. The simplest option; best for a handful of people who already have accounts. |
| **Private network** (VPN, Tailscale/Headscale, WireGuard, campus VPN) | only to that network | the network, and the machine on it | Set `BIND_ADDR` in `.env` to the machine's address *on that network* (not `0.0.0.0`) and re-run `scripts/podman-install.sh`. |
| **Your own domain + reverse proxy** (Caddy, nginx, Traefik) | public | a host reachable from the internet, DNS, a TLS certificate | The proxy terminates HTTPS and forwards to `127.0.0.1:3300`. University networks often block inbound traffic to desktops, so this usually runs on a server or VPS. |
| **Outbound tunnel** (Cloudflare Tunnel, ngrok, frp/rathole to a VPS, Tailscale Funnel) | public | an account with the tunnel provider (or a VPS) | No inbound port: a connector on this machine dials out. Many providers can also require a login before traffic reaches you (see C below). For a Cloudflare Tunnel, set `TUNNEL_TOKEN` in `.env`; the installer then adds a `cloudflared` container to the pod, and the tunnel's origin is `http://localhost:3300`. |

Whatever you choose, expose **only Grafana**. Prometheus (`:9390`) and the collector
(`:9877`) have no authentication, and Prometheus runs with its admin API on (it can delete
data). Both stay bound to `127.0.0.1`.

## Logging in

Settings go in `deploy/grafana/grafana.env` (git-ignored; recipes in
`deploy/grafana/grafana.env.example`). Re-run `scripts/podman-install.sh` afterwards.

**A. Grafana accounts.** Turn off anonymous access and create accounts under
*Administration → Users*: one per person, or one shared **Viewer** account.
* A shared account is easy to hand out, but anyone holding it can change its password
  (locking everyone else out) and its preferences. Five wrong passwords in a row lock it
  for a few minutes. Change the password when someone leaves.
* Per-person accounts avoid all of this, but you have to create them.

**B. Grafana's own single sign-on.** Google, GitHub, GitLab, Microsoft or any OIDC provider,
restricted to an email domain, organisation or group. Each person gets their own account
automatically (as Viewer). Configured entirely in Grafana; see Grafana's documentation for
your provider.

**C. Login at an identity-aware proxy.** The tunnel provider or reverse proxy authenticates
people (e.g. oauth2-proxy, Authelia, or the access controls many tunnel providers offer)
before any request reaches this machine. You can stop there and pair it with A. Or, if the
proxy sends a signed identity token (JWT), Grafana can verify it and sign each person in
automatically under their own email (recipe C in `grafana.env.example`). Only trust such a
header if Grafana is reachable *exclusively* through that proxy.

## Checklist for a public URL

- [ ] HTTPS only. Set `GF_SERVER_ROOT_URL` to the public URL and `GF_SECURITY_COOKIE_SECURE=true`.
- [ ] `GF_AUTH_ANONYMOUS_ENABLED=false`. The default config gives anonymous visitors
      *Editor* rights, which is fine on `127.0.0.1`, not on the internet.
- [ ] The `admin` password is changed, and nobody else is Admin.
- [ ] Everyone else is a **Viewer** (the default for new accounts).
- [ ] Prefer two layers: a login at the edge (C) *and* a Grafana login (A or B).
      Then one misconfiguration doesn't expose everything.
- [ ] Only port 3300 is reachable from outside, and only through your chosen path.
- [ ] Images are kept up to date (bump the tags in `scripts/podman-install.sh` /
      `compose.yaml`) for Grafana security fixes.
- [ ] You know how to turn it off: remove the tunnel or proxy route (or set `BIND_ADDR`
      back to `127.0.0.1`) and re-run the installer.
