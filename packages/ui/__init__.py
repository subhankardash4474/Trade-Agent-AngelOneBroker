"""POD 3: ui -- read-only dashboard.

Imports allowed:  core
Imports FORBIDDEN: trader, research, strategies, training

Entry point (target):  python -m ui

Currently a SKELETON. The existing monitoring/dashboard.py + streamlit_app.py
will move here in Phase 1.5. The pod reads from the shared DB (RDS in cloud,
SQLite on laptop) and renders live state without ever placing or modifying
trades. Read-only by design.
"""
