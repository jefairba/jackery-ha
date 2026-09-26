"""Tiny Home Assistant websocket client for validating this integration.

Runs each JSON command given on argv and prints one JSON result per line.
Needs HA_URL and HA_TOKEN in the environment and the `websockets` package.

Example (on a host with the token in its environment):
    python scripts/ha_ws.py '{"type":"get_states"}' '{"type":"system_log/list"}'
"""
import asyncio, json, os, sys
import websockets

URL = os.environ["HA_URL"].replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": os.environ["HA_TOKEN"]}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"auth failed: {auth.get('type')}")
        for n, raw in enumerate(sys.argv[1:], start=1):
            msg = json.loads(raw); msg["id"] = n
            await ws.send(json.dumps(msg))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == n:
                    print(json.dumps(reply.get("result") if reply.get("success") else reply.get("error")))
                    break

asyncio.run(main())
