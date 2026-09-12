# Bookam — full walkthrough

Every screen captured from a running instance seeded with a realistic demo:
**Adjoa's Beauty Bar** (salon, 2 branches, 2 staff, home service, ⭐ 4.7) and
**Kofi Cuts** (barber), with bookings in every lifecycle state.

## Customer side

| # | Screen | What it shows |
|---|---|---|
| 01 | Landing | The pitch + signup |
| 02 | Discover | Public directory with star ratings |
| 03 | Booking page (home visit) | Branch picker, venue choice, address, GH₵130 deposit incl. travel fee |
| 04 | Live tracker | "On the way" state, auto-refreshing status pills |
| 05 | Receipt + rating | Completed booking asking "How was it?" |
| 06 | WhatsApp chat | The whole journey in-chat: broadcast → "find braids" → search results → booking with location pin, staff choice, deposit → "status" |

## Business side

| # | Screen | What it shows |
|---|---|---|
| 07 | Dashboard | Today's pipeline (done / on-the-way / confirmed), stats, booking + WhatsApp links |
| 08 | Services | Prices, deposits, home-service travel fee |
| 09 | Hours | Per-branch weekly hours with branch tabs |
| 10 | Locations | Osu + East Legon branches |
| 11 | Team | Staff per branch (capacity + pick-your-person) |
| 12 | Customers | Visit counts, no-show flags, one-tap WhatsApp |
| 13 | Updates | Broadcast to all customers, reach count, history |
| 14 | Reports | Monthly bookings/deposits, top services & customers, CSV export |

Regenerate: run the app in demo mode, seed, and screenshot with Playwright
(scripts lived in the dev session; any fresh seed works the same way).
