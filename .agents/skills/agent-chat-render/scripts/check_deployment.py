"""Check a public Agent Chat deployment and its Git revision without credentials."""

# This standalone CLI is executed by path rather than imported as a package.
# ruff: noqa: INP001

import argparse
import json
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.request import urlopen


def deployment_url(value):
    """Reject credentials, query parameters and non-HTTPS deployment URLs."""
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise argparse.ArgumentTypeError("Use an HTTPS deployment URL without credentials, query or fragment")
    return value.rstrip("/")


def check_deployment(url, revision=None, timeout=50):
    """Return health evidence and reject a mismatched expected full Git SHA.

    Args:
        url: Public deployment base URL, validated without credentials.
        revision: Full expected Git SHA; omit for a read-only health observation.
        timeout: Maximum seconds to wait for the health response.
    """
    url = deployment_url(url)
    started = time.monotonic()
    with urlopen(f"{url}/api/health", timeout=timeout) as response:  # noqa: S310 - HTTPS is validated above.
        data = json.load(response)
        if not isinstance(data, dict) or response.status != 200 or data.get("status") != "ok":
            raise ValueError("Deployment health did not return HTTP 200 with status ok")
    actual = data.get("revision")
    if revision and actual != revision:
        raise ValueError(f"Revision mismatch: expected {revision}, got {actual}")
    return {"url": url, "checked_at": datetime.now(UTC).isoformat(), "http_status": 200,
            "revision": actual, "seconds": round(time.monotonic() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, type=deployment_url)
    parser.add_argument("--revision")
    parser.add_argument("--timeout", type=float, default=50)
    args = parser.parse_args()
    try:
        print(json.dumps(check_deployment(args.url, args.revision, args.timeout)))
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
