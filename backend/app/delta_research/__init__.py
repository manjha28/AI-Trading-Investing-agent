"""Delta Exchange perpetual-futures research area (public market data only).

Integration design (Milestone 1)
- Boundary: the host FastAPI process is the only API the browser talks to. The
  research package (manjha28/del_trading_bot, ``research_platform``) is reached
  only through adapters in this package. Nothing here imports it yet, because it
  needs pandas/numpy and the host does not declare them; that is a Milestone 2
  decision.
- Storage: the research SQLite store stays isolated (option A). It is never the
  host ledger and is never merged into PostgreSQL, so there is no migration. It
  will be opened read-only (``ResearchDataStore(path, read_only=True)``) by a
  single managed reader; the collector remains a separate process and sole writer.
- Product contract: research only. No orders, no execution, no account data and no
  Delta credentials. Delta perpetuals never enter the equity portfolio valuation.
- Disabled by default: ``DELTA_RESEARCH_ENABLED=false``. Filesystem paths are never
  returned by the API.
"""
