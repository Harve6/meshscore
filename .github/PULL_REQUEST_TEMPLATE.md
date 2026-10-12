## What and why

## Checklist

- [ ] `pytest -m "not network"` and `ruff check .` pass
- [ ] real-data suites run if a league changed (`pytest`)
- [ ] messages stay <= 130 bytes and don't add chatter (a new alert kind is in `min`/`max` on purpose)
- [ ] a restart still announces nothing that already happened
- [ ] docs / CHANGELOG updated for user-visible changes
