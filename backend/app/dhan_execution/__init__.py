"""Dhan live order execution: the ONE owner-approved exception to the read-only rule (see AGENTS.md).

NSE equity, INTRADAY, quantity 1, two allowlisted strategies, off by default in three independent layers. Nothing in this package runs
under pytest or CI against a real broker: ``client.DhanOrderClient`` refuses to leave the process there.
"""
