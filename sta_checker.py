"""
FCC STA Tracker - Starter Script (v2)
Monitors Special Temporary Authorization applications for "D-Fend Solutions"
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

# ============================================================
# EMAIL CONFIG
# ============================================================
import os
APPLICANT_NAME = "D-Fend Solutions"
DB_PATH = Path("sta_tracker.db")
SEARCH_URL = "https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Comma-separated list from the secret, or fallback
_recipients_raw = os.getenv("ALERT_RECIPIENTS", "")
ALERT_RECIPIENTS = [r.strip() for r in _recipients_raw.split(",") if r.strip()]

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            file_number     TEXT PRIMARY KEY,
            applicant       TEXT,
            status          TEXT,
            call_sign       TEXT,
            receipt_date    TEXT,
            grant_date      TEXT,
            sta_start_date  TEXT,
            sta_expiration_date TEXT,
            city            TEXT,
            state           TEXT,
            application_seq TEXT,
            last_seen       TEXT,
            first_seen      TEXT,
            raw_data        TEXT
        )
    """)

    # Add columns if the table already exists from earlier runs
    try:
        cur.execute("ALTER TABLE applications ADD COLUMN city TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE applications ADD COLUMN state TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE applications ADD COLUMN application_seq TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE applications ADD COLUMN sta_start_date TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE applications ADD COLUMN sta_expiration_date TEXT")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT,
            records_found INTEGER,
            notes TEXT
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database ready: {DB_PATH.absolute()}")


def load_previous_state() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Order by receipt_date descending (newest first)
    rows = conn.execute("""
            SELECT * FROM applications
            ORDER BY 
                CASE 
                    WHEN receipt_date = '' OR receipt_date IS NULL THEN '0000-00-00'
                    ELSE substr(receipt_date, 7, 4) || '-' || substr(receipt_date, 1, 2) || '-' || substr(receipt_date, 4, 2)
                END DESC
        """).fetchall()
    conn.close()
    return {row["file_number"]: dict(row) for row in rows}


