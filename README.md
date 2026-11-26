# Quantify Capital — MF Report Portal

Generates the fixed 2-page A4 portfolio backtest report (5-year lumpsum + 60 monthly SIPs vs the
Nifty 50) defined in [system.md](system.md). NAVs are fetched live from mfapi.in on every run.

- `portal.py` / `portal.html`: web portal (form, scheme search, report list)
- `report_engine.py`: validation, NAV fetch, calculations, reconciliation, CLI
- `report_render.py`: report layout (HTML) and PDF printing via headless Chromium/Chrome
- `system.md`: the report rules

Generated PDFs are saved in `reports/`. That folder, `*.pdf` and `.env` are git-ignored, because
they hold client data and secrets. Keep this repository **private**.

## Run on a Windows PC

Requires Python 3.12+ and Google Chrome or Microsoft Edge.

```
pip install -r requirements.txt
```

Double-click `start_portal.bat`, or run `python portal.py --open`, then go to http://localhost:8000.

## Deploy on a Linux server with Docker (recommended)

Requires Docker with the compose plugin, and a domain name pointing at the server.

```bash
git clone <this-repo-url> mf-report-portal
cd mf-report-portal
cp .env.example .env            # then edit .env and set a long random PORTAL_PASSWORD
mkdir -p reports && sudo chown 1000:1000 reports
docker compose up -d --build
curl http://127.0.0.1:8000/healthz   # prints: ok
```

The container listens only on `127.0.0.1:8000`. Put an HTTPS reverse proxy in front of it. The
login is HTTP Basic, so HTTPS is required. With [Caddy](https://caddyserver.com), which obtains
certificates automatically, this is the whole `/etc/caddy/Caddyfile`:

```
reports.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

Open `https://reports.yourdomain.com` and log in with `PORTAL_USER` / `PORTAL_PASSWORD`.

To update after pushing new code: `git pull && docker compose up -d --build`.
Reports persist in `./reports` on the server; back that folder up.

## Deploy without Docker (Ubuntu 24.04)

```bash
sudo apt install -y python3-venv fonts-inter fonts-dejavu-core
# Install Google Chrome from the .deb at https://www.google.com/chrome/ (the Ubuntu chromium
# snap cannot read the temporary files the renderer writes), or set CHROME_PATH to a browser.
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Example systemd unit (`/etc/systemd/system/mf-report-portal.service`):

```ini
[Unit]
Description=MF Report Portal
After=network-online.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/mf-report-portal
Environment=TZ=Asia/Kolkata
EnvironmentFile=/home/youruser/mf-report-portal/.env
ExecStart=/home/youruser/mf-report-portal/.venv/bin/python portal.py --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl enable --now mf-report-portal` and add the same Caddy reverse proxy.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORTAL_PASSWORD` | (none) | Turns on login for every page. Required to listen on a non-local address. |
| `PORTAL_USER` | `admin` | Login user name |
| `PORTAL_HOST` | `127.0.0.1` | Address to listen on (`0.0.0.0` inside Docker) |
| `PORT` | `8000` | Port |
| `TZ` | server zone | Set `Asia/Kolkata` so "today", the report date and the 5-year window follow IST |
| `CHROME_PATH` | auto-detect | Path to Chrome/Chromium/Edge if it isn't found automatically |
| `CHROME_NO_SANDBOX` | `0` | `1` inside containers (set in the Dockerfile) |

`GET /healthz` returns `ok` without login, for uptime checks.

## Command line

```
python report_engine.py client.json
```

See `input_template.json` for the format. A fund with under 5 years of NAV history needs
`"expected_return_pct"`. The command prints the three result lines and saves the PDF in `reports/`.
