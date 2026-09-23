"""Module runner for database split ETL."""

import sys
from pathlib import Path

# Add scripts directory to path if running directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from split_databases import main, split_database, DOMAIN_TABLE_MAP, DOMAIN_MIGRATIONS

__all__ = ["main", "split_database", "DOMAIN_TABLE_MAP", "DOMAIN_MIGRATIONS"]

if __name__ == "__main__":
    main()
