"""
Quick standalone test to confirm Supabase connection works
before wiring it into the real backend. Safe to delete after.
"""
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SECRET_KEY")

if not url or not key:
    print("ERROR: SUPABASE_URL or SUPABASE_SECRET_KEY missing from .env")
    exit(1)

supabase = create_client(url, key)

# Try writing one test row into the sessions table
test_row = {
    "session_id": "test-session-001",
    "state_json": {"hello": "world", "test": True},
}

result = supabase.table("sessions").upsert(test_row).execute()
print("Write successful:", result.data)

# Now try reading it back
read_result = supabase.table("sessions").select("*").eq("session_id", "test-session-001").execute()
print("Read back:", read_result.data)

print("\nSupabase connection is working correctly.")
