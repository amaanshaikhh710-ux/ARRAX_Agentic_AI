"""Safe local development entry point for ``python -m arrax``."""

from __future__ import annotations

import os

from .api import create_app
from .provider import GeminiProvider, ProviderConfig, ProviderConfigError


def main() -> None:
    """Start the dashboard on localhost without debug or reloader mode."""

    host = os.getenv("ARRAX_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("ARRAX_PORT", "5000"))
    except ValueError as exc:
        raise SystemExit("ARRAX_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("ARRAX_PORT must be between 1 and 65535")

    try:
        provider_config = ProviderConfig.from_env()
    except ProviderConfigError as exc:
        raise SystemExit(str(exc)) from exc
    provider = GeminiProvider(provider_config)
    app = create_app(
        db_path=os.getenv("ARRAX_DB_PATH", "arrax.sqlite"),
        workspace_root=os.getenv("ARRAX_WORKSPACE_ROOT"),
        provider=provider,
    )
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
