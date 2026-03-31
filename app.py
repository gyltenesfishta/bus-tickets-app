from flask import Flask, jsonify, request
import sqlite3
from flask_cors import CORS
from db import get_connection
import hmac
import hashlib
from flask_mail import Mail, Message
import os
import smtplib
import qrcode
from io import BytesIO
from datetime import datetime, timedelta
import stripe
from flask import Flask, request, jsonify
import os
from dotenv import load_dotenv


load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")




app = Flask(__name__)
CORS(app)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD") 
app.config['MAIL_DEFAULT_SENDER'] = "gyltene.sfishta@gmail.com"

mail = Mail(app)

SECRET_KEY = os.getenv("APP_SECRET_KEY").encode()

@app.route("/api/tickets", methods=["POST"])
def api_tickets():
    data = request.json or {}
    route_id = data.get("trip_id")   
    adults = data.get("adults", 0)
    children = data.get("children", 0)
    email = data.get("email")

    return_date = data.get("return_date")
    is_round_trip = bool(return_date)

    requested_date = data.get("date")
    requested_time = data.get("selected_departure_time")

    if not route_id:
        return jsonify({"error": "trip_id is required"}), 400

    try:
        adults = int(adults)
        children = int(children)
    except (TypeError, ValueError):
        return jsonify({"error": "adults/children must be integers"}), 400

    if adults < 0 or children < 0:
        return jsonify({"error": "adults/children must be >= 0"}), 400

    count = adults + children
    if count < 1:
        return jsonify({"error": "At least 1 passenger is required"}), 400

    if not email:
        return jsonify({"error": "email is required"}), 400

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        trip_row = None
        template = None

        # 1) Gjej trip-in per date/ore
        if requested_date:
            if requested_time:
                trip_row = cur.execute(
                    """
                    SELECT id AS trip_id, total_seats, base_price, departure_at
                    FROM trips
                    WHERE route_id = ?
                      AND DATE(departure_at) = ?
                      AND strftime('%H:%M', departure_at) = ?
                    ORDER BY departure_at
                    LIMIT 1
                    """,
                    (route_id, requested_date, requested_time),
                ).fetchone()
            else:
                trip_row = cur.execute(
                    """
                    SELECT id AS trip_id, total_seats, base_price, departure_at
                    FROM trips
                    WHERE route_id = ?
                      AND DATE(departure_at) = ?
                    ORDER BY departure_at
                    LIMIT 1
                    """,
                    (route_id, requested_date),
                ).fetchone()

            # Nese ska trip per ate date/ore -> krijo nje te ri nga template
            if trip_row is None:
                template = cur.execute(
                    """
                    SELECT total_seats, base_price, departure_at
                    FROM trips
                    WHERE route_id = ?
                    ORDER BY departure_at
                    LIMIT 1
                    """,
                    (route_id,),
                ).fetchone()

                if template is None:
                    return jsonify({"error": "No template trip for this route"}), 404

                # ruaj oren: nese user ska zgjedh ore, merre nga template
                if requested_time:
                    time_str = requested_time
                else:
                    departure_dt_tmp = datetime.fromisoformat(template["departure_at"])
                    time_str = departure_dt_tmp.strftime("%H:%M")

                departure_dt = datetime.fromisoformat(f"{requested_date}T{time_str}:00")

                cur.execute(
                    """
                    INSERT INTO trips (route_id, departure_at, total_seats, base_price)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        route_id,
                        departure_dt.isoformat(timespec="seconds"),
                        template["total_seats"],
                        template["base_price"],
                    ),
                )
                trip_id = cur.lastrowid
                total_seats = template["total_seats"]
                base_price = template["base_price"]
            else:
                trip_id = trip_row["trip_id"]
                total_seats = trip_row["total_seats"]
                base_price = trip_row["base_price"]

        else:
            trip_row = cur.execute(
                """
                SELECT id AS trip_id, total_seats, base_price
                FROM trips
                WHERE route_id = ?
                ORDER BY departure_at
                LIMIT 1
                """,
                (route_id,),
            ).fetchone()

            if trip_row is None:
                return jsonify({"error": "No trip found for this route"}), 404

            trip_id = trip_row["trip_id"]
            total_seats = trip_row["total_seats"]
            base_price = trip_row["base_price"]

        # 3) Seats te zena
        taken_rows = cur.execute(
            """
            SELECT seat_no
            FROM tickets
            WHERE trip_id = ?
              AND status IN ('reserved', 'paid')
            ORDER BY seat_no
            """,
            (trip_id,),
        ).fetchall()
        taken_seats = {row["seat_no"] for row in taken_rows}

        # 4) Greedy: bllok seats ngjitur sa me afer mesit
        middle = total_seats / 2.0
        best_block = None
        best_distance = None

        for start in range(1, total_seats - count + 2):
            block = list(range(start, start + count))
            if any(seat in taken_seats for seat in block):
                continue

            center = start + (count - 1) / 2.0
            distance = abs(center - middle)
            if best_block is None or distance < best_distance:
                best_block = block
                best_distance = distance

        if best_block is None:
            return jsonify({"error": "Not enough adjacent seats available"}), 400

        tickets = []

        # 5) Krijo tickets + token (HMAC)
        try:
            for i, seat_no in enumerate(best_block):
                sold_count = len(taken_seats) + i
                occupancy = sold_count / total_seats if total_seats > 0 else 0

                if occupancy < 0.3:
                    factor = 1.0
                elif occupancy < 0.7:
                    factor = 1.2
                else:
                    factor = 1.5

                base_seat_price = round(base_price * factor, 2)
                round_trip_multiplier = 2 if is_round_trip else 1

                if i < adults:
                    price = round(base_seat_price * round_trip_multiplier, 2)
                    passenger_type = "adult"
                else:
                    price = round(base_seat_price * 0.9 * round_trip_multiplier, 2)
                    passenger_type = "child"

                message = f"{trip_id}:{seat_no}".encode("utf-8")
                full_token = hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()
                token = full_token[:16]

                cur.execute(
                    """
                    INSERT INTO tickets (trip_id, seat_no, price, status, token)
                    VALUES (?, ?, ?, 'reserved', ?)
                    """,
                    (trip_id, seat_no, price, token),
                )

                tickets.append(
                    {
                        "seat_no": seat_no,
                        "price": price,
                        "status": "reserved",
                        "token": token,
                        "passenger_type": passenger_type,
                    }
                )

            conn.commit()
        except Exception as e:
            conn.rollback()
            print("❌ /api/tickets error:", e)   
            return jsonify({"error": "Database error", "details": str(e)}), 500

    # 6) Dërgo email + bashkangjit QR (token-only)
    try:
        msg = Message(
            subject="Your Bus Ticket Reservation",
            recipients=[email],
        )

        body_lines = [
            "Thank you for your reservation!",
            f"Route ID: {route_id}",
            f"Trip ID: {trip_id}",
            f"Number of tickets: {len(tickets)}",
            "",
            "Ticket details:",
        ]
        for t in tickets:
            body_lines.append(f" - Seat {t['seat_no']}: {t['price']} € ({t['passenger_type']})")

        msg.body = "\n".join(body_lines)

        # QR attachments (një PNG për çdo token)
        for t in tickets:
            token = t["token"]
            qr_img = qrcode.make(token)   # mos e bë URL, vetëm token
            img_io = BytesIO()
            qr_img.save(img_io, format="PNG")
            img_io.seek(0)

            msg.attach(
                filename=f"ticket_{token}.png",
                content_type="image/png",
                data=img_io.read(),
            )

        mail.send(msg)

    except Exception as e:
        print("Failed to send email with QR:", e)

    return jsonify(
        {
            "trip_id": trip_id,
            "tickets": tickets,
            "base_price": base_price,
        }
    )



# ---------------------------------------------------
# 2) KONFIRMIMI I PAGESËS /api/tickets/confirm (POST)
# ---------------------------------------------------
@app.route("/api/tickets/confirm", methods=["POST"])
def api_confirm_tickets():
    data = request.json or {}
    tokens = data.get("tokens")

    if not tokens or not isinstance(tokens, list):
        return jsonify({"error": "tokens list is required"}), 400

    with get_connection() as conn:
        placeholders = ",".join(["?"] * len(tokens))

        rows = conn.execute(
            f"""
            SELECT id, token, status
            FROM tickets
            WHERE token IN ({placeholders})
            """,
            tokens,
        ).fetchall()

        if not rows:
            return jsonify({"error": "No tickets found for provided tokens"}), 404

        already_paid = [r["token"] for r in rows if r["status"] == "paid"]
        to_pay_ids = [r["id"] for r in rows if r["status"] == "reserved"]

        if to_pay_ids:
            placeholders_ids = ",".join(["?"] * len(to_pay_ids))
            conn.execute(
                f"UPDATE tickets SET status = 'paid' WHERE id IN ({placeholders_ids})",
                to_pay_ids,
            )
            conn.commit()

        return jsonify({
            "paid_count": len(to_pay_ids),
            "already_paid": already_paid,
            "total_found": len(rows),
            "tokens": tokens,
        })




@app.get("/api/tickets/<token>")
def get_ticket(token):
    conn = sqlite3.connect("app.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute(
        """
        SELECT 
            t.token,
            t.status,
            t.seat_no,
            t.price,
            tr.departure_at,
            r.origin,
            r.destination
        FROM tickets t
        JOIN trips tr ON t.trip_id = tr.id
        JOIN routes r ON tr.route_id = r.id
        WHERE t.token = ?
        """,
        (token,)
    ).fetchone()

    if row is None:
        return jsonify({"error": "Bileta nuk u gjet."}), 404

    # ---- VALIDITY WINDOW ----
    departure = datetime.fromisoformat(row["departure_at"])
    now = datetime.now()

    valid_from = departure - timedelta(hours=1)
    valid_until = departure + timedelta(hours=1)

    if now < valid_from:
        return jsonify({
            "error": "Ticket not valid yet.",
            "status": "not_valid_yet",
            "departure_at": row["departure_at"]
        }), 400

    if now > valid_until:
        return jsonify({
            "error": "Ticket expired.",
            "status": "expired",
            "departure_at": row["departure_at"]
        }), 400

  

    return jsonify({
        "token": row["token"],
        "status": row["status"],
        "seat_no": row["seat_no"],
        "price": row["price"],
        "departure_at": row["departure_at"],
        "from": row["origin"],
        "to": row["destination"]
    })


@app.post("/api/tickets/<token>/checkin")
def checkin_ticket(token):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT t.id, t.status, tr.departure_at
            FROM tickets t
            JOIN trips tr ON t.trip_id = tr.id
            WHERE t.token = ?
            """,
            (token,),
        ).fetchone()

        if not row:
            return jsonify({"error": "Ticket not found"}), 404

        status = row["status"]
        if status == "reserved":
            return jsonify({"error": "Ticket is not paid yet."}), 400

        if status == "used":
            return jsonify({"error": "Ticket already used."}), 400

        # ---- VALIDITY CHECK ----
        from datetime import datetime, timedelta

        departure = datetime.fromisoformat(row["departure_at"])
        now = datetime.now()

        valid_from = departure - timedelta(hours=1)
        valid_until = departure + timedelta(hours=1)

        if now < valid_from:
            return jsonify({"error": "Ticket not valid yet."}), 400

        if now > valid_until:
            return jsonify({"error": "Ticket expired."}), 400


        # check-in OK
        conn.execute(
            "UPDATE tickets SET status = 'used' WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

    return jsonify({"ok": True, "status": "used"})
    

@app.get("/api/stats/routes")
def stats_routes():
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = cur.execute(
            """
            SELECT
                r.name AS route_name,
                COUNT(t.id) AS tickets_sold,
                COALESCE(SUM(t.price), 0) AS total_revenue,
                CASE
                    WHEN COUNT(t.id) > 0 THEN ROUND(AVG(t.price), 2)
                    ELSE 0
                END AS avg_price
            FROM routes r
            LEFT JOIN trips tr ON tr.route_id = r.id
            LEFT JOIN tickets t ON t.trip_id = tr.id
                  AND t.status IN ('paid', 'used')
            GROUP BY r.id
            ORDER BY r.id;
            """
        ).fetchall()

    stats = []
    for row in rows:
        stats.append({
            "route_name": row["route_name"],
            "tickets_sold": row["tickets_sold"],
            "total_revenue": row["total_revenue"],
            "avg_price": row["avg_price"],
        })

    return jsonify({"routes": stats})

@app.route("/api/payments/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.get_json() or {}
    tokens = data.get("tokens", [])
    raw_amount = data.get("amount") 
    email = data.get("email")

    
    try:
        amount_cents = int(round(float(raw_amount)))
    except (TypeError, ValueError):
        return {"error": "Invalid amount"}, 400

    if not tokens or amount_cents <= 0:
        return {"error": "Missing tokens or amount"}, 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {
                            "name": f"Bus tickets ({len(tokens)} passenger(s))",
                        },
                        "unit_amount": amount_cents,  
                    },
                    "quantity": 1,
                }
            ],
            success_url="http://localhost:5173/payment-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="http://localhost:5173/payment-cancelled",
            metadata={
                "ticket_tokens": ",".join(tokens),
            },
        )

        return {"url": session.url}
    except Exception as e:
        print("Stripe error:", e)
        return {"error": "Failed to create checkout session"}, 500



