# Ahad Co - Authentication System

A complete authentication system with beautiful glassmorphism UI, 2FA support, and database management.

## Features

✅ **User Authentication**
- Sign Up with email verification (OTP)
- Login with username or email
- Password strength indicator
- Remember me functionality
- Password reset with OTP

✅ **Dashboard**
- Profile management
- Links management
- Security settings
- Session management

✅ **Security**
- Two-Factor Authentication (2FA) with TOTP
- Backup codes for 2FA
- Session management (view/revoke sessions)
- Login history tracking
- Rate limiting
- Password hashing with bcrypt

✅ **UI/UX**
- Beautiful glassmorphism design
- Dark/Light theme toggle
- Responsive design
- Keyboard shortcuts
- Toast notifications
- Loading animations

## Quick Start

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn app:app --reload
```

### Docker

```bash
# Build and run
docker-compose up

# Or with Docker directly
docker build -t ahad-co .
docker run -p 8000:8000 \
  -e BREVO_API_KEY=your_api_key \
  -e SENDER_EMAIL=your@email.com \
  ahad-co
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `BREVO_API_KEY` | Brevo/Sendinblue API key for emails | Yes |
| `SENDER_EMAIL` | Email address to send from | Yes |
| `SENDER_NAME` | Sender name (default: "Ahad Co") | No |
| `DATABASE_URL` | PostgreSQL connection string (Supabase). If set → **permanent PostgreSQL storage**; if unset → local SQLite | No |
| `DB_PATH` | Path to SQLite database (only used when `DATABASE_URL` is unset) | No |
| `PG_SSLMODE` | PostgreSQL SSL mode (default: `prefer`) | No |
| `OTP_EXPIRY_MINUTES` | OTP expiry time (default: 10) | No |

## Database: SQLite vs PostgreSQL (Supabase)

The app supports **two** database backends and picks one automatically:

* **PostgreSQL (Supabase)** — used when `DATABASE_URL` is set. Your data is
  stored permanently in Supabase and survives every redeploy. **Recommended for
  production.**
* **SQLite** — used when `DATABASE_URL` is unset. A single file at `DB_PATH`.
  Great for local development (zero setup), but data does not survive container
  redeploy unless you mount a persistent disk.

> 💡 The code uses the same query syntax for both engines; the `database.py`
> layer transparently handles placeholder style (`?` → `%s`), `lastrowid`
> (via `RETURNING id`), `SERIAL`/`CITEXT`, and upserts (`ON CONFLICT`).

### Switch to Supabase (permanent data, free, no credit card)

