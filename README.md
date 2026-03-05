# Signal Trader — Deployment Guide

A distributed trading automation platform:  
**Telegram → GPT-4o Mini → Bybit Futures**

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Vercel (Frontend)                                          │
│  Next.js 14 Dashboard ─── polls every 2s ──────────────────┤
│  /dashboard  /settings                                      │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS + Bearer Token
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  GCP VM (Ubuntu 22.04)                                      │
│                                                             │
│  ┌─────────────────────────┐  ┌────────────────────────┐   │
│  │  main.py (asyncio)      │  │  FastAPI :8000         │   │
│  │  ├── Telethon listener  │  │  GET  /logs            │   │
│  │  ├── Signal Parser      │  │  GET  /config          │   │
│  │  └── CCXT Trader        │  │  POST /config          │   │
│  └─────────────────────────┘  │  GET  /status          │   │
│                                └────────────────────────┘   │
│  config.json (encrypted)  master.key  activity.log          │
└─────────────────────────────────────────────────────────────┘
```

---

## Part 1: GCP VM Setup

### 1.1 Create VM

1. Go to **Google Cloud Console → Compute Engine → VM Instances**
2. Create instance:
   - **OS**: Ubuntu 22.04 LTS
   - **Machine type**: e2-small (2 vCPU, 2GB RAM) minimum
   - **Boot disk**: 20GB SSD
   - **Firewall**: Allow HTTP, HTTPS traffic (we'll fine-tune with UFW)
3. Note the **External IP address**

### 1.2 Upload and Run Setup Script

```bash
# From your local machine — upload project files
gcloud compute scp --recurse ./backend/ YOUR_VM_NAME:/tmp/backend/
gcloud compute ssh YOUR_VM_NAME

# On the VM
chmod +x /tmp/backend/setup.sh
sudo bash /tmp/backend/setup.sh
```

### 1.3 Configure Environment Variables

```bash
sudo nano /opt/signal-trader/.env
```

Fill in:
```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE=+1234567890
API_BEARER_TOKEN=<generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
UVICORN_HOST=0.0.0.0
UVICORN_PORT=8000
ALLOWED_ORIGINS=https://your-app.vercel.app
```

> **Generate Bearer Token:**
> ```bash
> python3 -c "import secrets; print(secrets.token_hex(32))"
> ```
> Copy this value — you'll need it for Vercel env vars too.

### 1.4 Telegram One-Time Authentication

```bash
pm2 stop signal-trader
cd /opt/signal-trader
source venv/bin/activate
python main.py
# Enter the OTP code sent to your Telegram account
# Wait for "Connected as: username" message
# Press Ctrl+C
pm2 start signal-trader
pm2 save
```

### 1.5 Configure Bybit & OpenAI via Dashboard

After deploying the frontend, use the Settings page to enter:
- Bybit API Key + Secret (encrypted immediately)
- OpenAI API Key (encrypted immediately)
- Risk per trade in USDT
- Telegram Channel IDs to monitor

---

## Part 2: UFW Firewall Rules

```bash
# Reset and configure
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing

# SSH (keep this open — critical!)
sudo ufw allow 22/tcp comment "SSH"

# FastAPI Bridge for Vercel dashboard
sudo ufw allow 8000/tcp comment "FastAPI Bridge"

# Enable
sudo ufw --force enable
sudo ufw status verbose
```

> **For Production:** Restrict port 8000 to Vercel's IP ranges only:
> ```bash
> # Allow only Vercel edge IPs (check current list at vercel.com/docs)
> sudo ufw delete allow 8000/tcp
> sudo ufw allow from 76.76.21.0/24 to any port 8000
> ```

---

## Part 3: Vercel Deployment

### 3.1 Push Frontend to GitHub

```bash
cd frontend/
git init
git add .
git commit -m "Initial Signal Trader frontend"
gh repo create signal-trader-frontend --private --push
```

### 3.2 Deploy on Vercel

1. Go to [vercel.com](https://vercel.com) → **New Project**
2. Import your GitHub repository
3. Set **Framework**: Next.js (auto-detected)
4. Set **Root Directory**: `frontend/` (if in monorepo)

### 3.3 Vercel Environment Variables

In Vercel dashboard → **Settings → Environment Variables**, add:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `http://YOUR_GCP_EXTERNAL_IP:8000` |
| `NEXT_PUBLIC_API_BEARER_TOKEN` | `<same token as API_BEARER_TOKEN on VM>` |

> **HTTPS Note:** For production, put an Nginx reverse proxy with SSL in front of the FastAPI server and change the URL to `https://`.

---

## Part 4: Obtaining Telegram Credentials

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. Log in with your phone number
3. Create a new application
4. Copy **API ID** and **API Hash**

**Finding Channel IDs:**
- Add [@userinfobot](https://t.me/userinfobot) to the channel
- Forward a message from the channel to it — it replies with the channel ID
- IDs are negative numbers like `-1001234567890`

---

## Part 5: PM2 Management

```bash
# View status
pm2 status

# View live logs
pm2 logs signal-trader

# Restart
pm2 restart signal-trader

# Stop
pm2 stop signal-trader

# View last 100 lines
pm2 logs signal-trader --lines 100

# Monitor resources
pm2 monit
```

---

## Part 6: Nginx + SSL (Recommended for Production)

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

# Configure reverse proxy
sudo nano /etc/nginx/sites-available/signal-trader
```

```nginx
server {
    listen 80;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/signal-trader /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# SSL certificate
sudo certbot --nginx -d your.domain.com
```

Then update `ALLOWED_ORIGINS` in `.env` and `NEXT_PUBLIC_API_URL` in Vercel.

---

## Security Checklist

- [ ] `master.key` has `chmod 600` (set automatically)
- [ ] `.env` has `chmod 600` (set automatically)
- [ ] `config.json` has `chmod 600` (set automatically)
- [ ] Bearer token is at least 32 random hex chars
- [ ] `ALLOWED_ORIGINS` restricted to your Vercel domain
- [ ] Port 8000 restricted to Vercel IPs (optional, high-security)
- [ ] Bybit API key has IP whitelist set to GCP VM external IP
- [ ] Bybit API key has only "Trade" permissions (no withdrawal)
- [ ] Testnet enabled for initial testing

---

## File Structure

```
signal-trader/
├── backend/
│   ├── main.py              # Entrypoint: asyncio orchestrator
│   ├── config_manager.py    # AES-256 encrypted config
│   ├── signal_parser.py     # GPT-4o Mini signal parser
│   ├── trader.py            # CCXT Bybit execution engine
│   ├── api_server.py        # FastAPI bridge
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx          # Redirects to /dashboard
│   │   ├── globals.css
│   │   ├── dashboard/page.tsx
│   │   └── settings/page.tsx
│   ├── lib/api.ts
│   ├── package.json
│   ├── tailwind.config.js
│   ├── next.config.js
│   └── .env.example
└── setup.sh
```
