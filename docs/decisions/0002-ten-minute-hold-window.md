# 2. Ten-minute hold window

## Status
Accepted

## Context
A held seat blocks other users from booking it. The hold must live long
enough for a real user to complete payment (redirect to a payment provider,
enter card/UPI details, 3DS/OTP step), but short enough that an abandoned
checkout doesn't lock inventory for long.

## Decision
Seat holds expire 10 minutes after creation (`hold_expires_at = now() + 10
minutes`), enforced by the expiry job (build order step 3) and reflected in
`SeatInventory.hold_expires_at`.

## Rationale
- Razorpay and comparable checkout flows (redirect + OTP/3DS) typically
  complete well under 5 minutes; 10 minutes gives comfortable slack for a
  distracted user without being generous enough to enable seat squatting.
- It's a single configuration value (`HOLD_TTL_SECONDS`), not hardcoded, so it
  can be tuned per event (e.g. shorter for high-demand on-sales) without a
  schema change.

## Consequences
- The expiry job must run frequently enough (every 30s, per calude.md) that
  the effective hold time doesn't meaningfully exceed 10 minutes under load.
- A payment that completes after expiry (race between expiry job and webhook)
  is handled by the late-payment refund edge case in the webhook flow, not by
  extending the hold.
