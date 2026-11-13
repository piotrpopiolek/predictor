"""Service tests against the shared dev Postgres.

Each test opens its own engine and deletes the rows it inserted. The ids are
fixed, so these tests stay in one process. Parallel workers would mix rows.
Use ``pytest -m unit`` for the set that does not touch the database.
"""
