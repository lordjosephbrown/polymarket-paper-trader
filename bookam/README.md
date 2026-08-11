# Bookam

**Appointment booking with MoMo deposits, for Ghanaian service businesses.**

Salons, barbers, nail techs, makeup artists, tailors, and clinics lose money every week to
no-shows. Bookam gives each business a booking page they can share on WhatsApp and
Instagram; clients pick a slot and pay a small **mobile money deposit** (via Paystack) to
lock it. No-shows drop, and the deposit is the business's to keep.

## Features (MVP)

- **Business signup** with phone + password — no email needed
- **Services** with duration, full price, and deposit amount
- **Multiple locations/branches** — each branch has its own hours and its own calendar;
  customers pick the branch when booking (web and WhatsApp)
- **Home service / mobile businesses** — a service can happen at the shop, at the
  customer's place, or either. Home visits capture the customer's address and add a
  configurable travel/callout fee to the deposit
- **Public directory** at `/discover` — customers find businesses by name, category, or area
- **Working hours** per weekday per branch; closed days have no slots
- **Public booking page** at `/b/<slug>` — mobile-first, made to be opened from WhatsApp
- **Live slot availability** — double-bookings are impossible; unpaid holds lapse after 15 min
- **Paystack deposits** (mobile money + card, GHS) with a zero-config demo mode
- **Book entirely inside WhatsApp**: a bot walks customers through service → day → time
  with native tap-to-select lists, then pushes the deposit as a **MoMo approval prompt**
  on their phone (Paystack Charge API) — they never open a browser
- **Business notifications on WhatsApp**: new bookings and cancellations arrive as messages
- **Appointment reminders**: customers get a WhatsApp nudge ~2 hours before their slot
- **Customer cancellation**: in the chat (send "cancel") or from the receipt page
- **Paystack server-to-server webhook** (`/pay/webhook`, signature-verified) so payment
  confirmation doesn't depend on the customer's browser
- **Live delivery tracking** — the business taps On my way → Start → Done; the customer
  gets a WhatsApp update at each step and sees a live status tracker on their receipt
  page. "status" in the chat lists their bookings and where each one stands
- **Business-initiated changes** — reschedule (customer notified of the new time, old
  slot freed) or cancel (customer notified with a refund note); illegal status jumps
  are rejected
- **Broadcast updates** — one message a day to every past customer on WhatsApp:
  new services, price changes, moved shops, holiday closures
- **Dashboard**: today's appointments, upcoming, delivery pipeline buttons, deposit stats
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

### WhatsApp (optional, but the killer feature)

Booking inside WhatsApp uses Meta's **WhatsApp Business Cloud API**. One-time setup:
create a Meta Business app with WhatsApp enabled (developers.facebook.com), add a phone
number, and point the app's webhook at `https://<your-domain>/wa/webhook` using your
`WHATSAPP_VERIFY_TOKEN`. Then set:

| Variable | Purpose |
|---|---|
| `WHATSAPP_TOKEN` | Cloud API access token. |
| `WHATSAPP_PHONE_ID` | The Cloud API phone number ID messages are sent from. |
| `WHATSAPP_VERIFY_TOKEN` | Any string; must match the webhook config in Meta. |
| `WHATSAPP_APP_SECRET` | Meta app secret, used to verify webhook signatures. |
| `WHATSAPP_PUBLIC_NUMBER` | The number customers message, international format (e.g. `233XXXXXXXXX`). Enables the "Book on WhatsApp" button and dashboard deep link. |

Without these, the bot logic still runs (webhook + outbox) — messages just aren't
delivered to real phones. In Paystack live mode also configure the webhook URL
`https://<your-domain>/pay/webhook` in the Paystack dashboard so MoMo charge
confirmations arrive server-to-server.

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

- Per-business Paystack subaccounts so deposits settle straight to each business
- SMS fallback (Arkesel) for customers not on WhatsApp
- Password reset via phone OTP; multi-staff calendars
- Phase 2: orders + invoicing + payment links for product sellers (the commerce toolkit)
