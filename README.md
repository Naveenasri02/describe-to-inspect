# Describe-to-Inspect

A webcam + laptop packaging-line QA bench for tablet blister strips. One spoken or typed sentence sets up the rules, the bench checks every pocket and names the rule that broke, tests itself every shift with painted defects, signals the machine and drafts the shift record.

Built for the Meta AI Developer Hackathon (Indie track).

Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo — everything here was written during the event (from the 26 Oct kickoff to 4 Nov).

## The problem

On a packaging line, QA runs a physical challenge test: a strip with known defects is put through the line to show the inspection catches it, and the result is written into the shift record by hand. The check and the paperwork both depend on someone remembering to do them, every shift.

## What it does

1. **Teach by sentence.** Say or type the rule in English or Tamil (for example "6 tablets in 2 rows of 3, reject empty pockets"). Muse Spark 1.3 turns it into a numbered checklist (`d2i/checklist.py`); a logged-in QA user approves it, and each approved checklist is versioned. Teaching is also QA-only.
2. **Find the pockets.** SAM 3.1 outlines the strip and each pocket (`d2i/pockets.py`), with prompt fallbacks; without the Meta API the bench falls back to the checklist grid. A frozen, offline DINOv2 judge learns what a good pocket looks like from good strips (`d2i/judge.py`).
3. **Self-test every shift.** The bench paints fake defects onto a known-good strip (always an empty pocket, plus every listed check) and must catch all of them before it is allowed to inspect (`d2i/defects.py`, `d2i/station.py`). If it misses any, the station LOCKS.
4. **Inspect and reject.** Each strip gets one verdict that names the rule, for example "Rule 2 failed: pocket 7 is empty". The reject, hold and lock signals go out as Modbus TCP (`d2i/plc.py`); `/lamp` on a phone works as a red/green stack light. The "Tray clip" button records a short clip and judges it in one SAM 3.1 video call, one verdict per strip (`d2i/tray.py`).
5. **Draft the record and hold the line.** Human actions and the Shift Supervisor agent's steps go into a hash-chained, tamper-evident audit log with QA roles and quarantine (`d2i/audit.py`). Verdicts and self-tests are kept in `log.jsonl` and `selftests.jsonl`; when QA signs the shift record (`d2i/record.py`), the sha256 of both files goes into the signed audit entry. The Shift Supervisor agent (`d2i/supervisor.py`) investigates a failed self-test or a spike in rejects using measuring tools (image health, camera shift, reject stats, per-pocket scores), may hold the line, and can only request a re-teach, resume or quarantine release; a logged-in QA user approves each request with a password re-entry.

The frozen vision model decides pass or fail. Muse Spark 1.3 drafts the checklist and the shift-record summary and answers questions about the records; Muse Spark Open runs the Shift Supervisor agent. Neither language model decides pass or fail.

## What it checks

Each strip gets one verdict. Every reason carries a severity: critical, major or minor. A critical reject holds the line.

| Condition | Side of the strip | Severity | What decides it | Works offline |
|---|---|---|---|---|
| Empty pocket | Tablet side | major | DINOv2 judge (frozen) against good pockets | Yes |
| Broken tablet | Tablet side | major | DINOv2 judge (frozen) against good pockets | Yes |
| Two tablets in one pocket | Tablet side | major | SAM 3.1 outlines the tablets, then plain measurement against the taught tablet | No (needs SAM 3.1) |
| Wrong tablet (shape or colour) | Tablet side | critical | SAM 3.1 outlines, then measurement against the taught tablet; a pocket the DINOv2 judge fails can also be named by the DINOv2 prototypes | Judge route: yes; measurement: no (needs SAM 3.1) |
| Spot on a tablet | Tablet side | minor | SAM 3.1 outlines, then measurement against the taught tablet | No (needs SAM 3.1) |
| Foil damage at the strip border | Foil or tablet side | major | DINOv2 judge (frozen) on the strip border | Yes |
| Strip cut wrong or wrong size | Tablet side | major | SAM 3.1 strip outline, then measurement against the taught strip | No (needs SAM 3.1) |
| Pocket count differs from the checklist | Tablet side | major | SAM 3.1 pockets (or the checklist grid) counted against the rule | Grid only |
| Foil print: batch, MFG, EXP, MRP | Foil side | HOLD | Muse Spark reads the four fields; plain code compares them with the batch record | No (needs the network) |

Notes:

- A finding the checklist does not list is not rejected; it is held for QA ("... looks unusual — check by hand").
- A wrong print holds the strip and the line; a print that cannot be read holds the strip only.
- A strip cut short skips the pocket checks; the screen says "Not checked: pocket checks (strip cut)". Checks that cannot run are always listed on the strip's verdict and in the shift record.
- Every REJECT or HOLD saves an annotated photo in `data/<product>/rejects/` (strip outline blue, each failing pocket red with its rule number, the strip outline red for a strip-level reason). The batch record shows the latest 12 as thumbnails with their reasons.

## What it does not check

- Seal pinholes and wrinkles.
- Leak detection: the leak test is done by a person; the bench only logs the result (pass or fail, with a note).
- Tablet size in millimetres (it compares with the taught tablet, in pixels).
- Line speed: it is a bench, one strip at a time.

## Line process

The batch is the unit of work. All of this is logged and printed in the shift record.

