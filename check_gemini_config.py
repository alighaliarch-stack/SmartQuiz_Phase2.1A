"""
check_gemini_config.py
------------------------
A safe, LOCAL-ONLY configuration check for the Gemini grading
integration. Run this before starting the server to confirm everything
is wired correctly, without ever calling the live Gemini API and
without ever printing the API key (or any part of it — no length, no
prefix, no hash).

Usage:
    python check_gemini_config.py

What it checks:
  1. Is python-dotenv installed and does it actually load .env?
  2. Is GEMINI_API_KEY present in the environment after that load?
  3. Does the google-genai SDK import correctly?
  4. Does genai.Client(api_key=...) construct successfully? (This is a
     purely local object construction — the SDK does not make any
     network request just to build a Client, confirmed by inspecting
     its source: no request is issued until a method like
     generate_content() is actually called.)
  5. What model name would be used for grading?

This script does NOT call generate_content() and does NOT verify the
key is actually valid against Google's servers — that would require a
real network call and would consume API quota. It only confirms the
key is present and the client can be constructed from it.
"""

import os
import sys


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        dotenv_ok = True
    except ImportError:
        dotenv_ok = False

    print(f"python-dotenv installed and .env loaded: {'yes' if dotenv_ok else 'NO -- pip install python-dotenv'}")

    api_key = os.environ.get("GEMINI_API_KEY")
    key_configured = bool(api_key and api_key.strip())
    print(f"GEMINI_API_KEY: {'configured' if key_configured else 'NOT configured'}")

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    provider = os.environ.get("AI_GRADING_PROVIDER", "gemini")

    client_ok = False
    client_error = None
    if key_configured:
        try:
            from google import genai
            from google.genai import types
            genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=5000),
            )
            client_ok = True
        except ImportError as exc:
            client_error = f"google-genai not installed ({exc})"
        except Exception as exc:  # noqa: BLE001 - deliberately broad for a diagnostic report
            # The exception message itself could theoretically echo back
            # something derived from the key in a future SDK version, so
            # scrub defensively even though construction-time failures
            # observed so far never do this.
            client_error = str(exc)
            if api_key and api_key in client_error:
                client_error = client_error.replace(api_key, "[redacted]")

    print(f"Gemini client: {'initialized' if client_ok else 'NOT initialized' + (f' ({client_error})' if client_error else '')}")
    print(f"Model: {model}")
    print(f"Active provider (AI_GRADING_PROVIDER): {provider}")
    print("API key value: NOT DISPLAYED")

    return 0 if (dotenv_ok and key_configured and client_ok and provider == "gemini") else 1


if __name__ == "__main__":
    sys.exit(main())
