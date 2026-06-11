#!/usr/bin/env python3
"""
AlayaCare Client Creation Agent

Reads a My Aged Care (MAC) referral PDF, extracts client details using Claude,
and creates a new fully-populated client in AlayaCare via a 3-step workflow:
  1. POST /clients          — create client (basic demographics + external_id)
  2. PUT  /clients/{id}     — update with full demographics (DOB, phone, address, Medicare)
  3. POST /contacts         — create self-contact record (phone, email, address)

Usage:
    python alayacare_create_client.py <path_to_pdf> [--dry-run]

Environment variables (set in .env or shell):
    ALAYACARE_SERVER      e.g. https://asa.uat.alayacare.com
    ALAYACARE_USERNAME    Basic Auth username
    ALAYACARE_PASSWORD    Basic Auth password
    ANTHROPIC_API_KEY     Your Anthropic API key
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ALAYACARE_SERVER   = os.environ.get("ALAYACARE_SERVER", "https://asa.uat.alayacare.com")
ALAYACARE_USERNAME = os.environ.get("ALAYACARE_USERNAME", "")
ALAYACARE_PASSWORD = os.environ.get("ALAYACARE_PASSWORD", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_URL = f"{ALAYACARE_SERVER}/ext/api/v2"

EXTRACTION_PROMPT = """You are an expert at reading My Aged Care (MAC) referral documents from the Australian Department of Health and mapping their fields to AlayaCare client intake data.

From the provided MAC PDF, extract ALL available client information and return a JSON object with EXACTLY this structure.
Only include fields where data is actually present — omit fields that are "not applicable", blank, or "N/A".

{
  "demographics": {
    "first_name": "string (REQUIRED)",
    "last_name":  "string",
    "gender":     "M | F | O  (M=Male, F=Female, O=Other/Unknown)"
  },
  "external_id":           "string — the Aged Care ID value (e.g. AC27413301)",
  "language":              "string — ISO 639-1 code (English→en, French→fr, Mandarin→zh)",
  "timezone":              "string — IANA timezone derived from state in address (SA→Australia/Adelaide, NSW/ACT→Australia/Sydney, VIC→Australia/Melbourne, QLD→Australia/Brisbane, WA→Australia/Perth, NT→Australia/Darwin, TAS→Australia/Hobart)",
  "correspondence_method": "email | mail",
  "intake_group":          true,

  "_update_demographics": {
    "birthday":         "YYYY-MM-DD (convert DD/MM/YYYY from PDF)",
    "phone_personal":   "mobile phone number as string",
    "phone_main":       "home phone number as string",
    "email":            "email address",
    "email_preferred":  "email address (same as email if present)",
    "address":          "street address only (number + street name)",
    "city":             "suburb/city",
    "state":            "2-letter state code (SA, NSW, VIC, etc.)",
    "zip":              "postcode as string",
    "country":          "Australia",
    "health_card":      "Medicare number as string",
    "care_needs":       "brief summary of care needs from assessment and interactions (max 200 chars)"
  },

  "_self_contact": {
    "first_name": "same as demographics.first_name",
    "last_name":  "same as demographics.last_name",
    "gender":     "same as demographics.gender",
    "phone_main": "mobile phone — same as _update_demographics.phone_personal",
    "address":    "same as _update_demographics.address",
    "city":       "same as _update_demographics.city",
    "state":      "same as _update_demographics.state",
    "zip":        "same as _update_demographics.zip",
    "country":    "Australia"
  },

  "_info": {
    "date_of_birth":                        "DD/MM/YYYY as shown on document",
    "medicare_number":                      "string",
    "aboriginal_torres_strait_islander":    true,
    "marital_status":                       "string",
    "lives_with":                           "string",
    "accommodation_type":                   "string",
    "dva_card":                             "string or null",
    "assessment_date":                      "date of most recent comprehensive assessment",
    "approved_services":                    ["list of service names from Care Approvals section"],
    "health_summary":                       "free-text summary of health conditions from Interactions"
  }
}