def save_state(records: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for rec in records:
        file_number = rec.get("file_number")
        if not file_number:
            continue

        cur.execute("SELECT first_seen FROM applications WHERE file_number = ?", (file_number,))
        existing = cur.fetchone()
        first_seen = existing[0] if existing else now

        cur.execute("""
            INSERT INTO applications (
                file_number, applicant, status, call_sign,
                receipt_date, grant_date, sta_start_date, sta_expiration_date,
                city, state, application_seq,
                last_seen, first_seen, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_number) DO UPDATE SET
                status = excluded.status,
                call_sign = excluded.call_sign,
                receipt_date = excluded.receipt_date,
                grant_date = excluded.grant_date,
                sta_start_date = excluded.sta_start_date,
                sta_expiration_date = excluded.sta_expiration_date,
                city = CASE WHEN excluded.city IS NOT NULL AND excluded.city <> ''
                            THEN excluded.city
                            ELSE applications.city
                        END,
                state = CASE WHEN excluded.state IS NOT NULL AND excluded.state <> ''
                             THEN excluded.state
                             ELSE applications.state
                        END,
                application_seq = excluded.application_seq,
                last_seen = excluded.last_seen,
                raw_data = excluded.raw_data
        """, (
            file_number,
            rec.get("applicant", APPLICANT_NAME),
            rec.get("status", "Unknown"),
            rec.get("call_sign", ""),
            rec.get("receipt_date", ""),
            rec.get("grant_date", ""),
            rec.get("sta_start_date", ""),
            rec.get("sta_expiration_date", ""),
            rec.get("city", ""),
            rec.get("state", ""),
            rec.get("application_seq", ""),
            now,
            first_seen,
            json.dumps(rec),
        ))

    conn.commit()
    conn.close()
    print(f"Saved {len(records)} records.")

# ============================================================
# FETCH WITH PLAYWRIGHT
# ============================================================

def fetch_stas(applicant: str = APPLICANT_NAME) -> list[dict]:
    print(f"Launching browser and searching for '{applicant}'...")
    records = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            print("1. Opening search page...")
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            print("   Page loaded.")

            # Fill Applicant Name
            page.fill('input[name="name_licensee"]', applicant)

            # Ensure Special Temporary Authority is checked
            sta = page.locator('input[name="special_temporary_authority"]')
            if not sta.is_checked():
                sta.check()

            # ----- Status filters -----
            # Uncheck "Include all statuses"
            all_status = page.locator('input[name="all"]')
            if all_status.is_checked():
                all_status.uncheck()

            # Check only the statuses we want
            page.locator('input[name="granted"]').check()
            page.locator('input[name="pending"]').check()
            page.locator('input[name="dismissed"]').check()
            # Leave "expired" unchecked

            # ----- Records per page -----
            page.fill('input[name="show_records"]', "100")

            print("2. Submitting search (Granted + Pending + Dismissed, 100 records)...")
            page.click('input[type="submit"][value="Start Search"]')
            page.wait_for_load_state("domcontentloaded", timeout=90000)
            print("   Results page loaded.")

            html = page.content()
            Path("fcc_last_result.html").write_text(html, encoding="utf-8")
            print("   Saved fcc_last_result.html")

            # ---------- Improved parsing ----------
            from bs4 import BeautifulSoup
            import re

            soup = BeautifulSoup(html, "html.parser")

            # Find all data rows
            rows = soup.select("tr.rowprimary, tr.rowalternate")
            print(f"3. Found {len(rows)} data rows.")

            for row in rows:
                # Get all text cells
                cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]

                # Look for a File Number pattern like 1504-EX-ST-2026
                file_number = None
                for cell in cells:
                    match = re.search(r"\d{4}-EX-[A-Z]{2}-\d{4}", cell)
                    if match:
                        file_number = match.group(0)
                        break

                # Also try to pull it from links if not found in text
                if not file_number:
                    for a in row.find_all("a", href=True):
                        match = re.search(r"id_file_num=([^&]+)", a["href"])
                        if match:
                            file_number = match.group(1)
                            break

                if not file_number:
                    continue

                # Status is usually a short word near the end
                status = "Unknown"
                for cell in reversed(cells):
                    cell_lower = cell.lower()
                    if cell_lower in ("pending", "granted", "denied/dismissed", "expired",
                                      "grant expired due to new license", "dismissed"):
                        status = cell
                        break
                    if "pending" in cell_lower:
                        status = "Pending"
                        break
                    if "granted" in cell_lower and "expired" not in cell_lower:
                        status = "Granted"
                        break

                # Receipt / Status date – look for MM/DD/YYYY
                dates = re.findall(r"\d{2}/\d{2}/\d{4}", " ".join(cells))
                receipt_date = dates[0] if dates else ""
                status_date = dates[-1] if len(dates) > 1 else (dates[0] if dates else "")

                # Extract application_seq from the "Initial" or "Current" link
                application_seq = ""
                for a in row.find_all("a", href=True):
                    match = re.search(r"application_seq=(\d+)", a["href"])
                    if match:
                        application_seq = match.group(1)
                        break

                record = {
                    "file_number": file_number,
                    "status": status,
                    "applicant": applicant,
                    "call_sign": "",
                    "receipt_date": receipt_date,
                    "grant_date": status_date if status.lower() == "granted" else "",
                    "sta_start_date": "",
                    "sta_expiration_date": "",
                    "city": "",
                    "state": "",
                    "application_seq": application_seq,
                    "raw_row": " | ".join(cells[:12]),  # keep a sample for debugging
                }
                records.append(record)

            print(f"4. Successfully parsed {len(records)} records.")

            # Quick preview
            for r in records[:len(records)]:
                print(f"   → {r['file_number']}  |  {r['receipt_date']}   |  {r['status']}   |  {r['grant_date']}")

        except Exception as e:
            print(f"Error during browser automation: {e}")
            try:
                Path("fcc_error_page.html").write_text(
                    page.content(),
                    encoding="utf-8"
                )
            except Exception:
                pass
            raise
        finally:
            browser.close()

    return records

def fetch_station_location(application_seq: str) -> tuple[str, str]:
    if not application_seq:
        return "", ""

    url = f"https://apps.fcc.gov/oetcf/els/reports/STA_Print.cfm?mode=initial&application_seq={application_seq}&RequestTimeout=1000"
    print(f"   Fetching location for application_seq={application_seq}...")

    try:
        import re
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            fieldset = page.locator('fieldset[title="Station Location"]')
            if fieldset.count() == 0:
                print("   → Station Location section not found")
                browser.close()
                return "", ""

            cells = fieldset.locator("td").all_inner_texts()
            cleaned = [" ".join(c.split()).strip() for c in cells]

            city = ""
            state = ""

            if len(cleaned) >= 3:
                candidate_city = cleaned[1]
                candidate_state = cleaned[2]

                # Only reject if it looks like a coordinate or frequency
                coord_or_freq = r"(\d+\.\d+|GHz|MHz|°|North\s+\d|South\s+\d|East\s+\d|West\s+\d|Within\s+\d|NAD\s*83)"

                if candidate_city and not re.search(coord_or_freq, candidate_city, re.IGNORECASE):
                    city = candidate_city

                if candidate_state and not re.search(coord_or_freq, candidate_state, re.IGNORECASE):
                    state = candidate_state

            browser.close()

            if city or state:
                print(f"   → Found: '{city}', '{state}'")
                return city, state
            else:
                print("   → Both City and State missing / invalid → storing as empty")
                return "", ""

    except Exception as e:
        print(f"   → Error fetching location: {e}")
        return "", ""

