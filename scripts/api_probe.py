#!/usr/bin/env python3
"""Read-only Jackery cloud probe for debugging, run outside Home Assistant.

Logs in, lists devices and dumps the first device's properties. It never
sends a command. Note: logging in displaces any other session on the same
account (error 10403), so the phone app and HA will be signed out.

Usage: python scripts/api_probe.py <username>   (password is prompted)
"""

import getpass
import importlib.util
import logging
import sys
from pathlib import Path

# Load api.py by path. Putting the integration folder on sys.path would let
# its select.py shadow the stdlib select module that requests depends on.
_API_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "jackery" / "api.py"
_spec = importlib.util.spec_from_file_location("jackery_api", _API_PATH)
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
JackeryAPI = _api.JackeryAPI
JackeryAuthenticationError = _api.JackeryAuthenticationError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def test_jackery_api(username, password):
    """Test the Jackery API connection."""
    print(f"Testing Jackery API with username: {username}")

    try:
        # Create API instance
        api = JackeryAPI(account=username, password=password)

        # Test login
        print("Testing login...")
        if api.login():
            print("✅ Login successful!")
        else:
            print("❌ Login failed!")
            return False

        # Test device list
        print("Testing device list...")
        device_list = api.get_device_list()
        devices = device_list.get("data", [])
        print(f"✅ Found {len(devices)} devices")

        for device in devices:
            device_id = device.get("devId")
            device_name = device.get("devName", "Unknown")
            print(f"  - Device: {device_name} (ID: {device_id})")

        # Test device detail for first device
        if devices:
            first_device = devices[0]
            device_id = first_device["devId"]
            print(f"Testing device detail for {device_id}...")

            device_detail = api.get_device_detail(device_id)
            properties = device_detail.get("data", {}).get("properties", {})
            print(f"✅ Device detail retrieved with {len(properties)} properties")

            for key, value in properties.items():
                print(f"  - {key}: {value}")

        return True

    except JackeryAuthenticationError as e:
        print(f"❌ Authentication error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/api_probe.py <username>")
        sys.exit(1)

    username = sys.argv[1]
    # Prompt rather than take argv: argv lands in shell history and `ps`.
    password = getpass.getpass("Jackery password: ")

    success = test_jackery_api(username, password)
    sys.exit(0 if success else 1)
