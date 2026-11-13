# CovenantIQ

AI contract intelligence and vendor risk analysis platform. The full design is in [PROJECT_SPEC.md](PROJECT_SPEC.md).

## Quick start (development)

```bash
uv sync                      # install dependencies
cp .env.example .env         # then edit the values
uv run covenantiq --help
make check                   # lint, types, import boundaries, unit tests
```

Docker-based startup arrives in Milestone 2.

Architecture decisions live in [docs/adr/](docs/adr/).