def fetch_operation_period(application_seq: str) -> tuple[str, str]:
    if not application_seq:
        return "", ""

    url = (
        f"https://apps.fcc.gov/oetcf/els/reports/STA_Print.cfm?mode=initial&application_seq={application_seq}&RequestTimeout=1000"
    )
    print(f"   Fetching operation period for application_seq={application_seq}...")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            fieldset = page.locator('fieldset[title="Requested Period of Operation"]')

            if fieldset.count() == 0:
                print("   → Requested Period of Operation section not found")
                browser.close()
                return "", ""

            rows = fieldset.locator("tr")

            sta_start_date = ""
            sta_expiration_date = ""

            for i in range(rows.count()):
                texts = rows.nth(i).locator("td").all_inner_texts()
                texts = [t.strip() for t in texts]

                for j, text in enumerate(texts):
                    lower = text.lower()

                    if "start date" in lower and j + 1 < len(texts):
                        sta_start_date = texts[j + 1]

                    if "end date" in lower and j + 1 < len(texts):
                        sta_expiration_date = texts[j + 1]

            browser.close()

            print(f"   → Found: Start='{sta_start_date}', End='{sta_expiration_date}'")
            return sta_start_date, sta_expiration_date

    except Exception as e:
        print(f"   → Error fetching operation period: {e}")
        return "", ""


# ============================================================
# CHANGE DETECTION + EMAIL EMPLOYEES
# ============================================================

def detect_changes(old: dict, new_records: list[dict]) -> list[tuple]:
    alerts = []
    new_dict = {r["file_number"]: r for r in new_records if r.get("file_number")}
    for fn, rec in new_dict.items():
        if fn not in old:
            alerts.append(("new", rec))
        else:
            old_status = (old[fn].get("status") or "").lower()
            new_status = (rec.get("status") or "").lower()
            if old_status == "pending" and new_status == "granted":
                alerts.append(("granted", rec))
    return alerts


import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email(subject: str, body: str, recipients: list[str] | None = None) -> bool:
    if recipients is None:
        recipients = ALERT_RECIPIENTS

    if not recipients:
        print("No recipients configured – email not sent.")
        return False

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"Email sent → {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

def parse_date(date_str: str):
    """Parse MM/DD/YYYY → datetime object. Returns None if invalid."""
    if not date_str or date_str.strip() in ("", "N/A"):
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y")
    except ValueError:
        return None

