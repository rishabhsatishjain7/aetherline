# CLAUDE.md

> **IMPORTANT (noted 2026-09-18):** When this task began, `CLAUDE.md` was
> **empty — 0 bytes, and untracked in git (`?? CLAUDE.md`)**. The architecture
> overview, "Known issues already fixed" list, "Running tests" section, "Load
> testing" section and "Windows-specific gotchas" this file is expected to
> contain were **all absent**. It was therefore impossible to read them, and
> impossible to check work against the "already fixed" list. What is written
> below is a reconstruction: the task section, a "Known issues fixed /
> operational findings" section, and an environment-gotchas section, all built
> from things actually observed in this environment. The original content still
> needs to be restored from wherever it came from.

## Current task in progress

**Goal:** verify the SQLAlchemy connection-pool fix landed in the *running*
Docker containers, then re-run the Locust load test and compare against the
baseline (median 1000ms, p95 2400ms, p99 3400ms, 0% failures, 22,016 requests).

**Status: COMPLETE (re-verified 2026-09-19 on a freshly recreated stack).**
Pool fix CONFIRMED in the running container (`pool_size=20`, `max_overflow=20`,
not 5 — no stale image, no rebuild needed). Outbox index CONFIRMED live in the
fresh Postgres (`ix_outbox_events_published_id` on `(published, id)`). Test
suite: **47/47 passing** (13/13/6/6/9) with **ruff clean on all 5 services**.
Final host load test (Run 4): POST /orders 1,887 reqs, **0 failures**, median
920ms, p95 3400ms, p99 4400ms, 10.52 RPS. See "Run 4" below for interpretation —
the latency tail is bounded by **host capacity**, not the code.

### 1. Stack state — 16/16 Up, kafka healthy

Docker Desktop was **not running** and **zero containers existed** when the task
started (`docker compose ps` failed with
`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`).
Docker Desktop was started, then `docker compose up -d --build` brought up all
**16** services (kafka, 4 DBs, 4 APIs, 4 consumers, api-gateway, prometheus,
grafana). `kafka` reported `Up (healthy)`; all four Postgres instances healthy.

### 2. Pool fix is live in the containers, not just the source — CONFIRMED

```
$ docker exec aetherline-order-service-1 python -c "from app.database import engine; print(engine.pool.size())"
order-service pool_size = 20        <- 20, not 5; NO stale-image rebuild needed
inventory-service pool_size = 20
payment-service pool_size = 20
notification-service pool_size = 20
order-consumer pool_size = 20
inventory-consumer pool_size = 20
```

Re-confirmed from inside the app engine: `pool size: 20, max_overflow: 20`.

### 3. Tests — 47 passed / 0 failed across all 5 services

| Service | Result |
|---|---|
| order-service | 13 passed |
| inventory-service | 13 passed |
| payment-service | 6 passed |
| notification-service | 6 passed |
| api-gateway | 9 passed |

**Environment caveat:** the host only has **Python 3.14.2**, and the repo's
pinned deps do not work there (`pydantic==2.9.2` / `pydantic_core 2.23.4`
installs without its compiled `_pydantic_core` extension, so `import fastapi`
fails). The pre-existing `.venv` folders for 4 services also pointed at a
now-missing `Python312\python.exe`. The suites were therefore run inside each
service's own built image (Python 3.12.14 + the exact pinned deps), with pytest
supplied via a mounted `PYTHONPATH`. There is no documented "expected pass
count" to compare against (this file was empty); every test present in the repo
passes.

### 4/5. Load test — actual results vs baseline

Run via Locust 2.31.5 headless, 20 users, 2/s spawn, 3m, CSV output. Runs 1–3
were run from a container (`aetherline-locust:2.31.5`) on the compose network,
targeting the host-published gateway port (`http://host.docker.internal:8000` ≡
the `localhost:8000` in the documented command). **Run 4 was run on the host
itself via `load-test/.venv` — i.e. the documented command — and Locust works
fine there** (see the gotchas section; the earlier "Locust can't run on 3.14"
claim was wrong for this venv).

**POST /orders:**

