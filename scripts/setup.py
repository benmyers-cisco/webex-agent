#!/usr/bin/env python3
"""Interactive setup wizard for the Webex Agent.

Walks a new user through:
1. Creating .env from .env.example
2. Running OAuth flow
3. Setting up preferences.md from template
4. Verifying the connection works
5. Optionally configuring cron jobs
"""

import os
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")
ENV_EXAMPLE = os.path.join(PROJECT_DIR, ".env.example")
PREFS_FILE = os.path.join(PROJECT_DIR, "preferences.md")
PREFS_EXAMPLE = os.path.join(PROJECT_DIR, "preferences.example.md")
VENV_PYTHON = os.path.join(PROJECT_DIR, ".venv", "bin", "python3.12")


def step(n, title):
    print(f"\n{'='*60}")
    print(f"  Step {n}: {title}")
    print(f"{'='*60}\n")


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def confirm(prompt):
    return input(f"  {prompt} (y/n): ").strip().lower() in ("y", "yes")


def main():
    print("\n  Webex Agent — Setup Wizard")
    print("  " + "-" * 40)
    print("  This will configure the agent for first-time use.\n")

    # Step 1: Environment file
    step(1, "Environment Configuration")

    if os.path.exists(ENV_FILE):
        print(f"  .env already exists at {ENV_FILE}")
        if not confirm("Overwrite it?"):
            print("  Keeping existing .env")
        else:
            os.remove(ENV_FILE)

    if not os.path.exists(ENV_FILE):
        print("  Creating .env from template...\n")

        email = ask("Your Webex email", "you@example.com")
        auth_method = ask("Auth method — oauth or token", "oauth")

        lines = []
        if auth_method == "oauth":
            client_id = ask("Webex OAuth Client ID")
            client_secret = ask("Webex OAuth Client Secret")
            lines.append(f"WEBEX_CLIENT_ID={client_id}")
            lines.append(f"WEBEX_CLIENT_SECRET={client_secret}")
        else:
            token = ask("Webex Personal Access Token (expires in 12h)")
            lines.append(f"WEBEX_ACCESS_TOKEN={token}")

        lines.append(f"\nSUMMARY_USER_EMAIL={email}")

        delivery = ask("Briefing delivery — webex, email, or both", "webex")
        lines.append(f"SUMMARY_DELIVERY={delivery}")
        if delivery in ("webex", "both"):
            space = ask("Webex space name for briefings", "My Webex Summaries")
            lines.append(f'SUMMARY_WEBEX_SPACE="{space}"')
        if delivery in ("email", "both"):
            email_to = ask("Email address for briefings", email)
            lines.append(f"SUMMARY_EMAIL_TO={email_to}")

        print()
        claude_method = ask("Claude API — bedrock or direct", "direct")
        if claude_method == "bedrock":
            profile = ask("AWS profile name", "default")
            lines.append(f"\nCLAUDE_CODE_USE_BEDROCK=true")
            lines.append(f"AWS_PROFILE={profile}")
            lines.append("AWS_SDK_LOAD_CONFIG=1")
        else:
            api_key = ask("Anthropic API key (or leave blank if set globally)", "")
            if api_key:
                lines.append(f"\nANTHROPIC_API_KEY={api_key}")

        with open(ENV_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n  Created {ENV_FILE}")

    # Step 2: OAuth flow
    step(2, "Webex Authentication")

    token_file = os.path.join(PROJECT_DIR, ".webex_token.json")
    if os.path.exists(token_file):
        print("  Token file already exists (.webex_token.json)")
        if not confirm("Re-authenticate?"):
            print("  Skipping OAuth flow.")
        else:
            _run_oauth()
    else:
        # Check if using OAuth
        with open(ENV_FILE) as f:
            env_content = f.read()
        if "WEBEX_CLIENT_ID" in env_content and "your_client_id" not in env_content:
            print("  Running OAuth flow — this will open your browser...")
            if confirm("Ready?"):
                _run_oauth()
        else:
            print("  Using personal access token — no OAuth needed.")

    # Step 3: Preferences
    step(3, "Triage Preferences")

    if os.path.exists(PREFS_FILE):
        print(f"  preferences.md already exists.")
        print("  You can edit it anytime or train via the update_preferences MCP tool.")
    else:
        print("  Creating preferences.md from template...")
        shutil.copy(PREFS_EXAMPLE, PREFS_FILE)
        print(f"  Created {PREFS_FILE}")
        print("\n  Edit this file to configure:")
        print("    - Your role and focus areas")
        print("    - Spaces to always scan (by priority)")
        print("    - Spaces to never scan")
        print("    - Space-specific rules")
        print("\n  You can also train preferences conversationally later.")

    # Step 4: Verify connection
    step(4, "Verify Connection")

    if confirm("Test the Webex connection now?"):
        _verify_connection()

    # Step 5: Cron setup (optional)
    step(5, "Scheduled Briefings (Optional)")

    print("  You can set up cron jobs for automated daily triage and weekly retros.")
    print("  Example cron entries:\n")
    print(f"    # Daily triage at 8:30am and 4pm (adjust timezone offset)")
    print(f"    30 12 * * 1-5 cd {PROJECT_DIR} && set -a && source .env && set +a && .venv/bin/python3.12 scripts/daily_summary.py")
    print(f"    0 20 * * 1-5 cd {PROJECT_DIR} && set -a && source .env && set +a && .venv/bin/python3.12 scripts/daily_summary.py")
    print(f"\n    # Weekly retro Friday 1pm")
    print(f"    0 17 * * 5 cd {PROJECT_DIR} && set -a && source .env && set +a && .venv/bin/python3.12 scripts/weekly_retro.py")
    print("\n  Add these to your crontab with: crontab -e")

    # Done
    print(f"\n{'='*60}")
    print("  Setup complete!")
    print(f"{'='*60}")
    print("\n  Next steps:")
    print("    1. Edit preferences.md with your spaces and role")
    print("    2. Open Claude Code and ask: 'What needs my attention in Webex?'")
    print("    3. After a few triages, teach it: 'Add X to Always Scan P1'")
    print()


def _run_oauth():
    """Run the OAuth flow."""
    cmd = [VENV_PYTHON, os.path.join(PROJECT_DIR, "servers", "oauth.py")]
    env = os.environ.copy()
    # Load .env for the subprocess
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"')
    try:
        subprocess.run(cmd, env=env, check=True)
        print("\n  OAuth complete — tokens saved.")
    except subprocess.CalledProcessError:
        print("\n  OAuth failed. Check your client ID/secret and try again.")
    except FileNotFoundError:
        print(f"\n  Python not found at {VENV_PYTHON}")
        print("  Run: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt")


def _verify_connection():
    """Quick test that the Webex API is reachable."""
    cmd = [VENV_PYTHON, "-c", """
import sys, os
sys.path.insert(0, os.environ.get('PROJECT_DIR', '.'))
sys.path.insert(0, os.path.join(os.environ.get('PROJECT_DIR', '.'), 'servers'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ.get('PROJECT_DIR', '.'), '.env'))
from oauth import get_valid_token
from webex_client import WebexClient
client_id = os.environ.get('WEBEX_CLIENT_ID', '')
client_secret = os.environ.get('WEBEX_CLIENT_SECRET', '')
token = ''
if client_id and client_secret:
    token = get_valid_token(client_id, client_secret)
if not token:
    token = os.environ.get('WEBEX_ACCESS_TOKEN', '')
if not token:
    print('No token available')
    sys.exit(1)
client = WebexClient(token)
spaces = client.list_spaces(max_results=5)
print(f'Connected! Found {len(spaces)} spaces (showing first 5).')
for s in spaces[:5]:
    print(f'  - {s["title"][:50]}')
"""]
    env = os.environ.copy()
    env["PROJECT_DIR"] = PROJECT_DIR
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"')
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError:
        print("  Connection test failed. Check your credentials.")


if __name__ == "__main__":
    main()
