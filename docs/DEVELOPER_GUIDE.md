# Developer Guide

## Blistrue blister-strip inspection station

Architecture, decision logic, data files, HTTP API, PLC signals and tests. For running the station see the [README](../README.md) and the [User Guide](USER_GUIDE.md).

---

## 1. Architecture

```
            ┌────────────── app.py (FastAPI + Uvicorn) ───────────────┐
 Camera ──► │ FrameSource: background reader, median of last 5 frames │
            │ HTTP API for the screens  ·  /stream (MJPEG)  ·  /board │
            └───────┬──────────────┬──────────────┬───────────────────┘
                    │              │              │
              d2i/station.py   d2i/batch.py   d2i/supervisor.py
              inspect, teach,  clearance,     Shift Supervisor agent
              self-test, lock  batch, release (tool calls, requests)
                    │              │              │
     ┌──────────────┼──────────┐   │              │
 pockets.py     judge.py   measure.py         llm.py
 SAM 3.1        DINOv2     size, shape,       Muse Spark Open (Baseten),
 layout         (frozen)   colour             Muse Spark 1.3 fallback
     │                                            │
 meta_api.py ── Meta Model API: SAM 3.1, Muse Spark 1.3, Muse Voice Transcribe (spend limit, retry, trace)

 Outputs: plc.py (Modbus TCP :5020) · audit.py (hash chain) · record.py (batch record) · linestate.py (/board)
```

- **One process.** `app.py` serves the screens (`static/`), reads the camera on a background thread and runs every station action under one station lock, so a verdict, a teach, a self-test or a signature never interleave.
- **Model calls run in parallel** where they are independent (the SAM 3.1 prompts of one layout, the painted strips of a self-test) through a bounded thread pool.
- **The decision is local.** The pass/fail decision uses the frozen DINOv2 judge and plain code; language models only write rules, read print, summarise and investigate.

## 2. Inspection, step by step (`Station.inspect`)

1. **Frame.** `FrameSource.steady()` returns the per-pixel median of the last five frames, which removes flicker bands from mains-powered lamps.
2. **Layout** (`pockets.find_layout`). SAM 3.1 is asked for the strip and the tablets with several prompts at once. The taught pocket template is fitted to the strip found; each pocket takes the nearest tablet outline. A product taught without a key uses the checklist grid.
3. **Count and strip shape.** A pocket count that differs from the rules is a HOLD (Rule 1). A strip whose shape no longer fits the taught strip is named *strip cut* and the pocket checks are skipped.
4. **Judge** (`judge.py`). DINOv2-small patch features of each pocket are compared with the taught good pockets; the score is the mean of the worst 2% of patch distances. A pocket fails when its score is over the pocket threshold. The strip border (pockets masked) is scored the same way against the border threshold.
5. **Name.** A failing pocket is named by its nearest prototype (`station._name`): painted examples made at teach time (empty, broken) and real examples from registered challenge strips. "Different tablet" is used only when the tablet colour differs from the taught tablet.
6. **Measure** (`measure.pocket_check`). Each outlined tablet is compared with the taught tablet profile: size, aspect ratio and Lab colour (ΔE). This finds two tablets in one pocket, a different tablet and a spot, and confirms an outlined empty dome.
7. **Fail-safes.** A pocket with no tablet outlined that the judge passed is held ("no tablet outlined — check by hand"). A finding the rules do not list is held, never passed. A SAM 3.1 failure holds the strip.
8. **Verdict.** REJECT if any listed rule failed, HOLD if anything needs a person, else PASS. Each reason carries rule, pocket, text and severity (critical, major, minor).

### Thresholds set at teach time

| Value | How it is set |
|---|---|
| Pocket threshold | Worst leave-one-photo-out score of the good teach photos × 1.5 (`judge.MARGIN`) |
| Border threshold | Same, × 2.5 (`judge.BORDER_MARGIN`); good foil borders vary more between strips |
| Tablet size / aspect tolerance | max(12% / 15% of the median, 3 standard deviations) |
| Tablet colour tolerance | max(12 ΔE, 3 standard deviations) |
| Empty-vs-broken margin | `station.EMPTY_MARGIN` = 0.12 similarity |

