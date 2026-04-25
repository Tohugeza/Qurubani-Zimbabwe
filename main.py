"""
================================================================================
  Forgotten Muslims / Qurbani Zimbabwe — FastAPI Backend
  File:    main.py
  Service: forgottenmuslims-api  (systemd)
  API:     api.forgottenmuslims.co.zw  (port 8001)
  DB:      PostgreSQL · database: qurbani · user: postgres
================================================================================

  ARCHITECTURE OVERVIEW
  ─────────────────────
  The donation flow works in two stages:

  STAGE 1 — Donor fills in the form on the website
    · Frontend collects: first name, surname, email, phone, qty,
      on-behalf-of names, special instructions, comms opt-in
    · Frontend POSTs this to POST /donations/pending
    · We save a "pending" row to qurbani_donations with paid=FALSE
      and no stripe_session_id yet
    · We return a pending_id to the frontend (not currently used
      for matching — kept for future token-based matching)

  STAGE 2 — Donor completes payment on Stripe
    · Stripe fires a checkout.session.completed webhook to
      POST /webhook/stripe
    · We verify the Stripe signature to confirm it's genuine
    · We extract: amount, email, name, phone from the Stripe event
    · We find the matching pending row using:
        - amount_pence matches exactly
        - created_at is within 10 minutes of NOW()
        - paid = FALSE (not already matched)
    · We update that row: fill in stripe_session_id, paid=TRUE,
      donor_first/last/phone from Stripe if our DB fields are empty
    · We send an email notification to the team

  MATCHING LOGIC NOTE
  ───────────────────
  We match pending rows to Stripe payments using:
    1. amount_pence must match exactly (qty × 8500)
    2. row must have been created within 10 minutes of the webhook
    3. row must not already be marked paid
  At our donation volumes (a handful per day at most), the chance
  of two donors submitting the exact same amount at the exact same
  time is essentially zero. The 10-minute window is generous —
  most donors complete Stripe checkout in under 2 minutes.

  FUTURE UPGRADE PATH (token-based matching)
  ───────────────────────────────────────────
  For higher volumes or to make matching 100% deterministic,
  upgrade to token-based matching:
    · POST /donations/pending returns a unique token (UUID)
    · Frontend appends token to Stripe Payment Link as
      ?client_reference_id=TOKEN
    · Stripe passes client_reference_id through to the webhook
    · Webhook looks up the row by token instead of time+amount
  The pending_id field and UUID import below are already in place
  for this upgrade. See the "FUTURE: token matching" comments.

  ENVIRONMENT VARIABLES (set in /etc/environment or .env)
  ────────────────────────────────────────────────────────
  STRIPE_WEBHOOK_SECRET   whsec_xxxxxxxxxxxxxxxx  (from Stripe dashboard)
  STRIPE_SECRET_KEY       sk_live_xxxxxxxxxxxxxxx (from Stripe dashboard — API keys)
  QURBANI_DATABASE_URL    postgresql://postgres:PASSWORD@localhost:5432/qurbani
  RESEND_API_KEY          re_xxxxxxxxxxxxxxxx      (from resend.com)
  NOTIFY_EMAIL            your@email.com           (team notification address)

================================================================================
"""

import os
import json
import uuid
import stripe
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List

# ── Environment variables ────────────────────────────────────────────────────
# These are read from the server environment. Set them in /etc/environment
# or in a .env file loaded by the systemd service (see override.conf).

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
DATABASE_URL          = os.environ.get("QURBANI_DATABASE_URL",
                            "postgresql://postgres:dyhjU5jivgobwoztuj@localhost:5432/qurbani")
RESEND_API_KEY        = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL          = os.environ.get("NOTIFY_EMAIL", "")

# Fixed price per goat in pence (£85.00)
GOAT_PRICE_PENCE = 8500

# How many minutes back we look when matching a Stripe payment to a
# pending donor submission. 10 minutes is generous — most donors
# complete checkout in under 2 minutes.
MATCH_WINDOW_MINUTES = 10

# ── FastAPI app setup ─────────────────────────────────────────────────────────

app = FastAPI(
    title="Forgotten Muslims API",
    description="Backend for Qurbani Zimbabwe donation platform",
    version="2.0.0"
)

