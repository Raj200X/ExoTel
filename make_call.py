"""
meta-NFS Trigger Outbound Test Call via Exotel API

Usage:
    python make_call.py <YOUR_MOBILE_NUMBER>

Example:
    python make_call.py 9876543210
"""

import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()


def trigger_call(to_phone_number: str):
    account_sid = os.getenv("EXOTEL_ACCOUNT_SID")
    api_key = os.getenv("EXOTEL_API_KEY")
    api_token = os.getenv("EXOTEL_API_TOKEN")
    virtual_number = os.getenv("EXOTEL_VIRTUAL_NUMBER", "").replace("-", "").strip()

    if not all([account_sid, api_key, api_token, virtual_number]):
        print("❌ Missing Exotel credentials in .env file.")
        return

    # Clean phone number
    clean_number = to_phone_number.strip().replace("+", "").replace("-", "")
    if len(clean_number) == 10:
        clean_number = "0" + clean_number  # Exotel standard format for Indian numbers

    url = f"https://{api_key}:{api_token}@api.exotel.com/v1/Accounts/{account_sid}/Calls/connect.json"

    data = {
        "From": clean_number,
        "To": virtual_number,
        "CallerId": virtual_number,
        "CallType": "trans",
    }

    print("=" * 60)
    print("         meta-NFS Outbound Phone Call Trigger")
    print("=" * 60)
    print(f"Connecting Exotel Virtual Number ({virtual_number}) -> {clean_number}...")

    try:
        r = httpx.post(url, data=data, timeout=15.0)
        if r.status_code == 200:
            res_json = r.json()
            call_id = res_json.get("Call", {}).get("Sid")
            status = res_json.get("Call", {}).get("Status")
            print("✅ Call Triggered SUCCESSFULLY!")
            print(f"   Call SID : {call_id}")
            print(f"   Status   : {status}")
            print(f"   Your phone ({clean_number}) should ring in a few seconds!")
        else:
            print(f"❌ Exotel API Error ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"❌ Connection Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_call.py <YOUR_10_DIGIT_MOBILE_NUMBER>")
        sys.exit(1)

    trigger_call(sys.argv[1])