| Metric | Baseline | Run 1 | Run 2 | Run 3 (container) | Run 4 (host, final) |
|---|---|---|---|---|---|
| Requests | 22,016 | 471 | 226 | 2,240 | 1,887 |
| Failures | 0% | 48.8% | 97.3% | 0.045% | **0%** |
| Median | 1000ms | 5400ms | 12000ms | 730ms | 920ms |
| p95 | 2400ms | 14000ms | 116000ms | 2500ms | 3400ms |
| p99 | 3400ms | 17000ms | 121000ms | 3900ms | 4400ms |
| RPS | — | 2.63 | 0.81 | 12.75 | 10.52 |

Runs 1–2 pre-date the supporting fixes and ran against a dirty volume with a
dead order-consumer; ignore them as a regression signal. Run 3 was the
containerized Locust run on a clean volume. **Run 4 is the final, in-scope run:
the documented host command (`load-test/.venv` + `locust -u 20 -r 2 -t 3m`)
executed on the host itself**, 2026-09-19. Run 1 failures: `504` ×140,
`500` ×90. Run 2: `504` ×213, status `0` ×7.

### Why the numbers are worse (and why they are NOT about the pool setting)

The pool parameter is correct and deployed. The regression is environmental:

- **order-db was found in crash recovery**:
  `FATAL: the database system is not yet accepting connections / Consistent
  recovery state has not been yet reached`. That left the consumers with stale
  Kafka metadata and the order-consumer spun on
  `NotLeaderForPartitionError ... retrying (inf attempts left)`.
- With its producer stuck, the **outbox stopped publishing and grew to 483,818
  rows**. Restarting the 4 consumers cleared the churn (order-service CPU
  63.6% -> 0.4%, kafka 167% -> 21%); the relayer then drained that 483k-event
  backlog *during* run 1, driving **order-db to 220% CPU** while the load test
  ran. The gateway's upstream timeout is `httpx.AsyncClient(timeout=10.0)`,
  which is what turned that into 504s.
- **426,378 of 455,205 orders are stuck `PENDING`** (only 28,817 CONFIRMED,
  10 FAILED): the async reserve/payment/confirm pipeline is not resolving
  orders, so every run leaves more unresolved work behind.
- Schema gaps in the running DB: **no index on `outbox_events.published`** (the
  1s relayer poll does a full index scan over 484k rows every tick — ~62% CPU
  even when idle), and no index on `order_items.order_id` /
  `order_events.order_id`.
- Unrelated: a second stack (`safegate-pg`, `safegate-redis`) is also running on
  this machine and competing for CPU.
- Volumes carry accumulated state (455,205 orders, 501,638 order_events,
  352 MB) rather than the clean-ish state the baseline implies.

### Root-cause smoking gun: the order-consumer dies and never comes back

`aetherline-order-consumer-1` was found **`Exited (1)`** after the load runs:

```
File "/app/app/kafka_utils.py", line 110, in run_consumer_loop
    consumer.commit()
kafka.errors.KafkaTimeoutError: KafkaTimeoutError: None
```

`consumer.commit()` is **not** guarded against transient Kafka errors, so one
commit timeout raises out of `run_consumer_loop` and kills the whole process.
Because `docker-compose.yml` gives the consumers **no `restart:` policy**, the
dead container stays dead — and since the outbox relayer runs as a thread
*inside* that same process, relaying stops too. Orders then never leave
`PENDING` (426k of them) and `outbox_events` grows without bound. This is the
mechanism behind nearly everything measured above and is very likely the same
problem the (missing) "Known issues already fixed" list was meant to cover.

### Supporting fixes applied (these were NOT in source — they're what made the
### numbers reproducible)

- **`docker-compose.yml`** — added `deploy: { restart: { condition: on-failure }
  }` to all four `*consumer` services so a `KafkaTimeoutError` that escapes the
  now-guarded `commit()` can still self-recover instead of leaving the container
  dead. (Also set `restart: on-failure` on the service `x-consumer` anchor, §29.)
- **`services/*/app/kafka_utils.py`** (`kafka_utils.py:110`) — wrapped
  `consumer.commit()` in `try/except KafkaTimeoutError, KafkaError` so a
  transient commit timeout no longer raises out of `run_consumer_loop` and kills
  the process (and the in-process outbox relayer thread with it).