1. **Line clearance.** Before a batch starts, the station looks for tablets or strips left on the line and records who confirmed it. A batch cannot start until the last clearance is clear, no more than 30 minutes old and made for that batch.
2. **Batch start.** QA enters batch, MFG, EXP and MRP. These are the values the foil print is compared with.
3. **Release gate.** Inspection is refused until the batch has a passed self-test, a start-up print check that matches the batch record, and QA's signature on that start-up specimen.
4. **Hourly checks.** A leak test and a print check are due every hour (the leak test is done by a person and its result entered); the QA screen shows what is due, and a missed hour is listed in the record.
5. **Foil change.** "Foil roll changed" starts the hourly clock again; the next print check is due after it.
6. **Drift rule.** Three rejects in a row hold the line, and so does any critical reject or a wrong print. QA resumes the line with the QA password and a reason.
7. **Reconciliation.** "End batch" counts inspected = passed + rejected + held + no strip and writes whether the totals balance.

## Run it

Python 3.12 (numpy 1.26.4 has no wheel for 3.13).

```
pip install -r requirements.txt
python app.py --add-user qa1 --role qa          # asks for a password; repeat with --role operator
python app.py --source 0 --product blister --port 8000
```

Steps on a new machine, in order (the screens are the tabs Run / Setup / QA / Record / System; Setup, QA and System show after a QA login):

1. Put the Meta Model API key in `MODEL_API_KEY` or `~/.secrets/meta_model_api_key.txt`. It is needed to make a checklist, to find pockets with SAM 3.1 and to read the foil print; without it the bench can still inspect with the offline checklist grid.
2. Start the app. DINOv2 (`facebook/dinov2-small`) downloads on the initial start.
3. Open http://localhost:8000 and log in as the QA user in the header.
4. Setup, step 1: describe the good strip (type it and press "Make checklist", or "Speak rule"). Step 2: check the rules and press "Approve".
5. Step 3: put a good strip under the camera and press "Save this strip" five times, moving the strip a little each time. The fifth press teaches the station.
6. Step 4: press "Run self-test" with a good strip under the camera. The station unlocks only when it passes.
7. QA screen: press "Line clearance" (the station looks for anything left on the line), enter the batch number, MFG, EXP and MRP and press "Start batch".
8. Release the batch (the three ticks under "Batch release"): the self-test for this batch, "Check print (foil side up)" on a start-up strip, and "Sign start-up specimen (QA)" with the QA password. Strips cannot be inspected until all three are done; the Run screen says "NOT RELEASED" until then.
9. Run screen: press "Inspect strip".

`--source` is a camera index, a video path or a photo folder (a folder cycles one photo every 2 s; the tray clip needs a camera or video). Product data is kept in `data/<product>/`. The app listens on 127.0.0.1; add `--host 0.0.0.0` to open http://<laptop-ip>:8000/lamp (the stack-light page) from a phone on the same network.

Keys: the Meta Model API key as above; the Baseten key from `BASETEN_API_KEY` or `~/.secrets/baseten_meta_hackathon_key.txt`. The only paid-call limit is a hard $50.00 spend stop built into the Meta client (there is no date stop): the app and the tools all count spend in one file, `meta_spend.json` at the repo root. If that file cannot be read, paid calls are refused and inspection keeps working offline.

Tests (no paid calls): `python -m pytest -q -p no:warnings`

## Models and licences

| Model | Used for | How |
|---|---|---|
| DINOv2 (`facebook/dinov2-small`) | Pass/fail judge per pocket and strip border | Runs locally, frozen, offline |
| SAM 3.1 | Strip and pocket outlines; tray clip video | Meta Model API |
| Muse Spark 1.3 | Checklist from a sentence, shift-record summary, answers about the records; fallback for the agent | Meta Model API |
| Muse Voice Transcribe | Spoken setup and hands-free commands | Meta Model API |
| Muse Spark Open (`meta-models/muse-spark-open-1026`) | Shift Supervisor agent | Open weights, served on Baseten |

Built with Muse Spark Open. The API models are used under the Meta Model API terms.

## Results

No accuracy figures are claimed in this README. The only source of numbers is the two eval scripts, which run on real strips; their output is saved to `EVAL.md` and `EVAL_AGENT.md`:

```
python tools/eval_strips.py photos/good photos/bad --sam --rows 2 --per-row 3 > EVAL.md
python tools/eval_agent.py data/blister --bad photos/bad --runs 3 --yes-live
```

`eval_strips.py` prints its results (the `>` saves them as `EVAL.md`; `--sam` makes paid SAM calls). `eval_agent.py` calls the paid model, refuses to run without `--yes-live`, and writes `EVAL_AGENT.md`. Both count their spend in `meta_spend.json`. Numbers measured on painted defects are never quoted.

## Honest limits

- This is a bench, not a line: it works at bench speed, not line speed. A 60-frame SAM video call takes about 2 minutes.
- The self-test is a per-shift functional check that runs on top of the physical challenge strip. It adds to the physical challenge test; it does not replace it.
- The shift record is a draft ("DRAFT — needs QA signature"). Your own QA decides whether to use this.
- The tray clip opens the camera again while the stream is running; if the camera driver allows only one user, the clip fails with an error and nothing is judged.
- The SAM prompt words were measured on real photos and may need tuning per product.

## Licence

PolyForm Noncommercial License 1.0.0 (see `LICENSE`). You are free to read, run and change this code for non-commercial use; commercial use needs permission from the owner.
