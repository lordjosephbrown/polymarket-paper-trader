# Bookam

**Appointment booking with MoMo deposits, for Ghanaian service businesses.**

Salons, barbers, nail techs, makeup artists, tailors, and clinics lose money every week to
no-shows. Bookam gives each business a booking page they can share on WhatsApp and
Instagram; clients pick a slot and pay a small **mobile money deposit** (via Paystack) to
lock it. No-shows drop, and the deposit is the business's to keep.

## Features (MVP)

- **Business signup** with phone + password — no email needed
- **Services** with duration, full price, and deposit amount
- **Working hours** per weekday; closed days have no slots
- **Public booking page** at `/b/<slug>` — mobile-first, made to be opened from WhatsApp
- **Live slot availability** — double-bookings are impossible; unpaid holds lapse after 15 min
- **Paystack deposits** (mobile money + card, GHS) with a zero-config demo mode
- **Dashboard**: today's appointments, upcoming, mark done / no-show, deposit stats
- **Customer list** with visit counts, no-show history, and one-tap WhatsApp

## Run it

```bash
cd bookam
pip install -r requirements.txt
uvicorn app.main:create_app --factory --reload
# open http://localhost:8000
```

Without configuration it runs in **demo mode**: deposits auto-succeed so you can try the
whole flow end to end.

## Tests

```bash
cd bookam
pip install -r requirements.txt pytest
python3 -m pytest tests/ -x -q
```

## Going live

Set these environment variables:

| Variable | Purpose |
|---|---|
| `PAYSTACK_SECRET_KEY` | Your Paystack secret key (`sk_live_…`). Enables real MoMo/card deposits. |
| `BOOKAM_SECRET` | Random string used to sign login sessions. Generate with `openssl rand -hex 32`. |
| `BOOKAM_BASE_URL` | Public URL of the deployment, e.g. `https://bookam.example.com`. Used in booking links and the Paystack callback. |
| `BOOKAM_DB` | Optional. Path to the SQLite file (default `~/.bookam/bookam.db`). |

### Deploy with Docker (Render, Railway, Fly.io, or any VPS)

```bash
docker build -t bookam .
docker run -p 8000:8000 -v bookam-data:/data \
  -e PAYSTACK_SECRET_KEY=sk_live_xxx \
  -e BOOKAM_SECRET=$(openssl rand -hex 32) \
  -e BOOKAM_BASE_URL=https://your-domain.com \
  bookam
```

Paystack setup: create a (free) Paystack Ghana account, get API keys from
Settings → API Keys, and set `PAYSTACK_SECRET_KEY`. Deposits settle to the bank/MoMo
account configured in Paystack. Use an `sk_test_…` key to test with Paystack's sandbox
before going live.

## Roadmap

- SMS/WhatsApp appointment reminders (Arkesel)
- Per-business Paystack subaccounts so deposits settle straight to each business
- Phase 2: orders + invoicing + payment links for product sellers (the commerce toolkit)