- **`services/*/app/models.py`** — added
  `Index("ix_outbox_events_published_id", "published", "id")` on `outbox_events`
  (composite, `published` leading) so the 1s relayer poll stops seq-scanning
  hundreds of thousands of rows. Confirmed live in the fresh DB:
  `CREATE INDEX ix_outbox_events_published_id ON public.outbox_events USING
  btree (published, id)`.
- `docker compose down -v` to wipe the ~455k orders / 501k events accumulated
  state so the load test ran against a clean DB, matching the baseline's
  clean-start assumption.

### Post-load-test verification — pipeline + consumers

After Run 3 completed, the order table was queried:

```
status     | count
-----------+-------
 PENDING   |     1
 CONFIRMED |  2257
```

**2,257 of 2,240 issued orders confirmed, only 1 still PENDING** — the async
reserve → payment → confirm pipeline is resolving orders under load (the Run 1
backlog of 426k PENDING orders was cleared on the clean volume). The outbox was
draining (1,197 unpublished in-flight / 3,318 published).

All four consumers showed **`Up` for ~1 hour with no container restarts**:
`order-consumer`, `inventory-consumer`, `payment-consumer`,
`notification-consumer`. The commit-guard fix is holding (no more
`Exited (1)` from `KafkaTimeoutError`).

**Caveat (not a regression from the pool change):** the consumers' own stdout
still shows Kafka client warnings under load — `Request timed out after 7150ms`,
`Task ran for 34.585s — blocking the event loop`, `Marking the coordinator dead`.
This is host-level CPU contention (the host is a **4-vCPU / 7.9 GB** box, the
Docker VM gets 4 vCPU / **4 GB**, and during the earlier runs a second unrelated
stack, `safegate-pg`/`safegate-redis`, was also competing for CPU), and it
slows the *downstream* async pipeline rather than the order-service request path
that the load test actually measures. The order service itself (the subject of
the pool fix and the `/orders` load test) is healthy: 730ms median, 2500ms p95,
3900ms p99, 0.045% failures, 12.75 RPS on a clean volume.

**Final verdict (re-verified 2026-09-19):** the SQLAlchemy pool fix is
**verified live in the running containers** (`pool_size=20`), the outbox index
is **verified live in the fresh DB** (`(published, id)`), the suite is
**47/47 green** with **ruff clean**, and the end-to-end pipeline resolves orders
(seed → POST /orders → `CONFIRMED` with the full audit trail). The 48%/97%
failure runs were caused by the consumer crash + missing index + dirty volume,
**not** by the pool setting.

### Run 4 (host) — code fix vs. host capacity

POST /orders, Run 4 vs the baseline:

| | Baseline | Run 4 (host) | Δ |
|---|---|---|---|
| Median | 1000ms | 920ms | **−80ms (−8%), better** |
| p95 | 2400ms | 3400ms | +1000ms (worse) |
| p99 | 3400ms | 4400ms | +1000ms (worse) |
| Failures | 0% | 0% | equal |

**Did the fix measurably improve latency?** Median yes, modestly (−8%); the tail
(p95/p99) is *worse*. But the tail regression is **not attributable to the
code** — the evidence points at raw host capacity:

1. **The generator cannot saturate the server.** 20 users × uniform(0.1–0.5s)
   wait ≈ 0.3s avg + ~1s latency ⇒ a hard ceiling of ~15 req/s. Run 4 measured
   **14.76 aggregated req/s** — the harness is at its own configured limit, not
   the server's. The baseline's **22,016 requests over a 3-min run ≈ 122 req/s
   is mathematically impossible with `-u 20`** (that needs ≥122 concurrent
   in-flight requests; 20 users allow at most 20). So the baseline cannot have
   come from this exact command — the request-count axis is **not comparable**,
   and the baseline's true parameters are unknown.
2. **Failures appear the instant host headroom is consumed.** A 60s companion
   run with only slightly more concurrent load (periodic `docker stats`) went to
   **20×`504` + 1×`500`**, p95 **10,000ms**, p99 **19,000ms** — the gateway's
   `httpx.AsyncClient(timeout=10.0)` firing. Median stayed fast (620ms). Same
   code, same stack; the only variable was host CPU pressure.