def build_weekly_summary() -> str:
    """Build the weekly summary email body from the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM applications").fetchall()
    conn.close()

    today = datetime.now(timezone.utc).date()
    one_week_ago = today - timedelta(days=7)
    three_months_from_now = today + timedelta(days=90)

    submitted_last_week = []
    still_pending = []
    granted_last_week = []
    expiring_soon = []

    for row in rows:
        file_number = row["file_number"]
        status = (row["status"] or "").strip()
        city = row["city"] or "N/A"
        state = row["state"] or "N/A"
        receipt = parse_date(row["receipt_date"])
        grant = parse_date(row["grant_date"])
        start = parse_date(row["sta_start_date"])
        expiration = parse_date(row["sta_expiration_date"])

        location = f"{city}, {state}"

        # 1. Submitted last week
        if receipt and one_week_ago <= receipt.date() <= today:
            submitted_last_week.append(
                f"  • {file_number} submitted on {row['receipt_date']} for {location}"
            )

        # 2. Still Pending
        if status.lower() == "pending" and receipt:
            days_pending = (today - receipt.date()).days
            still_pending.append(
                (days_pending, f"  • {file_number} submitted on {row['receipt_date']} for {location} ({days_pending} days since submission)")
            )

        # 3. Granted last week
        if grant and one_week_ago <= grant.date() <= today:
            days_to_grant = (grant.date() - receipt.date()).days if receipt else "?"
            granted_last_week.append(
                f"  • {file_number} granted on {row['grant_date']}, {days_to_grant} days after submission, for {location} beginning on {row['sta_start_date']}"
            )

        # 4. Expiring in the next 3 months
        if expiration and today <= expiration.date() <= three_months_from_now:
            days_until = (expiration.date() - today).days
            expiring_soon.append(
                (days_until, f"  • {file_number} expiring on {row['sta_expiration_date']} for {location} ({days_until} days until expiration)")
            )

    # Sort pending by oldest first, expiring by soonest first
    still_pending.sort(key=lambda x: x[0], reverse=True)
    expiring_soon.sort(key=lambda x: x[0])

    # Build the email body
    lines = []
    lines.append("FCC STA Weekly Summary – D-Fend Solutions")
    lines.append("=" * 50)
    lines.append("")

    lines.append(f"STA applications submitted last week: {len(submitted_last_week)}")
    if submitted_last_week:
        lines.extend(submitted_last_week)
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"STA applications still pending: {len(still_pending)}")
    if still_pending:
        lines.extend([item[1] for item in still_pending])
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"STA licenses granted last week: {len(granted_last_week)}")
    if granted_last_week:
        lines.extend(granted_last_week)
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"STA licenses expiring in the next 3 months: {len(expiring_soon)}")
    if expiring_soon:
        lines.extend([item[1] for item in expiring_soon])
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Report generated: {today.strftime('%Y-%m-%d')}")
    return "\n".join(lines)

def send_weekly_summary_if_monday():
    """Send the weekly summary only on Mondays."""
    today = datetime.now(timezone.utc)
    if today.weekday() == 0:  # Monday
        summary = build_weekly_summary()
        send_email(
            subject="FCC STA Weekly Summary – D-Fend Solutions",
            body=summary
        )
        print("Weekly summary email sent.")
    else:
        print("Not Monday – skipping weekly summary.")

# ============================================================
# MAIN
# ============================================================

def main():
    print(f"\n=== FCC STA Checker started at {datetime.now()} ===\n")
    init_db()

    previous = load_previous_state()
    print(f"Loaded {len(previous)} previously known applications.")

    current_records = fetch_stas()

    # Enrich records with City/State
    for rec in current_records:
        status_lower = (rec.get("status") or "").lower()
        if status_lower in ("pending", "granted", "denied/dismissed", "dismissed") and rec.get("application_seq"):
            # Only fetch if we don't already have the location stored
            existing = previous.get(rec["file_number"], {})
            if not existing.get("city") or not existing.get("state"):
                city, state = fetch_station_location(rec["application_seq"])
                rec["city"] = city
                rec["state"] = state

            if not existing.get("sta_start_date") or not existing.get("sta_expiration_date"):
                sta_start_date, sta_expiration_date = fetch_operation_period(
                    rec["application_seq"]
                )
                rec["sta_start_date"] = sta_start_date
                rec["sta_expiration_date"] = sta_expiration_date

    if not current_records:
        print("No records returned.")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO run_log (run_time, records_found, notes) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), 0, "No records")
        )
        conn.commit()
        conn.close()
        return

    alerts = detect_changes(previous, current_records)
    for event_type, rec in alerts:
        file_number = rec.get("file_number", "Unknown")
        status = rec.get("status", "Unknown")
        city = rec.get("city") or "N/A"
        state = rec.get("state") or "N/A"
        receipt = rec.get("receipt_date") or "N/A"
        grant_date = rec.get("grant_date") or "N/A"
        app_seq = rec.get("application_seq") or ""

        # Build direct link to the STA detail page
        if app_seq:
            detail_link = f"https://apps.fcc.gov/oetcf/els/reports/STA_Print.cfm?mode=initial&application_seq={app_seq}&RequestTimeout=1000"
        else:
            detail_link = "https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm"

        if event_type == "granted":
            subject = f"STA GRANTED: {file_number}"
            body = f"""A Special Temporary Authorization has been GRANTED.

    File Number: {file_number}
    Status: {status}
    Applicant: {rec.get('applicant', 'D-Fend Solutions')}
    City: {city}
    State: {state}
    Receipt Date: {receipt}
    Grant Date: {grant_date}

    Direct link to STA details:
    {detail_link}
    """
            send_email(subject, body)

        elif event_type == "new":
            subject = f"New STA filed: {file_number}"
            body = f"""A new STA application has been detected.

    File Number: {file_number}
    Status: {status}
    Applicant: {rec.get('applicant', 'D-Fend Solutions')}
    City: {city}
    State: {state}
    Receipt Date: {receipt}

    Direct link to STA details:
    {detail_link}
    """
            send_email(subject, body)

    save_state(current_records)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO run_log (run_time, records_found, notes) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), len(current_records), f"{len(alerts)} alerts")
    )
    conn.commit()
    conn.close()

    # Weekly summary (only on Mondays)
    send_weekly_summary_if_monday()

    print(f"\n=== Finished. {len(alerts)} alert(s). ===\n")

if __name__ == "__main__":
    main()