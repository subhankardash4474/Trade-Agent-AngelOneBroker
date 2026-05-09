# docs/

Project documentation. See:

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — system architecture, module boundaries, data flow.
- `journal/` — engineering journals (chronological, dated). Captures decisions, experiments, and post-incident reasoning.
- `audits/` — focused audit reports on specific concerns (hot-paths, risk-control completeness, etc.).
- `postmortems/` — incident write-ups when behaviour deviated from intent and required a code fix.

Operational logs (live agent, daemon, daily reports) live in `logs/` at the project root.
