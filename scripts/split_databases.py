"""CLI entrypoint for split_databases."""

from kryten_webqueue.scripts.split_databases import (
    main,
    split_database,
    DOMAIN_TABLE_MAP,
    DOMAIN_MIGRATIONS,
)

__all__ = ["main", "split_database", "DOMAIN_TABLE_MAP", "DOMAIN_MIGRATIONS"]

if __name__ == "__main__":
    main()