Thresholds, the checklist version and its SHA-256, and the model SHA-256 are printed in the record's Configuration block.

## 3. Self-test and challenge

- **Self-test** (`Station.self_test`, `defects.py`): paints an empty pocket, a broken tablet, a different (smaller) tablet and a spot onto the current good-strip photo, judges the painted strips together, and passes only if the good strip passes and every painted defect is caught and named right. Foil damage, strip cut and double tablet are listed as covered by real strips instead. A failed self-test locks the station and quarantines strips since the last passed one.
- **Challenge** (`challenge.py`): registered known-bad strips with an expected rule. A run must reject each one for that rule under the live camera. Registration with a blank kind lets the station detect kind, pocket and rule. Empty, broken and different-tablet registrations add real prototypes for naming.

## 4. Code map

| Module | Responsibility |
|---|---|
| `app.py` | FastAPI app, camera reader, MJPEG stream, all HTTP endpoints, command line |
| `d2i/station.py` | Teach, inspect, self-test, lock, naming of failing pockets |
| `d2i/pockets.py` | Strip and pocket finder (SAM 3.1 prompts; checklist grid offline) |
| `d2i/judge.py` | Frozen DINOv2 judge for pockets and the strip border |
| `d2i/measure.py` | Tablet and strip measurements against the taught profiles |
| `d2i/defects.py` | Painted defects for the self-test |
| `d2i/challenge.py` | Physical challenge set and runs |
| `d2i/checklist.py` | Sentence to numbered rules (Muse Spark, strict schema); approved versions |
| `d2i/commands.py` | Spoken commands to station actions |
| `d2i/printcheck.py` | Foil print read (Muse Spark) and field compare |
| `d2i/batch.py` | Line clearance, batch start and end, leak test, reconciliation |
| `d2i/ipqc.py` | Drift rule, hourly checks, release gate |
| `d2i/recheck.py` | Re-check window after a critical hold |
| `d2i/supervisor.py` | Shift Supervisor agent, its tools and QA requests |
| `d2i/llm.py` | Tool-calling chat: Muse Spark Open on Baseten, Muse Spark 1.3 fallback |
| `d2i/meta_api.py` | Meta Model API client: spend limit, one retry, transcript clean-up |
| `d2i/record.py` | Batch record, sign-off, questions about the records |
| `d2i/audit.py` | Hash-chained audit log, users and roles |
| `d2i/linestate.py` | PackML-style line state for `/board` |
| `d2i/openitems.py` | What waits for a person (board and sign gate) |
| `d2i/plc.py` | Modbus TCP server |
| `d2i/trace.py` | Model-call trace for the System screen |
| `d2i/tray.py` | Tray clip: one SAM 3.1 video call, one verdict per strip |
| `d2i/draw.py` | Pocket boxes and labels on frames and photos |
| `static/` | Station screens (`index.html`, `station.js`, `station.css`), line board, stack light, fonts |

## 5. Data files

Everything for a product lives in `data/<product>/`; users are in `data/users.json`. The folder is not under version control.

| File | Content |
|---|---|
| `checklists.jsonl` | Approved checklist versions |
| `good/`, `model.pt` | Teach photos; judge memory bank, thresholds, tablet and strip profiles |
| `log.jsonl` | Every verdict (strip id, status, reasons, user, batch) |
| `selftests.jsonl` | Self-test results |
| `challenge_set.json`, `challenge/`, `challenge_runs.jsonl` | Registered challenge strips, their photos and runs |
| `batches.jsonl`, `clearances.jsonl`, `prints.jsonl` | Batch start/end, line clearances, print checks |
| `states.jsonl` | Line state changes (time, from, to, cause, by, rule, strip) |
| `hold.json`, `recheck.json`, `requests.json` | Open hold, open re-check, agent requests waiting for QA |
| `rejects/` | Annotated reject and hold photos |
| `evidence/` | Pocket crops the agent measured, with their scores |
| `audit.jsonl` | Hash-chained audit log |
| `trace.jsonl` | Model calls: model, platform, job, milliseconds, cost |
| `deviations/` | Agent draft deviation notes |