# CORS — allows the website frontend to call this API from the browser.
# In production you could tighten allow_origins to ["https://forgottenmuslims.co.zw"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"]
)

# ── Pydantic models ──────────────────────────────────────────────────────────

class DonationSubmission(BaseModel):
    """
    Data sent by the frontend when the donor clicks 'Secure your Qurbani'
    before being redirected to Stripe.
    """
    donor_first:           str
    donor_last:            str
    donor_email:           str
    donor_phone:           str
    qty:                   int
    on_behalf_of:          Optional[str]  = None   # comma-separated names
    special_instructions:  Optional[str]  = None
    comms_opt_in:          Optional[bool] = False


class CheckoutRequest(BaseModel):
    """
    Data sent by the frontend to create a Stripe Checkout Session.
    qty  — number of goats (1–100)
    names — list of on-behalf-of names (can be empty)
    """
    qty:   int
    names: List[str] = []

# ── Database helpers ─────────────────────────────────────────────────────────

def get_db():
    """
    Open and return a new PostgreSQL connection.
    Uses RealDictCursor so rows come back as dicts (column: value)
    rather than plain tuples.
    Always call conn.close() in a finally block after use.
    """
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def save_pending_donation(data: DonationSubmission) -> int:
    """
    Stage 1 — Save the donor's form data BEFORE they go to Stripe.

    Inserts a new row with:
      · All donor details from the web form
      · amount_pence calculated from qty × GOAT_PRICE_PENCE
      · paid = FALSE  (not yet confirmed by Stripe)
      · stripe_session_id = 'PENDING' (placeholder until webhook arrives)
      · payer_name = donor_first + donor_last (kept for backwards compat)

    Returns the new row's id so we can reference it later.

    NOTE: stripe_session_id has a UNIQUE constraint, so we use a UUID
    placeholder to avoid conflicts between multiple pending rows.
    When the webhook matches and updates the row, it overwrites this
    with the real Stripe session ID.
    """
    pending_token = f"PENDING_{uuid.uuid4().hex}"

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO qurbani_donations (
                    stripe_session_id,
                    payer_name,
                    payer_email,
                    donor_first,
                    donor_last,
                    donor_phone,
                    on_behalf_of,
                    qty,
                    amount_pence,
                    special_instructions,
                    comms_opt_in,
                    paid,
                    notified
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, FALSE
                )
                RETURNING id
            """, (
                pending_token,
                f"{data.donor_first} {data.donor_last}",
                data.donor_email,
                data.donor_first,
                data.donor_last,
                data.donor_phone,
                data.on_behalf_of,
                data.qty,
                data.qty * GOAT_PRICE_PENCE,
                data.special_instructions,
                data.comms_opt_in,
            ))
            conn.commit()
            row = cur.fetchone()
            return row["id"]
    finally:
        conn.close()


def match_and_confirm_donation(
    stripe_session_id: str,
    amount_pence: int,
    stripe_name: str,
    stripe_email: str,
    stripe_phone: str,
) -> bool:
    """
    Stage 2 — Called by the Stripe webhook handler.

    Finds the most recent pending row where:
      · amount_pence matches exactly
      · created_at is within MATCH_WINDOW_MINUTES of now
      · paid = FALSE
      · stripe_session_id starts with 'PENDING_'

    If found, updates that row with:
      · stripe_session_id = real Stripe session ID
      · paid = TRUE
      · Fills donor_first/last/phone/email from Stripe IF our
        fields are empty (preserves form data where available)

    Returns True if a match was found and updated, False otherwise.
    If no match is found, inserts a new row from Stripe data alone
    so we never lose a payment.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT id, donor_first, donor_last, donor_phone, payer_email
                FROM qurbani_donations
                WHERE amount_pence = %s
                  AND paid = FALSE
                  AND stripe_session_id LIKE 'PENDING_%%'
                  AND created_at >= NOW() - INTERVAL '%s minutes'
                ORDER BY created_at DESC
                LIMIT 1
            """, (amount_pence, MATCH_WINDOW_MINUTES))

            row = cur.fetchone()

            if row:
                cur.execute("""
                    UPDATE qurbani_donations SET
                        stripe_session_id = %s,
                        paid              = TRUE,
                        payer_email       = COALESCE(NULLIF(payer_email, ''), %s),
                        donor_first       = COALESCE(NULLIF(donor_first, ''), %s),
                        donor_last        = COALESCE(NULLIF(donor_last, ''),  %s),
                        donor_phone       = COALESCE(NULLIF(donor_phone, ''), %s)
                    WHERE id = %s
                """, (
                    stripe_session_id,
                    stripe_email,
                    stripe_name.split(" ")[0] if stripe_name else "",
                    " ".join(stripe_name.split(" ")[1:]) if stripe_name and " " in stripe_name else "",
                    stripe_phone or "",
                    row["id"],
                ))
                conn.commit()
                print(f"[forgottenmuslims] Matched pending row id={row['id']} → {stripe_session_id}")
                return True

            else:
                print(f"[forgottenmuslims] No pending match for £{amount_pence/100:.2f} — inserting from Stripe data")
                qty = amount_pence // GOAT_PRICE_PENCE or 1
                cur.execute("""
                    INSERT INTO qurbani_donations (
                        stripe_session_id, payer_name, payer_email,
                        donor_first, donor_last, donor_phone,
                        qty, amount_pence, paid, notified
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, FALSE)
                    ON CONFLICT (stripe_session_id) DO NOTHING
                """, (
                    stripe_session_id,
                    stripe_name or "Unknown",
                    stripe_email or "",
                    stripe_name.split(" ")[0] if stripe_name else "",
                    " ".join(stripe_name.split(" ")[1:]) if stripe_name and " " in stripe_name else "",
                    stripe_phone or "",
                    qty,
                    amount_pence,
                ))
                conn.commit()
                return False

    finally:
        conn.close()


def get_donation_by_session(stripe_session_id: str) -> dict:
    """
    Fetch a single donation row by Stripe session ID.
    Used by the email notification to get full donor details.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM qurbani_donations WHERE stripe_session_id = %s",
                (stripe_session_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else {}
    finally:
        conn.close()


def mark_notified(stripe_session_id: str):
    """
    After sending the email notification, mark the row as notified=TRUE
    so we don't send duplicate emails if the webhook fires twice.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE qurbani_donations SET notified = TRUE WHERE stripe_session_id = %s",
                (stripe_session_id,)
            )
            conn.commit()
    finally:
        conn.close()


# ── Email notification ───────────────────────────────────────────────────────

def send_notification(donation: dict):
    """
    Send a team notification email via Resend (resend.com).

    Requires RESEND_API_KEY and NOTIFY_EMAIL environment variables.
    If either is missing, logs a warning and skips silently.
    """
    if not RESEND_API_KEY or not NOTIFY_EMAIL:
        print("[forgottenmuslims] Email not configured — skipping (set RESEND_API_KEY and NOTIFY_EMAIL)")
        return

    try:
        import resend
        resend.api_key = RESEND_API_KEY

        qty    = donation.get("qty", 1)
        label  = "goat" if qty == 1 else "goats"
        amount = donation.get("amount_pence", 0) / 100

        body_lines = [
            f"New Qurbani donation confirmed — £{amount:.2f} ({qty} {label})",
            "",
            "── DONOR DETAILS ──────────────────────────",
            f"Name:          {donation.get('donor_first', '')} {donation.get('donor_last', '')}".strip(),
            f"Email:         {donation.get('payer_email', '—')}",
            f"Phone:         {donation.get('donor_phone', '—')}",
            "",
            "── QURBANI DETAILS ─────────────────────────",
            f"Qty:           {qty} {label}",
            f"Total:         £{amount:.2f}",
            f"On behalf of:  {donation.get('on_behalf_of') or '—'}",
            f"Instructions:  {donation.get('special_instructions') or '—'}",
            f"Comms opt-in:  {'Yes' if donation.get('comms_opt_in') else 'No'}",
            "",
            "── STRIPE ──────────────────────────────────",
            f"Session ID:    {donation.get('stripe_session_id', '—')}",
            f"Timestamp:     {donation.get('created_at', '—')}",
        ]

        resend.Emails.send({
            "from":    "Qurbani Zimbabwe <noreply@forgottenmuslims.co.zw>",
            "to":      NOTIFY_EMAIL,
            "subject": f"[Qurbani 2026] New donation — £{amount:.2f} · {qty} {label}",
            "text":    "\n".join(body_lines),
        })

        print(f"[forgottenmuslims] Notification sent to {NOTIFY_EMAIL}")

    except Exception as e:
        print(f"[forgottenmuslims] Email error: {e}")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "project": "Qurbani Zimbabwe", "version": "2.0.0"}


@app.get("/health")
def health():
    try:
        conn = get_db()
        conn.close()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "ok",
        "project": "forgottenmuslims",
        "database": db_status,
    }


@app.post("/donations/pending")
async def create_pending_donation(data: DonationSubmission):
    """
    STAGE 1 — Receive donor form data before Stripe redirect.

    Called by the website frontend when the donor clicks
    'Secure your Qurbani'. Saves all form data as a pending row.
    """
    if data.qty < 1 or data.qty > 100:
        raise HTTPException(status_code=400, detail="qty must be between 1 and 100")

    try:
        pending_id = save_pending_donation(data)
        print(f"[forgottenmuslims] Pending saved: {data.donor_first} {data.donor_last} · {data.qty} goat(s) · id={pending_id}")
        return {
            "status":     "pending",
            "pending_id": pending_id,
            "amount":     data.qty * GOAT_PRICE_PENCE,
            "message":    "Donor details saved. Awaiting Stripe payment confirmation.",
        }
    except Exception as e:
        print(f"[forgottenmuslims] Pending save error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save donation details")


@app.post("/create-checkout-session")
async def create_checkout_session(req: CheckoutRequest):
    """
    Creates a Stripe Checkout Session with the correct total (qty × £85).
    Called by the frontend donate button before redirecting to Stripe.

    Request body:
    {
        "qty":   2,
        "names": ["Ahmed Khan", "Fatima Khan"]
    }

    Response:
    {
        "url": "https://checkout.stripe.com/c/pay/cs_live_..."
    }

    The frontend redirects the browser to the returned URL.
    Stripe handles payment and redirects back to success_url or cancel_url.
    """
    if req.qty < 1 or req.qty > 100:
        raise HTTPException(status_code=400, detail="qty must be between 1 and 100")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe secret key not configured")

    stripe.api_key = STRIPE_SECRET_KEY
    label = "goat" if req.qty == 1 else "goats"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "unit_amount": GOAT_PRICE_PENCE,
                    "product_data": {
                        "name": f"Qurbani Zimbabwe — {req.qty} {label}",
                    },
                },
                "quantity": req.qty,
            }],
            mode="payment",
            customer_creation="always",
            metadata={
                "qty":          str(req.qty),
                "on_behalf_of": ", ".join(req.names),
            },
            success_url="https://forgottenmuslims.co.zw/?success=true",
            cancel_url="https://forgottenmuslims.co.zw/?cancelled=true",
        )
        print(f"[forgottenmuslims] Checkout session created: {session.id} · {req.qty} {label}")
        return JSONResponse({"url": session.url})

    except Exception as e:
        print(f"[forgottenmuslims] Checkout session error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


@app.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    """
    STAGE 2 — Receive Stripe payment confirmation webhook.

    Stripe calls this endpoint automatically after a donor completes
    payment on the Stripe-hosted checkout page.

    Flow:
    1. Verify the Stripe webhook signature.
    2. Check event type is 'checkout.session.completed'.
    3. Extract donor details from the Stripe event.
    4. Match to a pending row in the database (time + amount).
    5. Update the matched row to paid=TRUE.
    6. Send team notification email.
    """
    payload = await request.body()

    try:
        stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        print("[forgottenmuslims] Invalid Stripe signature — rejecting webhook")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        print(f"[forgottenmuslims] Webhook parse error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    body = json.loads(payload)
    event_type = body.get("type")

    if event_type != "checkout.session.completed":
        return JSONResponse({"status": "ignored", "event": event_type})

    obj = body["data"]["object"]

    customer_details = obj.get("customer_details") or {}
    stripe_name  = customer_details.get("name")  or ""
    stripe_email = customer_details.get("email") or ""
    stripe_phone = customer_details.get("phone") or ""

    amount_pence      = obj.get("amount_total") or 0
    stripe_session_id = obj.get("id")

    print(f"[forgottenmuslims] Webhook received: {stripe_session_id} · £{amount_pence/100:.2f} · {stripe_name}")

    matched = match_and_confirm_donation(
        stripe_session_id = stripe_session_id,
        amount_pence      = amount_pence,
        stripe_name       = stripe_name,
        stripe_email      = stripe_email,
        stripe_phone      = stripe_phone,
    )

    donation = get_donation_by_session(stripe_session_id)

    if donation and not donation.get("notified"):
        send_notification(donation)
        mark_notified(stripe_session_id)

    return JSONResponse({
        "status":  "ok",
        "matched": matched,
    })


@app.get("/admin/donations")
def get_donations():
    """
    Admin endpoint — returns all donation records, newest first.

    TODO: Add authentication before exposing publicly.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    created_at,
                    donor_first,
                    donor_last,
                    payer_email,
                    donor_phone,
                    qty,
                    amount_pence,
                    on_behalf_of,
                    special_instructions,
                    comms_opt_in,
                    paid,
                    notified,
                    stripe_session_id
                FROM qurbani_donations
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()

        return {
            "count":     len(rows),
            "donations": [dict(r) for r in rows],
        }
    finally:
        conn.close()


# ================================================================================
#  PRESERVED ORIGINAL CODE (v1.0)
#  ─────────────────────────────────────────────────────────────────────────────
#  The original working code is kept below as comments for reference.
#  Use this if you need to understand what changed or roll back.
# ================================================================================

"""
# ── ORIGINAL save_donation (v1) ──────────────────────────────────────────────

def save_donation_v1(stripe_session_id, payer_name, payer_email, on_behalf_of, qty, amount_pence):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                INSERT INTO qurbani_donations
                    (stripe_session_id, payer_name, payer_email,
                     on_behalf_of, qty, amount_pence, paid)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (stripe_session_id) DO NOTHING,
                (stripe_session_id, payer_name, payer_email, on_behalf_of, qty, amount_pence)
            )
            conn.commit()
    finally:
        conn.close()


# ── ORIGINAL webhook handler (v1) ────────────────────────────────────────────

@app.post("/webhook/stripe_v1_reference")
async def stripe_webhook_v1(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    body = json.loads(payload)
    if body.get("type") != "checkout.session.completed":
        return JSONResponse({"status": "ignored", "event": body.get("type")})

    obj = body["data"]["object"]
    customer_details = obj.get("customer_details") or {}
    payer_name  = customer_details.get("name") or "Unknown"
    payer_email = customer_details.get("email") or ""

    client_ref = obj.get("client_reference_id") or "{}"
    try:
        ref_data     = json.loads(client_ref)
        on_behalf_of = ref_data.get("names", "")
        qty          = int(ref_data.get("qty", 1))
    except Exception:
        on_behalf_of = ""
        qty          = 1

    amount_pence      = obj.get("amount_total") or (qty * GOAT_PRICE_PENCE)
    stripe_session_id = obj.get("id")

    try:
        save_donation_v1(stripe_session_id, payer_name, payer_email,
                         on_behalf_of, qty, amount_pence)
        print(f"[forgottenmuslims] Saved: {payer_name} · £{amount_pence/100:.2f} · {stripe_session_id}")
    except Exception as e:
        print(f"[forgottenmuslims] DB error: {e}")
        raise HTTPException(status_code=500, detail="Database write failed")

    send_notification({
        "payer_name": payer_name, "payer_email": payer_email,
        "on_behalf_of": on_behalf_of, "qty": qty,
        "amount_pence": amount_pence, "stripe_session_id": stripe_session_id,
    })

    return JSONResponse({"status": "ok"})


# ── FUTURE: Token-based matching (v3 upgrade path) ───────────────────────────
# If you want 100% deterministic matching (useful at higher donation volumes),
# upgrade to this approach instead of time+amount matching:
#
# 1. POST /donations/pending returns a unique token (UUID)
# 2. Frontend appends to Stripe URL: ?client_reference_id=TOKEN
# 3. Stripe passes client_reference_id through to the webhook event
# 4. Webhook looks up by token:
#
#    cur.execute(
#        SELECT id FROM qurbani_donations
#        WHERE stripe_session_id = %s AND paid = FALSE,
#        (f"PENDING_{token}",)
#    )
#
# The pending_token UUID already used in save_pending_donation()
# is designed to be compatible with this upgrade path.
"""
