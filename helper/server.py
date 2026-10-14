#!/usr/bin/env python3
"""Loopback-only GD plan helper. Demo mode never calls an AI service."""
import argparse
import hmac
import json
import math
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT = 64
MAX_BODY = 32768
COORD_LIMIT = 100000
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "objects": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "enum": [1]},
                "x": {"type": "number"}, "y": {"type": "number"}
            },
            "required": ["id", "x", "y"], "additionalProperties": False
        }}
    },
    "required": ["summary", "objects"], "additionalProperties": False
}
INSTRUCTIONS = """You plan additions to the Geometry Dash editor.
This prototype ONLY supports adding standard square blocks (object ID 1).
A block is 30 by 30 editor units; x and y are its CENTER.
Use the requested origin as the first block center. Do not snap or change it.
For a normal horizontal row, advance x by 30 for each next block.
Use at most 64 blocks and coordinates within +/-100000.
Selected object data is context only: never delete, move, or recolor it.
If the request needs spikes, decoration, colors, triggers or other unavailable
features, return no objects and a short explanation of the limitation.
Do not claim a layout was playtested or is playable.
Return only the requested JSON schema, with a short plain-text summary.
"""


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Coordinates must be finite numbers.")
    if abs(value) > COORD_LIMIT:
        raise ValueError("Coordinates must be within +/-100000.")
    return float(value)


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"prompt", "origin", "selected"}:
        raise ValueError("Expected prompt, origin, and selected.")
    prompt = value["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 500:
        raise ValueError("Enter a request between 1 and 500 characters.")
    origin = value["origin"]
    if not isinstance(origin, dict) or set(origin) != {"x", "y"}:
        raise ValueError("Origin needs x and y.")
    selected = value["selected"]
    if not isinstance(selected, list) or len(selected) > LIMIT:
        raise ValueError("Select at most 64 objects for this prototype.")
    clean = []
    for obj in selected:
        if not isinstance(obj, dict) or set(obj) != {"id", "x", "y"}:
            raise ValueError("Invalid selected object.")
        if type(obj["id"]) is not int or not 1 <= obj["id"] <= 10000:
            raise ValueError("Invalid selected object ID.")
        clean.append({"id": obj["id"], "x": number(obj["x"]), "y": number(obj["y"])})
    return {"prompt": prompt.strip(), "origin": {
        "x": number(origin["x"]), "y": number(origin["y"])
    }, "selected": clean}


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {"summary", "objects"}:
        raise ValueError("The plan has an invalid structure.")
    summary, objects = value["summary"], value["objects"]
    if not isinstance(summary, str) or not 1 <= len(summary) <= 300:
        raise ValueError("The plan needs a short summary.")
    if not isinstance(objects, list) or len(objects) > LIMIT:
        raise ValueError("The plan exceeds the 64-block limit.")
    clean = []
    locations = set()
    for obj in objects:
        if not isinstance(obj, dict) or set(obj) != {"id", "x", "y"}:
            raise ValueError("The plan contains unsupported properties.")
        if type(obj["id"]) is not int or obj["id"] != 1:
            raise ValueError("Only standard block ID 1 is supported.")
        x, y = number(obj["x"]), number(obj["y"])
        if (x, y) in locations:
            raise ValueError("The plan contains duplicate block locations.")
        locations.add((x, y))
        clean.append({"id": 1, "x": x, "y": y})
    return {"summary": summary, "objects": clean}


def demo_plan(request):
    # Explicit deterministic transport test, NOT an AI simulation.
    prompt = request["prompt"].lower()
    match = re.fullmatch(r"(?:make|create|add) (\d+) (?:blocks|squares)(?: in a (?:horizontal )?row)?[.!]?", prompt)
    if not match:
        return {"summary": "Demo mode: try 'make 10 blocks in a row'.", "objects": []}
    count = int(match[1])
    if not 1 <= count <= LIMIT:
        raise ValueError("Demo requests must contain 1 to 64 blocks.")
    origin = request["origin"]
    return validate_plan({"summary": f"DEMO: {count} basic blocks, spaced 30 units apart.",
                          "objects": [{"id": 1, "x": origin["x"] + 30 * i,
                                       "y": origin["y"]} for i in range(count)]})


def ai_plan(request, api_key, model):
    payload = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": json.dumps(request, allow_nan=False),
        "max_output_tokens": 4096,
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "gd_block_plan",
                            "strict": True, "schema": PLAN_SCHEMA}}
    }
    req = urllib.request.Request("https://api.openai.com/v1/responses",
          data=json.dumps(payload).encode(),
          headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
          method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Never return raw upstream messages, prompts or secrets to the game.
        raise RuntimeError(f"AI API returned HTTP {error.code}. Check billing, model access, and API key.") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Could not reach the AI API. Check your internet connection.") from None
    if result.get("status") != "completed":
        raise RuntimeError("AI response did not complete. Try a smaller request.")
    chunks = []
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise RuntimeError("The AI declined this request.")
            if content.get("type") == "output_text":
                chunks.append(content["text"])
    if not chunks:
        raise RuntimeError("The AI returned no plan.")
    return validate_plan(json.loads("".join(chunks)))


def make_server(token, demo=False, api_key="", model="gpt-4.1-mini", port=8765):
    busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass  # No prompts, API keys or pairing tokens in request logs.

        def reply(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404,
                       {"mode": "demo" if demo else "api", "protocol": 1})

        def do_POST(self):
            if self.path != "/plan":
                self.reply(404, {"error": "Unknown route."})
                return
            # Reject browser origins and unexpected Host headers (DNS rebinding).
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Origin") or self.headers.get("Host") != expected_host:
                self.reply(403, {"error": "Only the local mod can request a plan."})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(),
                                       ("Bearer " + token).encode()):
                self.reply(401, {"error": "Paste the helper token into the mod settings."})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.reply(415, {"error": "Expected JSON."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    self.reply(413, {"error": "Request is too large or empty."})
                    return
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete request body.")
                request = validate_request(json.loads(raw))
            except (ValueError, UnicodeError):
                self.reply(400, {"error": "Invalid request. Check prompt and coordinates."})
                return
            if not busy.acquire(blocking=False):
                self.reply(429, {"error": "Another request is running. Try again shortly."})
                return
            try:
                plan = demo_plan(request) if demo else ai_plan(request, api_key, model)
                self.reply(200, plan)
            except (ValueError, RuntimeError) as error:
                self.reply(422, {"error": str(error)})
            except Exception:
                self.reply(502, {"error": "The helper could not generate a valid plan."})
            finally:
                busy.release()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Free offline connection test; no AI calls")
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "")
    if not args.demo and not key:
        parser.error("Set OPENAI_API_KEY locally, or use --demo. Never send your API key in chat.")
    token = secrets.token_urlsafe(32)
    server = make_server(token, demo=args.demo, api_key=key,
                         model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    print("GD Editor AI — " + ("DEMO (no AI calls or charges)" if args.demo else "AI API mode (paid API usage)"))
    print("Paste this LOCAL HELPER TOKEN into Drew Editor AI's mod settings:")
    print(token)
    print("Keep this window open. Press Control+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
