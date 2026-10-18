# LogIQ

**Ask questions about system logs. Get evidence-backed answers for the people who need to act.**

LogIQ combines SereneDB search and analytics with an agent that selects tools, reads executed SQL evidence, and recommends next steps for engineers, team leads, and managers.

![LogIQ: ask questions about logs, retrieve verified evidence, get role-specific recommendations](docs/assets/logiq-banner.png)

![Python 3.12, SereneDB 26.09.2, 19 SQL tools, Web / MCP / CLI](docs/assets/logiq-badges.png)

[Quick start](#quick-start) · [Worked example](#example-investigation) · [Reproduce results](#reproduce-our-results) · [Tool reference](FUNCTIONS.md)

> **Submission status:** documented against `feature_docker`. Corpus totals and investigation checks were executed against an existing populated database. A fresh Docker installation and new model answers were not executed for this documentation update. The restored offline suite runs but fails against this implementation; see [validation status](#validation-status).

## What LogIQ Does

One log corpus, three decisions. SereneDB searches and calculates; the model interprets bounded results rather than reading millions of raw lines.

| Role | Goal | Question | Answer |
|---|---|---|---|
| **Engineer** | **Diagnose** | What failed, and what should I check? | Error lines, surrounding context, labeled hypotheses, diagnostic checks |
| **Team Lead** | **Coordinate** | What recurs, and who owns it? | Counts, trends, component ownership, investigation priorities |
| **Manager** | **Prioritize** | What deserves attention first? | Ranked risks, proposed owners, concrete actions, explicit unknowns |

The checked-in [investigation evidence](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/evidence.jsonl) records **4,747,963 BGL lines** and **207,820 OpenStack lines**. These totals were rechecked against the existing database; they are log lines, not incidents.

## How It Works

![LogIQ architecture: user question, agent, tool selection, executed SQL, SereneDB, evidence, and role-specific answer, with evidence returning to the agent](docs/assets/logiq-architecture.png)

[Edit architecture in draw.io](docs/assets/logiq-architecture.drawio)

The agent chooses from predefined functions in [tools.py](tools.py); it does not generate arbitrary SQL. Tools return the executed SQL, parameters, scope, row counts, truncation flags, and caveats. The agent can gather more evidence before answering with `Q#` citations.

## Key Features

| Implemented capability | What a judge can inspect |
|---|---|
| Role-specific reasoning | Three prompts in [agent.py](agent.py), shared by web, CLI, and MCP |
| Full-text search | SereneDB inverted index, BM25 ranking, exact match counts, dataset/time/component/level filters |
| Event investigation | Neighboring lines, template history, preceding patterns, traceback context, entity tracing |
| Operational analytics | Recurrence, grouped counts, trends, period comparisons, estimated episodes, correlation chains |
| Prioritization | Pattern or team risk rankings using observed severity, frequency, spread, and trend |
| Inspectable evidence | Executed queries and parameters alongside bounded results and limitations |
| Three interfaces | Streaming FastAPI web app, MCP over HTTP or stdio, terminal agent |
| Multiple providers | Anthropic, NVIDIA, or an OpenAI-compatible endpoint |

See [FUNCTIONS.md](FUNCTIONS.md) for the 19-tool catalog. The callable signatures in [tools.py](tools.py) are authoritative when older examples differ.

## Quick Start

**Source:** a separate working-tree snapshot of `feature_docker`, without its Git history. Run setup commands from this directory. A public clone URL is not available yet.

**Prerequisites:** Git, Docker with the Compose plugin supporting `env_file.required` (Compose v2.24+), an internet connection for images/packages/datasets, and free ports **7890**, **8000**, **8001**. Docker supplies Python 3.12. A configured tool-calling model is needed for generated answers; the tool explorer works without a model key.

These commands follow the current Compose file. Fresh loading was **not run** during this update because it rebuilds database tables. Use the [existing database path](#existing-populated-database) to preserve loaded data.

```bash
# 1. Clone the implementation documented here.

# 2. Fresh checkout only: copy the template, then edit .env.
cp .env.example .env
```

Uncomment **one provider block** in `.env`. For example, set `LOGIQ_PROVIDER=nvidia` and supply your own `NVIDIA_API_KEY`. Anthropic uses `ANTHROPIC_API_KEY` **or** `ANTHROPIC_AUTH_TOKEN`; a local OpenAI-compatible server uses `LOGIQ_BASE_URL` and `LOGIQ_MODEL`. See [configuration](#technical-details). Keep `.env` private.

```bash
# 3. Validate configuration and start the database.
docker compose config --quiet
docker compose up -d serenedb

# 4. Repeat this check until it prints 1, then continue.
docker compose exec -T serenedb psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -c 'SELECT 1'

# 5. Fresh database only: install dependencies, prepare data, and load it.
docker compose run --rm --no-deps init

# 6. After init exits successfully, start both interfaces.
docker compose up -d --build --no-deps app mcp

# 7. Check application, database access, and provider configuration.
curl --fail --silent --show-error http://localhost:8000/api/meta
```

Open **[http://localhost:8000](http://localhost:8000)**. The metadata response should contain `db_error: null`, the two datasets, 19 tools, and `agent_ready: true` for a configured provider. `agent_ready` checks configuration, not whether a provider will accept a request. There is no `/health` route in this branch; `/api/meta` is the database-aware check.

**Why staged startup?** The short `depends_on` declarations in [docker-compose.yml](docker-compose.yml) wait for container startup, not database readiness or successful completion of `init`. `docker compose up --build` is the existing all-in-one entry point, but can start the UI before ingestion finishes. The sequence above waits explicitly.

| Interface | Address / entry point | How to use it |
|---|---|---|
| Web | `http://localhost:8000` | Choose a role in **Ask the agent**; inspect the **Evidence trail** |
| Tool explorer | Same web app, **Explore tools** | Run database tools directly without an LLM key |
| Corpus | Same web app, **Corpus & teams** | Inspect dataset and ownership totals |
| MCP HTTP | `http://localhost:8001/mcp` | Connect an MCP client; its own model reasons over LogIQ tools |
| CLI | `python agent.py engineer 'Your question'` | Use the manual environment or `docker compose exec -T app python agent.py ...` |

Stop with `docker compose down`; the named database volume and host `data/` remain. Starting the app with `--no-deps` avoids rerunning ingestion. `docker compose down -v` deletes the Compose database volume; it is a deliberate reset, never a resume command.

## Data Pipeline

![Implemented LogIQ data pipeline from Zenodo archives through parsing, CSV loading, unified tables, ownership views and full-text index to agent access](docs/assets/logiq-data-pipeline.png)

[Edit data pipeline in draw.io](docs/assets/logiq-data-pipeline.drawio) · [Edit banner](docs/assets/logiq-banner.drawio)

| Stage | Actual implementation | Output |
|---|---|---|
| Source | Loghub BGL and OpenStack archives from Zenodo, URLs defined in [prepare_data.py](prepare_data.py) | `data/BGL.zip`, `data/OpenStack.tar.gz` |
| Preparation | Extract raw logs; parse fields; merge three OpenStack logs by timestamp; mask variable tokens into templates | `data/BGL.log_structured.csv`, `data/OpenStack.log_structured.csv` |
| Ingestion | [load.py](load.py) streams local CSVs with `COPY FROM STDIN`; URLs/server paths use `read_csv` | Raw SQL tables `bgl`, `openstack` |
| Normalization | [sql/schema.sql](sql/schema.sql) unifies timestamps, sources, severity, alert labels, templates, and content | `events` |
| Ownership | Sample `ownership` rows; longest component-prefix match | `component_team`, then `events_v` with team and `is_problem` |
| Search | Text normalization and stemming, inverted index | `log_text` dictionary, `events_idx` index |
| Agent access | [tools.py](tools.py) executes parameterized search and analytics | Evidence envelopes for all three interfaces |

These are SQL tables/views/indexes, not separate vector collections. There is no embedding or vector-database stage.

**Source selection:** `BGL_SOURCE` / `OPENSTACK_SOURCE` overrides → local structured CSVs → remote Loghub 2k sample CSVs. Full-data reproduction requires both prepared CSVs. Sample data has different template IDs and cannot reproduce this investigation.

## Example Investigation

**OpenStack · InvalidDiskInfo / qemu-img · May 16–17, 2017**

Scope: `2017-05-16 00:00:00` inclusive through `2017-05-18 00:00:00` exclusive; timestamps as recorded. Focus: `oslo_service.periodic_task`.

| Role | Question, condensed from the recorded run | Recommended action |
|---|---|---|
| Engineer | Why does disk inspection fail, and what do nearby lines establish? | Check the referenced VM disk path and relevant lifecycle/storage records; determine why the file was unavailable |
| Team Lead | How often does it recur, which components are involved, and who owns the work? | Assign an explicit owner to the unassigned component; coordinate compute/virtualization and storage investigation |
| Manager | What should be prioritized, including rarer higher-severity risks? | Assign follow-up owners; review the CRITICAL token-validation pattern as well as disk-inspection errors |

| Actual recorded evidence | Scope and interpretation |
|---|---|
| **8 InvalidDiskInfo matches** | Engineer `Q1`; template `E64`, two log-file sources; first `2017-05-16 03:19:45`, last `2017-05-17 05:24:15` |
| **24 qemu-img matches** | Baseline `B2`; component `oslo_service.periodic_task`, level `ERROR`, same time window |
| **192 component ERROR lines** | Baseline `B3`; 24 templates, each occurring 8 times; current supported level-grouped check reproduces the 192 count |
| **144 lines on May 16; 48 on May 17** | Recorded Team Lead `Q5`; current component problem-line trend returns the same daily totals |
| **Owner: unassigned** | Sample ownership map has no matching prefix for `oslo_service.periodic_task` |
| **2 CRITICAL token-validation lines** | Template `E72`; recorded Manager `Q2` ranks it at severity 3 under the generic label `FATAL/SEVERE`. The actual OpenStack level is `CRITICAL` |

A representative exception at **line 45631** says `InvalidDiskInfo: Disk info file is invalid: qemu-img failed to execute ...`. Nearby stderr reports **“No such file or directory.”** This is evidence of the failed disk-path access; it does not establish why the path was unavailable.

**Do not add these counts together:** search matches, exception lines, and component ERROR lines overlap. Two source files do not prove two hosts, and eight exception lines do not establish eight independent incidents. Root cause and customer impact remain unknown. Suggested owners are recommendations, not recorded ownership assignments.

[Read the full illustrated investigation](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/submission_report_illustrated.md) · [Inspect the original JSONL evidence](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/evidence.jsonl)

> The saved run and current branch differ. In particular, this branch's `count_problems` has no `level` argument, and `problem_trend` has no `severity` argument. Historical results remain preserved; the compatible rerun below uses explicit supported calls and checks the ERROR scope separately. Some legacy draw.io edit links inside the historical report point to absent sources; the new README diagrams above include their editable files.

## Reproduce Our Results

### Validation status

| Check performed for this update | Result |
|---|---|
| Source branch | Separate snapshot of `feature_docker`; new publication Git history begins independently |
| Compose configuration | `docker compose config --quiet` passed |
| HTTP routes and MCP discovery | Local Uvicorn returned HTTP 200 for `/` and `/api/meta`; `db_error: null`; MCP discovery returned 19 tools and 3 prompts. Tested on port 18000, outside Compose |
| Read-only checks on an existing database | Corpus totals, 8 / 24 search counts, component counts, and supported investigation calls checked; see rerun below |
| Historical calls replayed unchanged | Partial: unsupported filters and changed payloads prevent an exact replay of the full saved run |
| Offline tests | Both modules are included unchanged. The source snapshot's restored suite ran 131 tests and failed; see the Tests section |
| Fresh clone, image build, download, ingestion | Not executed during this update; preparation and loading would modify data/database |
| New provider-backed answers | Not executed; provider configuration alone does not verify model access or answer quality |

### Fresh installation

Full source URLs, download/prepare/load commands, record counts, and the data-exclusion policy are in [Datasets and ingestion](README.md#datasets-and-ingestion).

Use a **new checkout and empty database volume** for this path. Run commands from the repository root. Shell examples use Bash/zsh; PowerShell users can use Docker commands directly and save the Python snippets below to temporary `.py` files instead of using heredocs.

| Step | Command | Expected result / verification |
|---|---|---|
| 1. Get source | Use this publication directory and configure `.env` using [Quick Start](#quick-start) | Application files and the configuration template are present; source snapshot is `feature_docker` |
| 2. Validate environment | `docker compose version` then `docker compose config --quiet` | Compose installed; config validation exits 0 |
| 3. Start storage | `docker compose up -d serenedb` | `docker compose ps serenedb` shows it running |
| 4. Wait for SQL | `docker compose exec -T serenedb psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -c 'SELECT 1'` | Prints `1`; repeat until ready before loading |
| 5. Prepare and ingest | `docker compose run --rm --no-deps init` | Exit 0; logs show both datasets, then loader counts. This runs pip install, `prepare_data.py`, and `load.py` |
| 6. Start application | `docker compose up -d --build --no-deps app mcp` | `docker compose ps` shows app and MCP running |
| 7. Verify services | `curl --fail --silent --show-error http://localhost:8000/api/meta` | Inspect JSON: `db_error` is null; full counts below; provider configured if `agent_ready` is true |
| 8. Run tests | See [tests](#tests) | Current checkout cannot pass the supplied suite; implementation/test contract differences remain a reproduction blocker |
| 9. Execute investigation | Run the evidence and optional model snippets below | Saved SQL evidence plus summary; optional new answers use the exact recorded role questions |
| 10. Inspect artifacts | `ls -lh reproduction/` | Contains the outputs listed in [reports and evidence](#reports-and-evidence) |

**Loading is destructive to LogIQ's schema.** [load.py](load.py) executes DROP statements before rebuilding tables/views/indexes. The init service always calls preparation and loading: a persisted volume does not make it a skip-if-populated initializer. It also reparses raw logs even when downloads/extraction can be reused. Do not run it against data you intend to preserve.

Check the full corpus after startup:

```bash
docker compose exec -T app python - <<'PY'
import tools
actual = {d['dataset']: d['lines'] for d in tools.corpus_overview()['data']['datasets']}
assert actual == {'bgl': 4747963, 'openstack': 207820}, actual
print(actual)
PY
```

Expected: the assertion passes and prints both counts. A count of 2,000 indicates sample data, not the full corpus. This assertion was executed locally against the existing populated database; its Docker wrapper was not run.

### Existing populated database

**Already loaded into this Compose project's volume:** skip init and preserve the database.

```bash
docker compose up -d serenedb
# Wait for the SELECT 1 check above, then:
docker compose up -d --build --no-deps app mcp
curl --fail --silent --show-error http://localhost:8000/api/meta
```

Expected: the existing corpus is visible without rebuilding tables. Verify full counts with the assertion above. Resume after a stop using the same sequence; do not use a plain `docker compose up` as a preservation strategy because it can start `init` again.

**Database running outside this Compose project:** use the [manual environment](#manual-environment) and set `.env`'s `SERENEDB_DSN` to that database. Skip both `prepare_data.py` and `load.py`, then run the same count assertion with `python` in place of `docker compose exec -T app python`. Start `python -m uvicorn app:app --host 127.0.0.1 --port 8000` and check `/api/meta`.

Compose explicitly overrides `SERENEDB_DSN` for every Python service. Editing the host `.env` DSN does **not** redirect the Compose app to an external database. The database checked for this update was an existing local instance on port 7894, reached through the private local `.env`; that is not the default fresh-install port.

### Tests

`tests/test_new_tools.py` and its recovered dependency `tests/test_severity_filters.py` are included unchanged. They use SQLite fixtures and scripted providers rather than querying SereneDB. Imports/shared fixtures work, but the regression expectations include behavior not implemented by this source snapshot.

```bash
# Inspect whether tests are actually included in your checkout.
git ls-files 'tests/*'

# Local/manual environment, if tests/ exists:
python -m unittest discover -s tests -v

# Equivalent inside a running Compose app, if tests/ exists:
docker compose exec -T app python -m unittest discover -s tests -v
```

**Source snapshot result:** 131 tests ran; 44 methods passed and 87 had at least one failure/error. The runner reported `FAILED (failures=36, errors=122)`, including multiple subtest outcomes. Unsupported filter arguments, missing evidence fields, truncation/validation behavior, and historical prompt contracts remain unresolved. This publication preserves the application and tests; it does not weaken assertions to claim a pass. A zero-test run is not a pass.

### Execute the worked investigation: current tools

This read-only rerun uses **supported current signatures**. It writes new evidence beside a summary without overwriting the submitted report. No model key is required. It checks full-data identity and separates ERROR counts from the broader problem-line trend.

For Docker, run the block as shown. For the manual environment, change its first line to `python - <<'PY'`.

```bash
docker compose exec -T app python - <<'PY'
import json
from pathlib import Path
import tools

out = Path('reproduction')
out.mkdir(exist_ok=True)
scope = dict(dataset='openstack', start='2017-05-16 00:00:00', end='2017-05-18 00:00:00')
component = dict(scope, component='oslo_service.periodic_task')
plan = [
    ('corpus', 'corpus_overview', {}),
    ('exception', 'search_logs', dict(scope, text='InvalidDiskInfo', limit=20)),
    ('qemu', 'search_logs', dict(component, text='qemu-img', level='ERROR', sort='time', limit=30)),
    ('event', 'investigate_event', dict(dataset='openstack', line_id=45631, window=10)),
    ('levels', 'count_problems', dict(component, group_by='level', problems_only=False)),
    ('templates', 'count_problems', dict(component, group_by='template', limit=25)),
    ('daily', 'problem_trend', dict(component, bucket='day')),
    ('risks', 'risk_ranking', dict(scope, by='pattern', top=5)),
]
results = {}
with (out / 'current-tools.jsonl').open('w') as f:
    for key, name, args in plan:
        envelope = tools.call_tool(name, args)
        results[key] = envelope['data']
        f.write(json.dumps(dict(check=key, name=name, input=args, envelope=envelope)) + '\n')

counts = {d['dataset']: d['lines'] for d in results['corpus']['datasets']}
assert counts == {'bgl': 4747963, 'openstack': 207820}, counts
assert results['exception']['total_matches'] == 8
assert results['qemu']['total_matches'] == 24
levels = {g['level']: g['lines'] for g in results['levels']['groups']}
assert levels == {'ERROR': 192}, levels
assert results['templates']['total_lines'] == 192
assert results['templates']['total_groups'] == 24
assert all(g['lines'] == 8 for g in results['templates']['groups'])
daily = {r['period'][:10]: r['lines'] for r in results['daily']['series']}
assert daily == {'2017-05-16': 144, '2017-05-17': 48}, daily
summary = ('# Current-tool investigation checks\n\n'
           'PASS: full corpus; 8 InvalidDiskInfo matches; 24 qemu-img matches; '
           '192 component ERROR lines; 24 templates x 8 lines; daily totals 144 / 48.\n\n'
           'These overlapping log-line counts are not independent incident counts. '
           'The daily call covers problem lines; the separate level check establishes '
           'that this component contains only ERROR lines in this window. '
           'Read current-tools.jsonl for executed SQL, scope, context and risks.\n')
(out / 'checks.md').write_text(summary)
print(summary)
PY
```

**Expected:** all assertions pass; `reproduction/current-tools.jsonl` contains eight evidence records, and `reproduction/checks.md` reports the checked counts. A failed assertion must be investigated, not replaced with the expected number. This exact Python block passed against the existing database during the documentation update, with only its output directory redirected to `/tmp`. The Docker wrapper was not executed. This check establishes selected results, not byte-for-byte equality with the historical run.

### Generate new role-specific answers (optional model run)

The original run metadata records NVIDIA `nvidia/nemotron-3-super-120b-a12b`. To use the same provider/model, set `LOGIQ_PROVIDER=nvidia`, `LOGIQ_MODEL=nvidia/nemotron-3-super-120b-a12b`, and your own `NVIDIA_API_KEY`, then recreate the app service to apply `.env` changes:

```bash
docker compose up -d --no-deps --force-recreate app
```

This step makes real provider requests and may incur provider charges. New answers/tool choices can differ with the model and current tool signatures. A request for a historical unsupported filter may fail; inspect the saved tool errors.

```bash
docker compose exec -T app python - <<'PY'
import json
from pathlib import Path
import agent

out = Path('reproduction')
out.mkdir(exist_ok=True)
records = [json.loads(line) for line in Path('docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/evidence.jsonl').read_text().splitlines()]
metadata = next(r['event'] for r in records if r['event']['type'] == 'metadata')
ready, reason = agent.ready()
if not ready:
    raise SystemExit(reason)
for role, question in metadata['questions'].items():
    answers, errors = [], []
    with (out / f'{role}-events.jsonl').open('w') as f:
        for event in agent.run(agent.Session(role), question):
            f.write(json.dumps(event, default=str) + '\n')
            f.flush()
            if event['type'] == 'answer':
                answers.append(event['text'])
            if event['type'] == 'error' or 'error' in event:
                errors.append(event)
    if not answers or errors:
        raise SystemExit(f'{role}: missing answer or errors; inspect reproduction/{role}-events.jsonl')
    (out / f'{role}.md').write_text(f'# {role}\n\n{question}\n\n' + '\n\n'.join(answers))
    print(f'{role}: saved answer and events')
PY
```

**Expected:** one nonempty answer Markdown file and one event JSONL file per role; each event stream includes successful tool results and an answer. Verify each factual claim against its `Q#` evidence and check for truncation, scope differences, errors, and unsupported causal claims. This provider-backed rerun was **not executed** during the documentation update.

### Reports and evidence

| Location | Contents |
|---|---|
| [docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/submission_report_illustrated.md](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/submission_report_illustrated.md) | Existing illustrated submission report, exact questions, recorded SQL and results |
| [docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/evidence.jsonl](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/evidence.jsonl) | Existing run metadata, baseline calls, role questions, events, answers, and evidence envelopes |
| `reproduction/current-tools.jsonl` | Generated by the current-tool block: eight calls and full evidence envelopes |
| `reproduction/checks.md` | Generated assertion summary |
| `reproduction/{engineer,team_lead,manager}.md` | Generated by the optional model block: new answers |
| `reproduction/{engineer,team_lead,manager}-events.jsonl` | Generated model events, tool evidence, errors, and usage |

Compose bind-mounts the repository at `/app`, so outputs from `exec` appear in the host's `reproduction/` directory. These generated files do not exist in a fresh clone. The web UI keeps sessions in memory and does not automatically save reports. The CLI prints tool names, arguments, query row counts, and answers; use the Python event capture above to retain full SQL envelopes.

There is no `worked_investigation.py` or `WORKED_INVESTIGATION.md` in this branch. Use the checked-in report and the supported snippets above instead of the older README's generator command.

## Project Structure

```text
.
├── README.md
├── Dockerfile / docker-compose.yml     # Python 3.12 app + DB/init/app/MCP services
├── .env.example / requirements.txt     # Configuration template and Python dependencies
├── app.py / static/index.html          # FastAPI API and web UI; no frontend build
├── agent.py                            # Provider loops, sessions, three role prompts
├── tools.py / mcp_server.py             # 19 SQL tools and MCP transports/prompts
├── prepare_data.py / load.py            # Archive preparation and database loading
├── sql/schema.sql                      # Tables, ownership, views, full-text index
├── agent_tools.py                      # Separate raw-table inspection CLI
├── FUNCTIONS.md                        # Tool reference
├── docs/assets/                        # README draw.io sources and PNG exports
└── docs/worked_investigations/         # One submitted report, its evidence, and diagram assets
```

`data/`, `.venv/`, and generated `reproduction/` outputs are local artifacts. The two offline test sources are included; their source-snapshot failures are disclosed above.

## Technical Details

<details>
<summary><strong>Configuration — providers, database, and every environment setting</strong></summary>

Copy [.env.example](.env.example) only when creating a new environment; preserve an existing `.env` and its credentials.

| Variable | Purpose |
|---|---|
| `SERENEDB_DSN` | Required database connection. Manual default: `postgresql://postgres:change-me@localhost:7890/postgres`. Compose overrides it with its internal service DSN |
| `LOGIQ_PROVIDER` | `anthropic` (default), `nvidia`, or `openai` |
| `ANTHROPIC_API_KEY` **or** `ANTHROPIC_AUTH_TOKEN` | Exactly one credential for Anthropic; token adds the OAuth beta header |
| `ANTHROPIC_BASE_URL` | Optional Anthropic gateway/proxy |
| `LOGIQ_EFFORT` | Anthropic effort: `low`, `medium` (default), `high`, `xhigh`, `max` |
| `NVIDIA_API_KEY` | NVIDIA catalog credential; client code also accepts `LOGIQ_API_KEY` as fallback |
| `LOGIQ_BASE_URL` | OpenAI-compatible endpoint; can also override NVIDIA's endpoint |
| `LOGIQ_API_KEY` | Compatible endpoint credential, if required; local keyless endpoints are supported |
| `LOGIQ_MODEL` | Model override; defaults in code: `claude-opus-5-5` for Anthropic, `nvidia/nemotron-3-super-120b-a12b` for NVIDIA. Set explicitly for provider `openai` |
| `BGL_SOURCE`, `OPENSTACK_SOURCE` | Loader source overrides: structured local CSV, URL, or server-visible path. Compose init sets `/data/...` server paths |
| `LOGIQ_MCP_TRANSPORT` | `stdio` by default; Compose sets `streamable-http` |
| `LOGIQ_MCP_HOST`, `LOGIQ_MCP_PORT` | HTTP MCP bind address and port; defaults `0.0.0.0`, `8001` |

The Compose database's `POSTGRES_PASSWORD` and internal DSN are explicitly set in [docker-compose.yml](docker-compose.yml); the `.env.example` host DSN uses a different example password. For a host process accessing a Compose-created database, use the password defined by Compose and `localhost:7890`. Do not assume the unchanged template DSN matches it. Changing a password environment variable does not reset credentials in an existing volume.

Manual DSNs accept URL or libpq key/value syntax (`host=localhost port=7890 dbname=postgres user=postgres password=change-me`). Percent-encode special characters in URL passwords, e.g. `@` → `%40`.

A model key is unnecessary for raw tools and MCP; MCP uses the client's model. `agent_ready` means configuration is present, not that authentication/model availability was tested. Model defaults above describe this code, not a guarantee of current provider availability. NVIDIA/compatible models must support tool calling.

The previous README's provider catalog command remains available for checking model IDs: `curl -s https://integrate.api.nvidia.com/v1/models`. It was not executed during this update; choosing an available model does not establish tool-calling compatibility.

For a host model server from Docker, `localhost` means the app container; on Docker Desktop use an accessible host address such as `http://host.docker.internal:11434/v1`. Native Linux may require separate host networking configuration. The Compose initializer's key warning checks API-key variables only, so it may warn for a valid Anthropic auth token or a keyless local endpoint.

After changing `.env`, recreate affected services (`docker compose up -d --no-deps --force-recreate app mcp`) to refresh their container environment.

</details>

<a id="manual-environment"></a>

<details>
<summary><strong>Manual environment — install, load, run, and inspect without Compose</strong></summary>

Use **Python 3.12+** for the full preparation path (including `tarfile` extraction filters); Docker uses 3.12. Python 3.13 was used for the existing-database checks in this update.

```bash
python3 -m venv .venv
source .venv/bin/activate
# Windows PowerShell activation: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Fresh environment only; then edit database/provider settings:
cp .env.example .env
```

[requirements.txt](requirements.txt) lists `psycopg[binary]`, `anthropic`, `python-dotenv`, `fastapi`, `uvicorn`, `mcp`, and `openai`. Versions are not pinned; dependency installation on a fresh machine was not verified in this update.

For a **new manual database**, the original container command remains:

```bash
docker run -d --name serenedb -p 7890:7890 \
  -e POSTGRES_PASSWORD=change-me \
  -v serenedb-data:/var/lib/serenedb \
  serenedb/serenedb:26.09.2
```

Wait until SQL is ready. The template's DSN matches this manual example. Verify the network connection from the host:

```bash
python - <<'PY'
import os
from dotenv import load_dotenv
import psycopg
load_dotenv('.env')
with psycopg.connect(os.environ['SERENEDB_DSN']) as conn:
    print(conn.execute('SELECT version()').fetchone())
PY
```

Expected: a SereneDB/PostgreSQL version string. SereneDB speaks the PostgreSQL protocol. Network connections need configured credentials; a GUI PostgreSQL client can use the same host, port, database, and user.

Only on a **fresh/disposable LogIQ schema**, prepare and load:

```bash
python prepare_data.py
python load.py
```

Expected: full CSVs in `data/` followed by 4,747,963 BGL and 207,820 OpenStack rows. `load.py` drops and rebuilds schema objects. For an existing populated database, skip both commands and use the corpus assertion in the reproduction section with local `python`.

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
# Separate terminal, same active venv:
python agent.py engineer 'Investigate InvalidDiskInfo in OpenStack on May 16–17, 2017.'
python agent.py team_lead 'Which problems recur most in BGL and who owns them?'
python agent.py manager 'What should we prioritize across BGL and OpenStack?'
python agent_tools.py levels bgl
```

Open `http://127.0.0.1:8000`; use `/api/meta` to check database access. The three agent commands need the configured provider. `agent_tools.py` is a raw-table CLI, separate from the agent tool layer. Manual container lifecycle: `docker stop serenedb`, `docker start serenedb`, `docker ps`.

</details>

<details>
<summary><strong>Dataset assumptions — sources, parsing, sample mode, and limitations</strong></summary>

| Full corpus, recorded and rechecked | BGL | OpenStack |
|---|---|---|
| Lines | 4,747,963 | 207,820 |
| Problem lines | 1,012,140 | 3,314 |
| Date span | 2005-06-03 → 2006-01-04 | 2017-05-14 → 2017-05-17 |
| Derived templates | 695 | 73 |

Downloads used by `prepare_data.py` (the script creates `data/`; manual downloads need `mkdir -p data` first):

```bash
mkdir -p data
curl -L -o data/BGL.zip 'https://zenodo.org/records/8196385/files/BGL.zip?download=1'
curl -L -o data/OpenStack.tar.gz 'https://zenodo.org/records/8196385/files/OpenStack.tar.gz?download=1'
```

Keep `?download=1` and follow redirects. Preparation reuses archives larger than 1 MB and already-extracted files, but writes structured CSVs again. Archive sizes and runtime depend on the source/network; this README makes no fresh-run timing claim.

- BGL parses `Label Timestamp Date Node Time NodeRepeat Type Component Level Content`; OpenStack parses `Logrecord Date Time Pid Level Component [ADDR] Content`. The parser repairs some malformed fields/control bytes and counts skipped unparseable lines in its output; inspect those counts.
- Templates mask UUIDs, paths, and tokens containing digits, preserving field names where possible. Numeric-only content becomes `<numeric-only message>`. `E1`, `E2`, … follow first appearance within each dataset. This is not Loghub's Drain parser and IDs differ from the 2k samples.
- OpenStack files are merged by timestamp. Adjacent line IDs can belong to different files; use `get_nearby_events(minutes=..., same_source=True)` for source-specific context. A source is a log file, not a verified host identity.
- OpenStack's per-instance anomaly labels are not loaded as per-line truth. To explore a labeled instance, search for its UUID, e.g. `544fd51c-4edc-4780-baae-ba1d80a0acfc`.
- Ownership is a **fictional sample organization**, mapped by longest component prefix. `oslo_service.periodic_task` and `keystonemiddleware.auth_token` are unassigned in the submitted corpus. Proposed compute/virtualization or identity ownership requires a human assignment.
- Severity is 3 for FATAL/SEVERE/FAILURE/CRITICAL, 2 for ERROR, 1 for WARNING/WARN, otherwise 0. A problem line is severity ≥ 1 or a BGL expert alert.
- Timestamps remain as recorded, with no timezone conversion. Dataset years differ; don't combine them into one time trend.

For a disposable **small-sample demo**, explicitly select the sample URLs instead of moving or deleting full CSVs:

```bash
BGL_SOURCE=https://raw.githubusercontent.com/logpai/loghub/master/BGL/BGL_2k.log_structured.csv \
OPENSTACK_SOURCE=https://raw.githubusercontent.com/logpai/loghub/master/OpenStack/OpenStack_2k.log_structured.csv \
python load.py
```

This still rebuilds the schema and cannot reproduce the full-data example. Custom CSVs can use `BGL_SOURCE=path/or/url.csv OPENSTACK_SOURCE=path/or/url.csv python load.py`; columns must match the supplied schema.

Local CSVs are streamed with `COPY FROM STDIN`, so the database need not see the host path. For custom server-side SQL such as `read_csv('/data/...')`, mount files into the database container. Compose already mounts `./data:/data` into SereneDB; its init service uses these server paths after preparation creates files on the shared host directory.

**Data citation:** Loghub, Zhu et al., *Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics*, ISSRE 2023. BGL: Oliner & Stearley, *What Supercomputers Say: A Study of Five System Logs*, DSN 2007.

</details>

<details>
<summary><strong>SQL evidence — provenance, exact counts, and interpretation</strong></summary>

A current tool returns this envelope (field names, not fabricated results):

```text
{ tool, scope, filters,
  queries: [{ purpose, sql, params, rows_returned, truncated }],
  data, notes }
```

The agent adds `evidence_id` (`Q1`, `Q2`, …). For MCP, the client numbers calls itself. Inspect scope and filters before comparing claims. Search runs a full count separately from a limited sample; bounded queries fetch an extra row to detect truncation. Returned log content may be cut at 300 characters.

This SQL is recorded in Engineer Q1 of the [full report](docs/worked_investigations/openstack_invaliddiskinfo_20261018T184344_296010Z/submission_report_illustrated.md):

```sql
SELECT dataset, count(*) AS matches, min(ts) AS first_ts, max(ts) AS last_ts
FROM events_idx
WHERE content @@ websearch_to_tsquery(%s)
  AND TRUE AND dataset = %s AND ts >= %s AND ts < %s
GROUP BY dataset
```

Parameters: `["InvalidDiskInfo", "openstack", "2017-05-16 00:00:00", "2017-05-18 00:00:00"]`. The recorded result is **8** matches. `%s` placeholders are bound by psycopg; this query is not a standalone psql command with literal placeholders.

- Search, count, and risk scopes differ; preserve the denominator when quoting percentages.
- `count_problems(group_by='level', problems_only=False, ...)` exposes actual levels without using unsupported `level=` filtering. `problem_trend` on this branch does not accept `severity=`. Do not silently drop a filter from historical evidence and claim an exact replay.
- `investigate_event` includes whole-dataset recurrence and separate context windows, even when the initial search used a narrower time range.
- Correlation/lift and `candidate_cause` are leads, not proof of causation. Strict chain confirmation means statistical repetition on multiple days, not a confirmed causal chain.
- `count_incidents` estimates episodes by source/time bucket; these are not verified independent incidents. Trend thresholds and priority scores are heuristics.
- Structured extraction fields (latency, HTTP status, request/tenant/instance IDs) are OpenStack-shaped. Missing coverage on other formats is not evidence of healthy behavior.
- No downtime, cost, customer impact, SLA breach, or confirmed remediation benefit can be inferred without additional evidence.

A secondary BGL example from the previous README searched `"Link has been severed"` after `2005-09-01` and reported 41,837 matching lines. To investigate with current tools, use the web explorer or `python agent.py engineer 'Investigate the "Link has been severed" errors in BGL after 2005-09-01; show SQL evidence.'`. That older model trace is not the reproducibility target here; check its claims through fresh tool evidence.

</details>

<details>
<summary><strong>Troubleshooting — startup, credentials, ports, and reproduction blockers</strong></summary>

| Symptom | Check / next step |
|---|---|
| UI opens but database is unavailable | Read `db_error` from `/api/meta`; confirm SQL readiness and successful init. HTTP 200 alone is not database health |
| Fresh init fails to connect | Wait for the database `SELECT 1` check, then rerun init only on a fresh/disposable schema |
| Only 2,000 rows per dataset | Sample fallback was loaded; prepare full CSVs and rebuild only in a disposable schema |
| Agent offline | Set the selected provider block; recreate app after `.env` changes; check `agent_setup` |
| Agent ready but request fails | Inspect provider authentication, model name/tool support, endpoint reachability, and streamed error events |
| `password authentication failed` | Match the existing database's actual password; Compose and manual example credentials differ |
| `fe_sendauth: no password supplied` | Add credentials to the host DSN |
| Port already allocated | Inspect existing listeners; stop the conflicting service intentionally or use a separate manual database port and matching host DSN |
| `No files found that match the pattern` | A server path is not visible to SereneDB. Use a host CSV through `load.py` COPY or an appropriate database mount |
| `unexpected keyword argument 'level'` / `'severity'` | Historical calls differ from this branch; use the current-tool investigation block |
| Offline test failures | Both modules are present; production behavior and regression contracts differ. Inspect failures rather than weakening assertions |
| Missing old report generator | Use the supported event-capture blocks above; the previous generator is absent |

```bash
docker compose ps -a
docker compose logs --tail 100 serenedb app mcp
# If init was started via compose up (run --rm prints to its invoking terminal):
docker compose logs -f init
```

Inspect port listeners before stopping anything: macOS `lsof -iTCP:8001 -sTCP:LISTEN`, Linux `ss -ltnp`, PowerShell `Get-NetTCPConnection -LocalPort 8001 -State Listen`. Repeat for 7890/8000 as needed.

For a manual `serenedb` container with no network password, an administrator can set one from inside the container:

```bash
docker exec -it serenedb psql -h 127.0.0.1 -p 7890 -U postgres -c "ALTER ROLE postgres PASSWORD 'change-me'"
```

That changes credentials; update the private DSN accordingly. It was not executed for this documentation update.

</details>

<details>
<summary><strong>Architecture decisions — interfaces, model boundary, security, and diagram editing</strong></summary>

**One SQL tool layer.** Database aggregation keeps raw logs out of the model context and records each query. There are no embeddings or frontend build steps. The static UI uses FastAPI streaming events; sessions are held in process memory and disappear on restart. The app has no login; use a trusted local environment. Roles change prompts, not database permissions; row-level security is not implemented.

**MCP uses the client's model.** The server exports the same 19 tools and three prompts (`engineer`, `team_lead`, `manager`). Connect to `http://localhost:8001/mcp` for the Compose HTTP service. A client supporting remote MCP can use that URL without a local Python environment. For a remote host, replace `localhost` with an address reachable from the client, subject to that host's networking/access controls.

For stdio, use absolute paths to your local venv Python and `mcp_server.py`. The server loads `.env` relative to the repository. A generic client configuration is:

```json
{
  "mcpServers": {
    "logiq": {
      "command": "/absolute/path/logiq-ai-agent-hackathon/.venv/bin/python",
      "args": ["/absolute/path/logiq-ai-agent-hackathon/mcp_server.py"]
    }
  }
}
```

Client-specific CLI/config syntax can change; these inherited integration examples were not re-tested during this update:

| Client | HTTP service | Local stdio |
|---|---|---|
| Claude Code | `claude mcp add --transport http logiq http://localhost:8001/mcp` | `claude mcp add logiq -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"` |
| OpenCode | `opencode mcp add logiq --global --url http://localhost:8001/mcp` | `opencode mcp add logiq --global -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"` |
| Codex CLI | `codex mcp add logiq --url http://localhost:8001/mcp` | `codex mcp add logiq -- /absolute/path/logiq-ai-agent-hackathon/.venv/bin/python /absolute/path/logiq-ai-agent-hackathon/mcp_server.py` |
| Cursor / Claude Desktop | Remote URL if supported by the installed client | Generic `mcpServers` configuration above |
| VS Code Copilot | `{"servers":{"logiq":{"type":"http","url":"http://localhost:8001/mcp"}}}` | Use `servers.logiq` with `type: "stdio"`, command and args from the generic block |
| Gemini CLI | Use the client's supported transport | Generic `mcpServers` entry in its settings |

Claude Code may register globally with `--scope user`; use `claude mcp list` or `/mcp` to verify. OpenCode exposes `opencode mcp list`. On Windows use absolute Python paths and escape backslashes in JSON, or use forward slashes. Codex's stdio configuration can alternatively use `[mcp_servers.logiq]` with `command` and `args` in its TOML configuration.

Role prompt example in Claude Code:

```text
/mcp__logiq__engineer "Investigate InvalidDiskInfo in OpenStack on May 16–17, 2017."
```

The corresponding Team Lead and Manager prompts use `/mcp__logiq__team_lead` and `/mcp__logiq__manager`. Clients without prompts can ask: “Using logiq, as an engineer, …; show tool calls and SQL evidence.” Always state dataset and time range. A headless Claude example from the prior README is `claude -p '/mcp__logiq__manager "What should we prioritize?"' --allowedTools "mcp__logiq__*"`; this integration was not rerun here.

Optional MCP inspection (requires Node.js and package download; not rerun here):

```bash
npx -y @modelcontextprotocol/inspector --cli "$PWD/.venv/bin/python" "$PWD/mcp_server.py" --method tools/list
```

Expect 19 tools. Use `--method prompts/list` for the three prompts; omit `--cli` for the inspector UI.

**Optional OpenTelemetry exploration is separate.** On-demand indexing and loading the OpenTelemetry Parquet data into this pipeline are not implemented. The prior README included this SQL exploration:

```sql
SELECT * FROM read_parquet('https://public-pme.s3.eu-west-3.amazonaws.com/text_bench/part_000.parquet') LIMIT 10;
```

It reported one billion rows in one file and a 132-second full aggregation, compared with much faster schema/count/sample reads. Those are historical observations, not measurements verified by this update. A proposed time-window index or summary table is future work, not an existing stage.

**Editable diagrams.** New diagrams use the existing investigation's white background, pastel blue/purple/teal/green cards, Helvetica text, and explicit evidence loop. Sources and exports are in `docs/assets/`. Open `.drawio` files with draw.io; the PNGs also embed their source diagrams. Export with the installed desktop CLI:

```bash
# macOS example; substitute your platform's draw.io executable if needed.
/Applications/draw.io.app/Contents/MacOS/draw.io --export --format png --scale 2 \
  --embed-diagram --theme light --output docs/assets/logiq-banner.png docs/assets/logiq-banner.drawio
/Applications/draw.io.app/Contents/MacOS/draw.io --export --format png --scale 2 \
  --embed-diagram --theme light --output docs/assets/logiq-architecture.png docs/assets/logiq-architecture.drawio
/Applications/draw.io.app/Contents/MacOS/draw.io --export --format png --scale 2 \
  --embed-diagram --theme light --output docs/assets/logiq-data-pipeline.png docs/assets/logiq-data-pipeline.drawio
/Applications/draw.io.app/Contents/MacOS/draw.io --export --format png --scale 2 --border 4 \
  --embed-diagram --theme light --output docs/assets/logiq-badges.png docs/assets/logiq-badges.drawio
```

The publication `.gitignore` excludes private credentials, downloaded/generated data, archives, caches, virtual environments, and temporary outputs. Approved diagram sources/PNGs and test sources belong in the publication manifest. No existing Git history, duplicate investigation reports, or publication audit is included.

</details>