`meta_spend.json` (repository root) holds the total paid-call spend.

## 6. HTTP API

All endpoints are served by `app.py`. Actions that change state need a login; QA actions ask for the QA password.

The server binds to 127.0.0.1 by default. Use `--host 0.0.0.0` only on a trusted network: the line board, stack light and a few question endpoints (`/command`, `/ask`, `/investigate`) need no login.

| Area | Endpoints |
|---|---|
| Screens | `GET /` station, `GET /board` line board, `GET /lamp` stack light, `GET /stream` camera (MJPEG), `GET /status`, `GET /line` |
| Login | `POST /login`, `POST /logout` |
| Setup | `POST /describe`, `POST /approve`, `POST /teach`, `GET /good/{i}`, `POST /selftest` |
| Inspection | `POST /inspect`, `POST /tray`, `POST /command`, `GET /rejects/{name}` |
| Batch | `POST /clearance`, `POST /batch/start`, `POST /print`, `POST /print/eye`, `POST /specimen/sign`, `POST /leak`, `POST /foil-change`, `POST /batch/end` |
| Line | `POST /hold`, `POST /resume`, `GET /recheck`, `GET /quarantine`, `POST /release` |
| Challenge | `POST /challenge/register`, `POST /challenge/inspect`, `POST /challenge/eject`, `POST /challenge/close`, `GET /challenge`, `GET /challenge/photo/{label}` |
| Agent | `POST /investigate`, `GET /investigation`, `POST /ask-operator-answer`, `POST /approve-request`, `POST /decline-request` |
| Record | `GET /record`, `POST /sign`, `GET /missed`, `POST /review-missed`, `POST /ask`, `GET /trace` |

## 7. PLC signals (Modbus TCP)

`plc.Plc` is a Modbus TCP server on port 5020 (all interfaces). It answers function codes 1 (read coils) and 3 (read holding registers); the machine polls it.

| Address | Type | Meaning |
|---|---|---|
| Coil 0 | FC1 | Reject pulse, 0.5 s per rejected strip |
| Coil 1 | FC1 | Station locked |
| Coil 2 | FC1 | Line held (by a verdict, a person or the agent; only a QA resume clears an agent hold) |
| Register 0 | FC3 | Passed strips |
| Register 1 | FC3 | Rejected strips |
| Register 2 | FC3 | Held strips |
| Register 3 | FC3 | Rule number of the last reject |

The connection state appears on the Run screen as "Reject gate".

## 8. Models, spend and trace

- **Meta Model API** (`meta_api.MetaClient`): SAM 3.1 image and video, Muse Spark 1.3 (`muse-spark-1.3`), Muse Voice Transcribe. Images are sent at most 1280 px on the long side.
- **Spend limit:** each call reserves its worst-case price before it is sent; the total is kept in `meta_spend.json` and calls stop at USD 50.00. If the file cannot be read, paid calls are refused.
- **Retry:** a busy (429, 5xx) or unreachable API is tried once more after 1 s.
- **Baseten** (`llm.ToolChat`): Muse Spark Open (`meta-models/muse-spark-open-1026`) for the agent's tool calls, with Muse Spark 1.3 as fallback.
- **Trace:** every call is written to `trace.jsonl` and shown on the System screen.

## 9. Testing

```bash
python -m pytest -q
```

564 tests, none of which makes a paid call. Model clients are replaced by fakes (`tests/test_station.py` has fake SAM clients that read the pixels of drawn strips), and test strips are drawn in code (`tests/conftest.py`). Screen scripts are parse-checked with Node.js when it is installed. Two tests judge a real photo from `photos/` and are skipped when it is absent.

## 10. Extending

- **A new product:** start with a new `--product` name, describe the strip, approve, teach. No code change.
- **A new tablet defect:** add its check to `measure.pocket_check` (measured) or a painted example in `defects.py` (judged), its text and severity to `station.TEXT`, and its rule kind to `checklist.CHECKS`.
- **Different PLC addresses:** change `Plc.coils` and `Plc.set_verdict` in `d2i/plc.py`.
- **Another camera:** `--source` takes any OpenCV camera index or a video file; teach again after moving the camera.