Field mapping rules:
- "Preferred correspondence method: Post" → correspondence_method = "mail"
- "Preferred Language: English" → language = "en"
- DOB "04/12/1965" → birthday = "1965-12-04"
- State "SA" in address → timezone = "Australia/Adelaide"
- Gender "Male" → "M",  "Female" → "F"
- care_needs: summarise from assessment recommended services + interaction health notes

Return ONLY the JSON object — no markdown fences, no explanation text."""


def extract_client_from_pdf(pdf_path: str, client: anthropic.Anthropic) -> dict:
    """Use Claude to extract structured client data from a MAC PDF."""
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64   = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip()
    return json.loads(raw)


def _api_headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json"}


def _auth():
    return (ALAYACARE_USERNAME, ALAYACARE_PASSWORD)


def step1_create_client(extracted: dict, dry_run: bool) -> dict | None:
    """POST /clients — create the client record."""
    api_fields = {
        "demographics", "external_id", "branch_id", "profile_id",
        "language", "groups", "timezone", "intake_group",
        "is_billing_contact", "correspondence_method",
    }
    payload = {k: v for k, v in extracted.items() if k in api_fields}

    print(f"\n[Step 1] POST {BASE_URL}/clients")
    print(json.dumps(payload, indent=2))

    if dry_run:
        return {"id": "DRY_RUN_ID", "external_id": payload.get("external_id")}

    resp = requests.post(
        f"{BASE_URL}/clients",
        json=payload,
        auth=_auth(),
        headers=_api_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def step2_update_demographics(client_id: str | int, extracted: dict, dry_run: bool) -> dict | None:
    """PUT /clients/{id} — patch with full demographics (DOB, phone, address, etc.)."""
    update_demo = extracted.get("_update_demographics", {})
    if not update_demo:
        print("\n[Step 2] No extended demographics to update — skipping.")
        return None

    payload = {"demographics": update_demo}
    print(f"\n[Step 2] PUT {BASE_URL}/clients/{client_id}")
    print(json.dumps(payload, indent=2))

    if dry_run:
        return {"updated": True}

    resp = requests.put(
        f"{BASE_URL}/clients/{client_id}",
        json=payload,
        auth=_auth(),
        headers=_api_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def step3_create_contact(client_id: str | int, extracted: dict, dry_run: bool) -> dict | None:
    """POST /contacts — create self-contact record for the client."""
    self_contact_demo = extracted.get("_self_contact", {})
    if not self_contact_demo:
        print("\n[Step 3] No self-contact data — skipping.")
        return None

    payload = {
        "client_id": int(client_id) if str(client_id).isdigit() else client_id,
        "contact_type": "Personal",
        "relationship": "Self",
        "is_billing_contact": False,
        "emergency": False,
        "language": extracted.get("language", "en"),
        "demographics": self_contact_demo,
    }
    print(f"\n[Step 3] POST {BASE_URL}/contacts")
    print(json.dumps(payload, indent=2))

    if dry_run:
        return {"id": "DRY_RUN_CONTACT_ID"}

    resp = requests.post(
        f"{BASE_URL}/contacts",
        json=payload,
        auth=_auth(),
        headers=_api_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def print_confirmation_table(extracted: dict) -> None:
    """Print a human-readable confirmation table before creating the client."""
    demo  = extracted.get("demographics", {})
    upd   = extracted.get("_update_demographics", {})
    info  = extracted.get("_info", {})

    w = 60
    print("\n" + "=" * w)
    print("  EXTRACTED CLIENT — PLEASE CONFIRM")
    print("=" * w)
    rows = [
        ("Name",              f"{demo.get('first_name', '')} {demo.get('last_name', '')}"),
        ("Gender",            demo.get("gender", "N/A")),
        ("Date of Birth",     info.get("date_of_birth") or upd.get("birthday", "N/A")),
        ("MAC Aged Care ID",  extracted.get("external_id", "N/A")),
        ("Language",          extracted.get("language", "N/A")),
        ("Timezone",          extracted.get("timezone", "N/A")),
        ("Correspondence",    extracted.get("correspondence_method", "N/A")),
        ("Phone (Mobile)",    upd.get("phone_personal", "N/A")),
        ("Phone (Home)",      upd.get("phone_main", "N/A")),
        ("Email",             upd.get("email", "N/A")),
        ("Address",           f"{upd.get('address','')} {upd.get('city','')} {upd.get('state','')} {upd.get('zip','')}".strip()),
        ("Medicare #",        info.get("medicare_number") or upd.get("health_card", "N/A")),
        ("ATSI Status",       "Yes" if info.get("aboriginal_torres_strait_islander") else "No"),
        ("Marital Status",    info.get("marital_status", "N/A")),
        ("Lives With",        info.get("lives_with", "N/A")),
        ("Accommodation",     info.get("accommodation_type", "N/A")),
    ]
    if info.get("approved_services"):
        rows.append(("Approved Services", f"{len(info['approved_services'])} services"))
    if info.get("health_summary"):
        summary = info["health_summary"][:70] + ("…" if len(info["health_summary"]) > 70 else "")
        rows.append(("Health Summary", summary))

    for label, value in rows:
        print(f"  {label:<20}: {value}")
    print("=" * w)


def main():
    parser = argparse.ArgumentParser(
        description="Create an AlayaCare client from a My Aged Care (MAC) PDF referral."
    )
    parser.add_argument("pdf", help="Path to the MAC referral PDF")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extract and show data without calling the AlayaCare API"
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        missing = [v for v in ("ALAYACARE_USERNAME", "ALAYACARE_PASSWORD") if not os.environ.get(v)]
        if missing:
            print(f"Error: Missing env vars: {', '.join(missing)}", file=sys.stderr)
            print("Set them in alayacare_agent/.env then retry.", file=sys.stderr)
            sys.exit(1)

    print(f"\nReading PDF: {pdf_path.name}")
    ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("Extracting client details with Claude…")
    try:
        extracted = extract_client_from_pdf(str(pdf_path), ac)
    except json.JSONDecodeError as e:
        print(f"Error: Claude returned non-JSON response: {e}", file=sys.stderr)
        sys.exit(1)

    print_confirmation_table(extracted)

    if not args.dry_run:
        confirm = input("\nCreate this client in AlayaCare? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    # ── Step 1: Create client ─────────────────────────────────────────────────
    try:
        client_result = step1_create_client(extracted, args.dry_run)
    except requests.HTTPError as e:
        print(f"\nStep 1 failed — {e.response.status_code}: {e.response.text}", file=sys.stderr)
        if e.response.status_code == 409:
            print("Hint: A client with this external_id already exists. Check for duplicates.")
        sys.exit(1)

    client_id = client_result.get("id") or client_result.get("client_id")
    print(f"\n  Client created — AlayaCare ID: {client_id}")

    # ── Step 2: Update with full demographics ────────────────────────────────
    try:
        step2_update_demographics(client_id, extracted, args.dry_run)
        print("  Demographics updated.")
    except requests.HTTPError as e:
        print(f"\nStep 2 warning — demographics update failed: {e.response.status_code}: {e.response.text}")
        print("  Client was created. You can update demographics manually in the portal.")

    # ── Step 3: Create self-contact record ───────────────────────────────────
    try:
        contact_result = step3_create_contact(client_id, extracted, args.dry_run)
        if contact_result:
            contact_id = contact_result.get("id") or contact_result.get("contact_id")
            print(f"  Self-contact created — Contact ID: {contact_id}")
    except requests.HTTPError as e:
        print(f"\nStep 3 warning — contact creation failed: {e.response.status_code}: {e.response.text}")
        print("  Client was created. You can add the contact manually in the portal.")

    # ── Summary ───────────────────────────────────────────────────────────────
    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'=' * 60}")
    print(f"  {mode}CLIENT CREATION COMPLETE")
    print(f"  AlayaCare Client ID : {client_id}")
    print(f"  MAC Aged Care ID    : {extracted.get('external_id', 'N/A')}")
    print(f"  Portal              : {ALAYACARE_SERVER}/patients/clients/{client_id}/details")
    print(f"{'=' * 60}")

    if args.dry_run:
        print("\n[DRY RUN] No API calls were made. Remove --dry-run to create the client.")


if __name__ == "__main__":
    main()
