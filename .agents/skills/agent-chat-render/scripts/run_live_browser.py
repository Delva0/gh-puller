"""Run the existing real browser acceptance with explicitly supplied credential sources."""

# This standalone CLI is executed by path rather than imported as a package.
# ruff: noqa: INP001

import argparse
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from check_deployment import check_deployment, deployment_url
from dotenv import dotenv_values

CREDENTIAL_ENV = {"CHAT_ACCESS_PASSWORD", "OPENAI_API_KEY", "OPENAI_BASE_URL", "GH_TOKEN", "GITCODE_TOKEN",
                  "BRAVE_SEARCH_API_KEY", "CHAT_TEST_MODEL"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, type=deployment_url)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        health = check_deployment(args.url, args.revision)
        env = os.environ.copy()
        for source in args.env_file:
            if not source.is_file():
                raise ValueError(f"Credential source does not exist: {source}")
            env.update({key: value for key, value in dotenv_values(source).items()
                        if key in CREDENTIAL_ENV and value is not None})
        missing = [key for key in ("CHAT_ACCESS_PASSWORD", "OPENAI_API_KEY") if not env.get(key)]
        if missing:
            raise ValueError("Missing required variables: " + ", ".join(missing))
        app = args.repo.resolve() / "apps/agent-chat"
        output = args.output or app / "verification/live-browser" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        env.update(CHAT_TEST_URL=args.url, CHAT_TEST_OUTPUT=str(output.resolve()))
        print(f"Verified deployment {health['revision']}; starting real browser acceptance", flush=True)
        return subprocess.run(["node", "scripts/live-acceptance.mjs"], cwd=app / "web", env=env, check=False).returncode
    except (OSError, ValueError) as error:
        print(f"Acceptance not started: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