1. Sign up at [supabase.com](https://supabase.com) and create a **free** project.
2. Open **Project Settings → Database → Connection string** and copy the
   **Session pooler** URL (port `5432`). Replace `[YOUR-PASSWORD]` with your
   database password:
   ```
   postgresql://postgres.xxxx:YOUR_PASSWORD@aws-0-region.pooler.supabase.com:5432/postgres
   ```
3. Set it as your `DATABASE_URL` environment variable (in `.env` locally, or the
   Render/Render dashboard in production).
4. Restart the app. On startup it auto-creates all tables (incl. the `citext`
   extension for case-insensitive usernames/emails).

**Bring your existing SQLite data over** — run the included migration script:

```bash
# 1. Make sure DATABASE_URL points at Supabase
export DATABASE_URL='postgresql://postgres.xxxx:YOUR_PASSWORD@aws-0-region.pooler.supabase.com:5432/postgres'
export DB_PATH='./database.db'   # your existing local file

# 2. Create the schema in Supabase, then copy everything
python migrate_to_supabase.py --init-schema --force
```

The script preserves all user ids, sessions, vault entries, notes, bookmarks,
2FA settings, etc., and advances each table's sequence so nothing collides. It
runs in a single transaction and rolls back on any error, so it is safe to re-run.

## Deploy to Render

1. Push to GitHub
2. Go to [Render](https://render.com)
3. Connect your GitHub repo
4. Create a Web Service:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add environment variables from Render Dashboard
6. Enable Persistent Disk for `/data`

### Preventing Render Free Tier Spin-Down (Uptime & Reliability)

Render's free tier web services automatically spin down after 15 minutes of no external/internal HTTP traffic, causing a ~30–60s cold start on the next request. To ensure seamless uptime, Ahad Co includes built-in mitigations and recommends an external guard:

1. **Built-in Self-Ping Mechanism**: 
   The app runs a background asyncio scheduled task that sends a lightweight HTTP GET request to its own `/health` endpoint every 10–14 minutes. This keeps the service from ever going fully idle long enough to spin down.
2. **RunSpace Proxy Retry-with-Backoff**: 
   When requests hit a RunSpace job URL after an idle period, the proxy layer automatically retries up to 3 times with short delays (2s, 4s, 8s) if the initial request fails or times out, smoothing over brief cold-start windows without requiring manual page refreshes.
3. **Clear Status Messaging**: 
   During a cold-start window, users see a clear, informative status message (`"Waking up your RunSpace... this can take up to a minute on the free tier"`) instead of a raw error.

#### Is Self-Ping Sufficient, or Should You Also Set Up an External Ping Service?
* **Self-Ping is highly effective** for preventing idle spin-downs under normal container operation.
* **External Ping (Recommended Second Layer)**: We recommend also setting up a free external ping service (such as **UptimeRobot**, which was used earlier in this project) configured to ping `https://<your-app>.onrender.com/health` every 5–10 minutes.
* **Conclusion**: For the most consistent, bulletproof 24/7 uptime on the free tier, **use both** the built-in self-ping mechanism and an external UptimeRobot monitor.

## Deploy to Friendhost/FriendsHost

```bash
# SSH into your server
ssh user@your-server

# Clone your repo
git clone https://github.com/yourusername/ahad-co.git
cd ahad-co

# Install dependencies
pip install -r requirements.txt

# Run with systemd (create a service file)
sudo nano /etc/systemd/system/ahad-co.service
```

```ini
[Unit]
Description=Ahad Co Auth System
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/ahad-co
Environment="BREVO_API_KEY=your_key"
Environment="SENDER_EMAIL=your@email.com"
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable ahad-co
sudo systemctl start ahad-co
```

## API Endpoints

### Authentication
- `POST /signup` - Create new account
- `POST /verify` - Verify email OTP
- `POST /resend-otp` - Resend verification code
- `POST /login` - Sign in
- `POST /logout` - Sign out

### Password Reset
- `POST /forgot-password` - Request reset code
- `POST /verify-reset-otp` - Verify reset code
- `POST /reset-password` - Set new password

### Profile
- `GET /profile` - Get profile data
- `POST /profile/update` - Update profile

### Two-Factor Authentication
- `GET /2fa/status` - Get 2FA status
- `POST /2fa/setup` - Setup 2FA
- `POST /2fa/verify-setup` - Verify and enable 2FA
- `POST /2fa/verify-login` - Verify 2FA during login

### Sessions
- `GET /sessions` - List all sessions
- `POST /sessions/revoke` - Revoke a session

### Vault
- `GET /vault` - List vault entries
- `POST /vault/add` - Add entry
- `POST /vault/update` - Update entry
- `POST /vault/delete` - Delete entry

### Utility
- `GET /health` - Health check
- `GET /login-history` - Login history
- `POST /account/delete` - Delete account

## Database Schema

The app works with **SQLite** (local dev) or **PostgreSQL / Supabase** (permanent
storage). The same tables are created either way:
- `users` - User accounts (case-insensitive username/email via `CITEXT`/`NOCASE`)
- `sessions` - Active sessions
- `vault_entries` - User data vault
- `user_2fa` - 2FA settings
- `login_history` - Login tracking
- `user_notes`, `user_bookmarks`, `user_categories`, `user_preferences`,
  `api_keys`, `activity_log`, `notifications`

## License

MIT License - Use freely for personal and commercial projects.
