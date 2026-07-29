"""One-time SharePoint sign-in for the fetchurls job.

The webqueue service only acquires Microsoft Graph tokens *silently* from a
pre-seeded MSAL cache (it never prompts). Run this once on the server to create
that cache via the device-code flow:

    python -m kryten_webqueue.jobs.fetchurls_auth [--config /path/to/config.json]

It prints a URL + code; sign in with the curiousmotors account and grant the
Files.ReadWrite.All consent. The refresh token is cached (~90 days) at
``fetchurls.token_cache_path`` so subsequent unattended job runs authenticate
silently. Re-run this when the cache expires.
"""

import argparse
import os
import sys

from ..config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time SharePoint sign-in for fetchurls"
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("WQ_CONFIG", "/etc/kryten-webqueue/config.json"),
        help="Path to the webqueue config.json (default: $WQ_CONFIG or /etc/kryten-webqueue/config.json)",
    )
    args = parser.parse_args(argv)

    config = Config.from_file(args.config)
    fu = config.fetchurls
    if not (fu.sharepoint_tenant_id and fu.sharepoint_client_id):
        print(
            "ERROR: fetchurls.sharepoint_tenant_id and sharepoint_client_id must be "
            "set in the config before authenticating.",
            file=sys.stderr,
        )
        return 2
    if not fu.token_cache_path:
        print(
            "ERROR: fetchurls.token_cache_path must be set so the token can be cached "
            "for the service to reuse.",
            file=sys.stderr,
        )
        return 2

    from ..integrations.cmsutils.fetchurls import acquire_graph_token

    token = acquire_graph_token(
        fu.sharepoint_tenant_id, fu.sharepoint_client_id, fu.token_cache_path
    )
    if token:
        print(f"\n✓ Authenticated. Token cached at: {fu.token_cache_path}")
        print("  The fetchurls job can now run unattended until the cache expires.")
        return 0
    print("ERROR: Authentication did not complete.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
