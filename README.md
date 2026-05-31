# SMTP Relay

A self-hosted SMTP relay that accepts email on ports **25** (legacy SMTP), **465** (SMTPS), and **587** (STARTTLS) and forwards it to recipients via the **Microsoft Graph API**. The web UI (port 5000) lets admins manage SMTP credentials, view sent emails, and configure the relay.

---

## Features

- SMTP relay on ports 25 (legacy), 465 (SMTPS), and 587 (STARTTLS), AUTH PLAIN + LOGIN
- Per-credential allow-lists for senders and recipients with wildcard support (`*@example.com`)
- Credentials can be activated / deactivated without deletion
- Emails forwarded via Microsoft Graph API (attachments up to **150 MB** each via upload sessions)
- Email log with download as `.eml`, configurable retention (default: 30 days)
- Web UI login via Microsoft Entra (OIDC) — first user gets admin automatically
- Self-signed TLS certificate generated on start, renewed automatically when < 90 days remain
- All settings in a single SQLite database; no other data store required
- Docker Compose deployment, all configuration via environment variables

---

## Prerequisites

1. Docker and Docker Compose
2. A Microsoft Entra (Azure AD) tenant with an **App Registration** configured as described below

---

## Entra App Registration setup

### 1. Create the App Registration

1. Open the [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**
2. Name: e.g. `smtp-relay`
3. Supported account types: **Accounts in this organizational directory only** (single tenant)
4. Redirect URI: `Web` → `https://<your-host>:5000/auth/callback`
   (For local testing use `http://localhost:5000/auth/callback`)
5. Click **Register**

### 2. Note the IDs

From the Overview page copy:
- **Application (client) ID** → `ENTRA_CLIENT_ID`
- **Directory (tenant) ID** → `ENTRA_TENANT_ID`

### 3. Create a client secret

Go to **Certificates & secrets** → **New client secret**, set an expiry, copy the **Value** → `ENTRA_CLIENT_SECRET`

### 4. Grant API permissions

The same App Registration is used for two flows:

| Flow | Permission type | Used for |
|---|---|---|
| SMTP → Graph send | **Application** | Client-credentials mail send (no signed-in user) |
| Web UI login | **Delegated** | Interactive OIDC sign-in |

Go to **API permissions** → **Add a permission** → **Microsoft Graph** and add exactly these permissions:

**Application permissions** (admin consent required):

| Permission | Why |
|---|---|
| `Mail.Send` | Send draft messages on behalf of mailboxes |
| `Mail.ReadWrite` | Create drafts, add attachments, clean up Sent Items |

**Delegated permissions** (for web UI login — `User.Read` may already be present by default):

| Permission | Why |
|---|---|
| `User.Read` | Sign in and read the signed-in user's profile (`openid email profile`) |

Click **Grant admin consent for \<your tenant\>** for the **Application** permissions.

### 5. Restrict mailbox access (Exchange Application Access Policy)

`Mail.Send` and `Mail.ReadWrite` are tenant-wide in Entra ID. Complement them with an **Application Access Policy** in Exchange Online so the app can only reach the sender mailboxes your relay uses.

> Microsoft is replacing Application Access Policies with [RBAC for Applications](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac). The commands below are for the legacy policy model; use RBAC for Applications if you are setting up a new tenant and prefer the current approach.

1. Put every mailbox the relay may send as into a **mail-enabled security group** (create one if needed).
2. Connect to Exchange Online PowerShell and create a **RestrictAccess** policy scoped to that group.
3. Test access for allowed and denied mailboxes.

Replace the placeholders:

- `<CLIENT_ID>` — Application (client) ID from step 2
- `<GROUP_SMTP>` — SMTP address of the mail-enabled security group
- `<ALLOWED_SENDER>` / `<OTHER_MAILBOX>` — mailbox addresses to test

```powershell
# Install once (PowerShell 7+ recommended)
Install-Module ExchangeOnlineManagement -Scope CurrentUser

Connect-ExchangeOnline

# Optional: create a mail-enabled security group and add sender mailboxes
New-DistributionGroup `
  -Name "SMTP Relay Senders" `
  -Type Security `
  -PrimarySmtpAddress "smtp-relay-senders@example.com"

Add-DistributionGroupMember -Identity "smtp-relay-senders@example.com" -Member "noreply@example.com"
Add-DistributionGroupMember -Identity "smtp-relay-senders@example.com" -Member "alerts@example.com"

# Restrict the app to mailboxes in the group only
New-ApplicationAccessPolicy `
  -AppId "<CLIENT_ID>" `
  -PolicyScopeGroupId "smtp-relay-senders@example.com" `
  -AccessRight RestrictAccess `
  -Description "SMTP relay — allowed sender mailboxes only"

# Verify: AccessCheckResult should be Granted for allowed senders, Denied for others
Test-ApplicationAccessPolicy -Identity "noreply@example.com" -AppId "<CLIENT_ID>"
Test-ApplicationAccessPolicy -Identity "ceo@example.com" -AppId "<CLIENT_ID>"

# List or remove policies later
Get-ApplicationAccessPolicy | Format-List
# Remove-ApplicationAccessPolicy -Identity "<policy-id>" -Confirm:$false

Disconnect-ExchangeOnline -Confirm:$false
```

Policy changes can take **up to an hour** to apply to Microsoft Graph, even when `Test-ApplicationAccessPolicy` already returns the expected result.

Keep the group's membership in sync with the **allowed senders** you configure on SMTP credentials in the web UI.

---

## Configuration

Copy the example files and adjust them for your environment:

```bash
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

Fill in `.env`:

```env
ENTRA_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ENTRA_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ENTRA_CLIENT_SECRET=your-client-secret-value

SECRET_KEY=<random 32+ character string>

SMTP_HOSTNAME=smtp.example.com   # Used as TLS certificate CN
DATA_DIR=/app/data               # Leave as-is for Docker
MAX_MESSAGE_SIZE_MB=35           # Max SMTP message size (default 35)
```

Generate a random `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Edit `docker-compose.yml` if you need different port mappings, a bind mount instead of the named volume, or extra services. The committed `docker-compose.example.yml` is only a starting point; your local `docker-compose.yml` is not tracked in git.

### Docker Secrets (`_FILE` pattern)

Every variable can be provided as a file path via `<VAR>_FILE`. The application reads the file content at startup. Example with Docker secrets:

```yaml
# docker-compose.yml
secrets:
  entra_secret:
    file: ./secrets/entra_client_secret.txt

services:
  app:
    secrets: [entra_secret]
    environment:
      ENTRA_CLIENT_SECRET_FILE: /run/secrets/entra_secret
```

---

## Running with Docker Compose

After creating `.env` and `docker-compose.yml` from the examples (see [Configuration](#configuration)):

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

The first user who opens `http://<host>:5000` and signs in with Microsoft is automatically granted admin rights.

---

## Using the SMTP relay

Configure your application or mail client:

| Setting | Value |
|---|---|
| Host | Your server's hostname or IP |
| Port | `587` (STARTTLS, recommended), `465` (SMTPS), or `25` (legacy — STARTTLS optional) |
| Username | The credential username from the web UI |
| Password | The credential password shown once at creation |
| TLS | Required on 465/587 (self-signed — your client may need to trust it); optional on 25 |

The `from` address of sent emails must match one of the credential's **allowed senders**, and all `to` addresses must match one of the **allowed recipients**. Wildcards like `*@example.com` are supported.

---

## File layout

```
smtp-relay/
├── app/
│   ├── main.py           # Flask app + startup orchestration
│   ├── config.py         # Environment variable parsing (_FILE support)
│   ├── models.py         # SQLAlchemy models
│   ├── database.py       # DB init, WAL mode, session helpers
│   ├── auth.py           # OIDC login (authlib), first-user-admin logic
│   ├── cert.py           # Self-signed TLS certificate management
│   ├── graph.py          # MS Graph API client (MSAL + httpx)
│   ├── smtp_server.py    # aiosmtpd Controller setup
│   ├── smtp_handler.py   # Auth, rule checking, email logging + forwarding
│   └── routes/           # Flask blueprints (credentials, emails, users, settings)
├── docker-compose.example.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

`docker-compose.yml` is created locally from the example and is gitignored.

Runtime data (SQLite database and TLS certificates) is stored in the `smtp_relay_data` Docker volume.

---

## Protecting against brute-force with fail2ban

Failed authentication attempts are logged to stdout in a consistent format:

```
SMTP auth failed from 1.2.3.4: wrong password for 'myuser'
SMTP auth failed from 1.2.3.4: unknown user 'root'
```

### 1. Create the filter

`/etc/fail2ban/filter.d/smtp-relay.conf`:

```ini
[Definition]
failregex = SMTP auth failed from <HOST>
ignoreregex =
```

### 2. Configure the jail

`/etc/fail2ban/jail.d/smtp-relay.conf`:

```ini
[smtp-relay]
enabled  = true
port     = 25,465,587
filter   = smtp-relay
backend  = systemd
journalmatch = CONTAINER_NAME=smtp-relay-app-1
maxretry = 5
findtime = 600
bantime  = 3600
```

> **Docker log backend:** fail2ban reads Docker container logs via the systemd journal (when Docker uses the `journald` log driver) or directly from the Docker log file. Adjust `journalmatch` / `logpath` to match your setup:
>
> - **journald driver** (recommended): set `backend = systemd` and `journalmatch = CONTAINER_NAME=smtp-relay-app-1`
> - **json-file driver** (default): set `backend = auto` and `logpath = /var/lib/docker/containers/<id>/<id>-json.log`

### 3. Reload fail2ban

```bash
fail2ban-client reload
fail2ban-client status smtp-relay
```

---

## Notes

- **Graph API attachments:** Mail is sent by creating a draft, attaching files, then sending. Attachments under 3 MB are added inline; larger files use a Graph [upload session](https://learn.microsoft.com/en-us/graph/outlook-large-attachments) (up to **150 MB** per attachment). The relay enforces that per-attachment limit in code. At SMTP ingress, `MAX_MESSAGE_SIZE_MB` (default **35**) caps the entire raw message, including all attachments. Your tenant's Exchange Online message size limit may reject larger messages as well — set `MAX_MESSAGE_SIZE_MB` to match the limit you want the relay to accept.
- **Sent Items:** For forwarded mail, each credential has a "Save to Sent Items" option. Graph always creates a Sent Items copy when sending a draft; when the option is unchecked, the relay removes that copy in a background thread after Graph accepts the send (best effort).
- **TLS certificate:** By default a self-signed certificate is generated on start and renewed automatically when fewer than 90 days remain. Set `SMTP_CERT_FILE` and `SMTP_KEY_FILE` to use your own certificate instead — auto-generation and auto-renewal are then disabled, and the relay reloads SMTP when those files change on disk. Clients will warn about an untrusted certificate unless you use a cert they already trust.
- **SQLite concurrency:** WAL mode is enabled for reliable concurrent access from the SMTP handler, web server, and background jobs.
- **Single gunicorn worker:** Required because the SMTP server and APScheduler run in-process threads that must not be duplicated across workers.
