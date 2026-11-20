"""Execution groundwork for Delta Exchange perpetuals: intents, guard-rails, simulated fills, audit.

POLICY (AGENTS.md "Execution policy"): nothing in this package transmits an order to a broker and
no execution credential is stored. A live adapter is a separate, owner-approved milestone. Everything
here runs in research or monitor mode; an approved guard-rail decision never implies an order.
"""
