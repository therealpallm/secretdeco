#!/usr/bin/env python3
"""Enter a key on your own computer; keep it out of shell history and files."""
import getpass
import os
import server

if __name__ == "__main__":
    print("This mode uses paid OpenAI API usage. It does not implement ChatGPT sign-in.")
    print("Requests send your prompt, first-block position, and up to 64 selected object IDs/positions.")
    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key (input hidden): ").strip()
    if not os.environ["OPENAI_API_KEY"]:
        raise SystemExit("No key entered. Use Start-Demo.command for the free test.")
    server.main()