@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        print("❌ Invalid payload")
        return "", 400
    except stripe.error.SignatureVerificationError:
        print("❌ Invalid signature")
        return "", 400

    print("✅ Webhook event received:", event["type"])

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        metadata = session.get("metadata", {}) or {}
        tokens_str = metadata.get("ticket_tokens", "")
        tokens = [t for t in tokens_str.split(",") if t]

        print("💳 Payment completed for tokens:", tokens)

        if tokens:
            
            with get_connection() as conn:
                placeholders = ",".join(["?"] * len(tokens))

                rows = conn.execute(
                    f"""
                    SELECT id, token, status
                    FROM tickets
                    WHERE token IN ({placeholders})
                    """,
                    tokens,
                ).fetchall()

                if not rows:
                    print("⚠️ No tickets found for provided tokens in webhook.")
                else:
                    already_paid = [r["token"] for r in rows if r["status"] == "paid"]
                    to_pay_ids = [r["id"] for r in rows if r["status"] == "reserved"]

                    if to_pay_ids:
                        placeholders_ids = ",".join(["?"] * len(to_pay_ids))
                        conn.execute(
                            f"UPDATE tickets SET status = 'paid' WHERE id IN ({placeholders_ids})",
                            to_pay_ids,
                        )
                        conn.commit()

                    print(
                        f"✅ Tickets updated via webhook. "
                        f"paid_count={len(to_pay_ids)}, already_paid={already_paid}, tokens={tokens}"
                    )
        else:
            print("⚠️ No ticket_tokens found in session metadata.")

    
    return "", 200


@app.route("/api/stats/monthly-revenue")
def monthly_revenue():
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Marrim të ardhurat mujore sipas DATËS SË UDHËTIMIT 
        rows = cur.execute(
            """
            SELECT
                strftime('%Y', tr.departure_at) AS year,
                strftime('%m', tr.departure_at) AS month,
                COALESCE(SUM(t.price), 0) AS total_revenue
            FROM tickets t
            JOIN trips tr ON t.trip_id = tr.id
            WHERE t.status = 'paid'
            GROUP BY year, month
            ORDER BY year, month
            """
        ).fetchall()

    months = []

    if rows:
        year = int(rows[0]["year"])
        revenue_by_month = {
            int(r["month"]): float(r["total_revenue"] or 0.0)
            for r in rows
            if r["year"] == str(year)
        }
    else:
        year = datetime.now().year
        revenue_by_month = {}

    for m in range(1, 13):
        label = f"{m:02d}/{year}"          
        month_key = f"{year}-{m:02d}"      
        total = revenue_by_month.get(m, 0.0)

        months.append(
            {
                "month": month_key,
                "label": label,
                "total_revenue": total,
            }
        )

    return jsonify({"months": months})




if __name__ == "__main__":
    app.run(debug=True)