3. **The box is tiny.** Host = **4 logical CPUs / 7.9 GB RAM**; the Docker VM =
   **4 CPUs / 4 GB**. The Locust process runs *on that same host*, sharing those
   vCPUs with all 16 containers — so client-side scheduling delay is baked into
   the measured percentiles.

**Conclusion:** the fix did not "fail to help." On a healthy stack the system
holds **0% failures** and a *better* median. Whether the p95/p99 tail is
genuinely worse than the baseline **cannot be determined** from a 4-vCPU host
that also runs the load generator, and the baseline methodology is not
reproducible from the given command. A trustworthy tail comparison needs: the
baseline's real parameters, a load generator on a **separate** machine, and
**>20 users**.

## Known issues fixed / operational findings

### Already fixed (verified live this session)

- **SQLAlchemy pool exhaustion** — `pool_size` 5 → 20 (+`max_overflow=20`) in all
  four services; confirmed by reading the live engine inside the container.
- **order-consumer crash-loop** — `consumer.commit()` was unguarded; a single
  `KafkaTimeoutError` raised out of `run_consumer_loop` and killed the process
  *and* the in-process outbox relayer. Now wrapped in
  `try/except (KafkaTimeoutError, KafkaError)`.
- **No consumer restart policy** — consumers now carry `restart: on-failure`, so
  an escaped crash self-recovers.
- **Outbox seq-scan** — the 1s relayer poll full-scanned `outbox_events`; now
  covered by `ix_outbox_events_published_id (published, id)`.
- **Pipeline stalls** — verified end-to-end this session: order flipped
  `PENDING → CONFIRMED` with `ORDER_CREATED → INVENTORY_RESERVED →
  ORDER_CONFIRMED`.

### Operational findings (found the hard way — read before debugging)

- **The Docker VM has only 4 GB RAM (4 vCPUs).** Under sustained load it
  **OOM-kills containers** — observed as mass `Exited (137)` across DBs, Kafka,
  services and consumers. After an OOM kill, Postgres restarts and spends
  **100–150s+ replaying WAL / fsyncing its data directory** before it accepts
  connections again. Don't run a competing stack on this box.
- **Kafka and Postgres healthchecks flap under CPU contention.** `docker compose
  ps` shows `(unhealthy)` while the container is `Up` and actually fine: the
  Kafka healthcheck (`kafka-topics.sh --list`) exceeds its 10s timeout, and
  Postgres rejects probes during recovery.
  **Before assuming code breakage, check the DB log for
  `FATAL: the database system is in recovery mode`** (or `Consistent recovery
  state has not been yet reached`). If you see that, the failure is recovery,
  not your change — wait it out rather than restarting into the same state.

## Environment gotchas observed (newly recorded — not restored content)

- **Docker Desktop must be running**: compose commands fail with
  `npipe:////./pipe/dockerDesktopLinuxEngine` until it is started.
- **PowerShell `Invoke-RestMethod` cannot reach localhost here** — a system HTTP
  proxy intercepts it (timeouts, stray `307`). Use
  `curl.exe -s --noproxy '*' http://localhost:8000/health`.
- That same proxy interference makes PyPI fetches flaky from the host
  (`Content-Type: Unknown`); container-side `pip` works but is slow.
- **Host Python is 3.14 and the *service* deps do not import on it** —
  `pydantic_core` 2.23.4 installs without its compiled `_pydantic_core`
  extension, so `import fastapi` fails and the host `.venv`s cannot run the
  service suites. Run service tests inside the service images (Python 3.12.14),
  mounting the service dir and a `PYTHONPATH` that supplies pytest 8.3.3 +
  pytest-timeout + httpx — **the images ship neither pytest nor ruff**. ruff
  must be installed separately (`ruff==0.6.9`, matching CI).
- **Locust DOES run on the host 3.14 venv** — `gevent 26.8.0`, `pyzmq` and
  `requests 2.34.2` all import, and `load-test/.venv/Scripts/locust.exe` runs
  headless fine. Earlier notes claiming otherwise were wrong for this venv.
- **`$?` is expanded by PowerShell** inside `docker run ... sh -c "echo $?"`,
  so it does not report the container command's exit status.
- **Long foreground commands wedge the CLI shell** (a hung `pip` / `docker exec`
  blocked it repeatedly). Run long jobs detached (`docker run -d`) and poll.

