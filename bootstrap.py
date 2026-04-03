#!/usr/bin/env python3
"""
bootstrap.py — Bootstrap the Mac edge worker with Supabase credentials.
Fetches server_id, server_secret, and api_key from the server-bootstrap-public endpoint.
"""
import yaml
import sys
import json

# We'll use the existing Servidor Central server credentials
# since we're just connecting to the same backend
SUPABASE_URL = "https://dwtzkynjbcwrdqlnmktj.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImR3dHpreW5qYmN3cmRxbG5ta3RqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzAyNTE3MzQsImV4cCI6MjA4NTgyNzczNH0.8kI_9sMjEqGEHUPMhH6CqbIQr_uLsWFlB53lffC2RLo"


def bootstrap(server_id: str = None, server_secret: str = None, api_key: str = None):
    """Update config.yaml with bootstrap credentials."""
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    vs = config["viewsense"]

    if server_id:
        vs["server_id"] = server_id
    if server_secret:
        vs["server_secret"] = server_secret
    if api_key:
        vs["api_key"] = api_key

    # Set proper URLs
    vs["api_url"] = f"{SUPABASE_URL}/functions/v1/ingest-detections"
    vs["heartbeat_url"] = f"{SUPABASE_URL}/functions/v1/server-heartbeat"
    vs["roi_sync_url"] = f"{SUPABASE_URL}/functions/v1/roi-sync"
    vs["anon_key"] = ANON_KEY

    with open("config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print("✅ config.yaml updated with bootstrap credentials")
    print(f"   server_id: {vs.get('server_id')}")
    print(f"   api_key: {vs.get('api_key')[:16]}..." if vs.get('api_key') else "   api_key: NOT SET")


if __name__ == "__main__":
    if len(sys.argv) >= 4:
        bootstrap(sys.argv[1], sys.argv[2], sys.argv[3])
    else:
        print("Usage: python bootstrap.py <server_id> <server_secret> <api_key>")
        print("")
        print("Get these from the ViewSense Dashboard → Servidores Edge → click on your server")
        print("Or use the get-ingest-key endpoint to fetch the api_key")
        sys.exit(1)
