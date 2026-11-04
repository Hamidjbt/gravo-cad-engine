# cad-fond-de-plan — urban morphology and metric CAD engine

Version 1.4.2. This repository implements deterministic footprint/block topology, a level-aware road graph, R2018 DXF output, and A3/A1/A0 paper-space layouts. Project coordinates remain projected metres; the Oran/western-Algeria profile uses UTM 30N / EPSG:32630 where appropriate.

See [`DEVELOPMENT_ARCHITECTURE_MAP.md`](DEVELOPMENT_ARCHITECTURE_MAP.md) for project history, layer schema, storage/quota guidance, and portability details. See [`SKILL.md`](SKILL.md) for operating rules and the source-data limitations.

## Deterministic developer commands

- `make test` — creates a temporary isolated environment, installs pinned project/test dependencies, runs the full test suite, smoke-imports the MCP server, then removes the environment.
- `make lint` — runs Ruff checks/format verification and Mypy in a temporary environment.
- `make build` — creates a wheel and source distribution in `dist/`.
- `make run-mcp` — launches the stdio MCP server with `uv`.

The full plan builder is network-enabled and writes to its requested output directory. Unit tests and lint do not run it. Do not call `generate_cad_plan` unless a plan generation is explicitly intended.

## MCP server

The official Python MCP SDK's FastMCP server exposes `generate_cad_plan`, `verify_topology`, and `list_supported_formats`. `verify_topology` is read-only and restricts inspected DXFs to `CAD_FOND_DE_PLAN_ALLOWED_ROOT`. Generation creates a unique non-overwriting run directory beneath `CAD_FOND_DE_PLAN_OUTPUT_ROOT`.

For Claude Desktop, copy the `mcpServers.cad-fond-de-plan` entry from [`claude_desktop_config.json`](claude_desktop_config.json) and replace `/ABSOLUTE/PATH/TO/gravo-cad-engine` with the local repository path. The MCP process uses the standard stdio transport.

## Source/data hygiene

Raw GIS extracts, vector tiles, rasters, temporary environments, build output, and test caches are excluded by `.gitignore`. Keep only download recipes and lightweight provenance in source control. OSM and Microsoft data retain their original licenses; imagery-derived geometry is not a cadastral survey.
