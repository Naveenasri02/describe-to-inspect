# Describe-to-Inspect

A webcam + laptop packaging-line QA bench for tablet blister strips. One spoken or typed sentence sets up the rules, the bench checks every pocket and names the rule that broke, runs an automatic self-test every shift, on top of the physical challenge strip, signals the machine and drafts the shift record (draft record — QA signs it).

Built for the Meta AI Developer Hackathon (Indie track).

Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo — everything here was written during the event (from the 26 Oct kickoff to 4 Nov).

## The problem

On a packaging line, QA runs a physical challenge test: a strip with known defects is put through the line to show the inspection catches it, and the result is written into the shift record by hand. The check and the paperwork both depend on someone remembering to do them, every shift. This bench is designed to support that work; it does not replace your QA.

## What it does

1. **Teach by sentence.** Say or type the rule in English or Tamil (for example "6 tablets in 2 rows of 3, reject empty pockets"). Muse Spark 1.3 turns it into a numbered checklist (`d2i/checklist.py`); a logged-in QA user approves it, and each approved checklist is versioned. Teaching is also QA-only.
2. **Find the pockets.** SAM 3.1 outlines the strip and each pocket (`d2i/pockets.py`), with prompt fallbacks; a product taught offline (no key) uses the checklist grid instead. A strip placed by hand, a little turned or shifted, is followed: each pocket takes the tablet SAM 3.1 finds nearest to it, and a pocket with no tablet keeps its taught box (that is how an empty pocket is seen). A frozen, offline DINOv2 judge learns what a good pocket looks like from good strips (`d2i/judge.py`); a teach photo that does not look like the others, or whose pockets SAM did not find, is refused by number. Two Meta models can cross-count the pockets: SAM 3.1 outlines them and Muse Spark points at them (`Station.crosscheck`); teaching with the cross-count on refuses a photo where the counts differ. It is a paid call, so it never runs inside an inspection, and it is a code option today (the station screen does not switch it on).
3. **Self-test every shift, and a physical challenge run.** The bench paints fake defects onto a known-good strip (always an empty pocket, plus every listed check) and must catch and name all of them before it is allowed to inspect (`d2i/defects.py`, `d2i/station.py`). If it misses any, the station LOCKS. On top of that, QA registers the physical challenge set (`d2i/challenge.py`): real known-bad strips, each with a label, its defect kind, the rule it must fail and a photo from the camera. When a set is registered, every batch needs a passed challenge run before release: each strip goes under the live camera (an uploaded photo is refused), the station must REJECT it for its expected rule, and the operator ticks "Ejected". A miss, a wrong rule or a strip left on the line locks the station and opens quarantine, as a failed self-test does. Challenge strips never count toward the batch. Self-test and challenge need a login; the self-test is QA-only.
4. **Inspect and reject.** Each strip gets one verdict that names the rule, for example "Rule 2 failed: pocket 7 is empty". Inspect, tray clip and the voice "inspect" need a login, and every verdict row carries the logged-in user. The reject, hold and lock signals go out as Modbus TCP (`d2i/plc.py`); `/lamp` on a phone works as a red/green stack light. Anyone at the bench can press "Hold line"; the hold reaches the machine at once, even while a tray clip, a teach or a self-test runs (the screen, the board and the audit log show it when that action ends). An inspection error is a HOLD that reaches the machine even if the log cannot be written. The "Tray clip" button records a short clip and judges it in one SAM 3.1 video call, one verdict per strip (`d2i/tray.py`).
5. **Draft the record and hold the line.** Human actions and the Shift Supervisor agent's steps go into a hash-chained, tamper-evident audit log with QA roles (`d2i/audit.py`). A failed self-test or a missed challenge strip quarantines every strip inspected since the last passed self-test; a later passing self-test unlocks the station, but only a QA release (password and reason) closes the quarantine. Verdicts, self-tests and line states are kept in `log.jsonl`, `selftests.jsonl` and `states.jsonl`; the shift record (`d2i/record.py`) is a draft until QA signs it (see Sign-off below). The Shift Supervisor agent (`d2i/supervisor.py`) investigates a failed self-test or a rise in rejects using measuring tools (image health, camera shift, reject stats, per-pocket scores), may hold the line, can write a draft deviation note for QA (`deviations/*.md`, headed "DRAFT — needs QA review", with each step it took), and can only request a re-teach, resume, quarantine release or re-check; a logged-in QA user approves each request ("Approve (QA)") or declines it with a reason ("Decline (QA)"), with a password re-entry. Both are audited. Requests waiting for QA are kept in `requests.json`, so a restart or a re-teach does not drop them. Built with Muse Spark Open.
6. **Show what each model did.** The System tab's "What each model did" lists each model call: model and platform, job, time in ms and cost (`d2i/trace.py`, kept in `trace.jsonl`). The record's Configuration block holds the model SHA-256, the checklist version and its SHA-256, the pocket and border thresholds, the camera source and the app version.

The frozen vision model decides pass or fail. Muse Spark 1.3 drafts the checklist, reads the foil print and drafts the shift-record summary, and answers questions about the records; Muse Spark Open runs the Shift Supervisor agent. Neither language model decides pass or fail.

## What it checks

Each strip gets one verdict. Every reason carries a severity: critical, major or minor. A critical reject holds the line.

| Condition | Side of the strip | Severity | What decides it | Works offline |
|---|---|---|---|---|
| Empty pocket | Tablet side | major | DINOv2 judge (frozen) against good pockets | Yes |
| Broken tablet | Tablet side | major | DINOv2 judge (frozen) against good pockets | Yes |
| Two tablets in one pocket | Tablet side | major | SAM 3.1 outlines the tablets, then plain measurement against the taught tablet | No (needs SAM 3.1) |
| Wrong tablet (shape or colour) | Tablet side | critical | SAM 3.1 outlines, then measurement against the taught tablet | No (needs SAM 3.1); offline, a different tablet is rejected by the judge as broken/empty (major) and does not hold the line |
| Spot on a tablet | Tablet side | minor | SAM 3.1 outlines, then measurement against the taught tablet | No (needs SAM 3.1) |
| Foil damage at the strip border | Foil or tablet side | major | DINOv2 judge (frozen) on the strip border | Yes |
| Strip cut wrong or wrong size | Tablet side | major | SAM 3.1 strip outline, then measurement against the taught strip | No (needs SAM 3.1) |
| Pocket count differs from the checklist | Tablet side | HOLD | SAM 3.1 pockets counted against the rule | No (needs SAM 3.1; the offline checklist grid always has the checklist count) |
| Foil print: batch, MFG, EXP, MRP, product name, strength | Foil side | HOLD; critical only after QA's "Wrong print" | Muse Spark reads the six fields; plain code compares them with the batch record; QA decides any difference by eye (strength by number and unit, so "500mg" matches "500 mg"; the product name is the brand name printed largest, not the ingredient line, and it matches when every word QA typed is printed in that order, case and punctuation ignored, so "Paracetamol" matches "Paracetamol Tablets I.P."; a name cut off by the strip edge, such as "Rapeed-" for "Rapeed-20", is "not readable — check by hand", not a difference) | No (needs the network) |

Notes:

- A finding the checklist does not list is not rejected; it is held for QA ("... looks unusual — check by hand").
- Muse Spark is a generative model, so it only flags the print; a person decides (draft EU GMP Annex 22: such a model must not make a critical GMP decision alone). Any difference between its read and the batch record (a look-alike character such as I/1, O/0, S/5 or B/8, a digit slip, a date read wrong) holds the strip "Check the print by eye", never the line. QA looks at the strip and presses "Print matches" (QA password and a note: PRINT OK, audited, and it counts for the start-up print check and release) or "Wrong print" (QA password: a critical wrong print that holds the line). A print that cannot be read holds the strip only ("check by hand").
- A strip cut short skips the pocket checks; the screen says "Not checked: pocket checks (strip cut)". Checks that cannot run are always listed on the strip's verdict and in the shift record.
- **Wi-Fi off: nothing passes unchecked.** A product taught with SAM 3.1 holds every strip for a hand check when SAM 3.1 does not answer (after one retry): "Pocket finder (SAM 3.1) did not answer — check this strip by hand". It is never judged with a different pocket finder, and its self-test does not run ("Self-test could not run: the pocket finder (SAM 3.1) did not answer — try again"; nothing is recorded). The line board shows SUSPENDED meanwhile.
- "Works offline" in the table means a product taught offline (no key) inspecting a batch that is already released; starting or releasing a batch needs the network (see Run it).
- Every strip REJECT or HOLD saves an annotated photo (clip-level tray holds have no single strip image, and a good strip held only because the line is held saves none) in `data/<product>/rejects/` (strip outline blue, each failing pocket red with its rule number, the strip outline red for a strip-level reason), drawn on the layout the strip was judged on. The batch record shows the latest 12 as thumbnails with their reasons.

## Not checked by this bench

Not checked by this bench: seal pinholes, leak integrity (logged from the person's test), QR/2D codes, artwork wording, cartons and leaflets, serialization. Machine temperatures stay in your batch record. Validation and calibration are done on site by your QA.

Also:

- Small nicks at the very edge of the foil may be missed by the border check (being measured on real strips).
- Leak test: done by a person; the bench logs the result (pass or fail, samples, a note), and a fail holds the line.
- Tablet size in millimetres: it compares with the taught tablet, in pixels.
- Line speed: it is a bench, one strip at a time.

## Line process

The batch is the unit of work. All of this is logged and printed in the shift record.

1. **Line clearance.** Before a batch starts, the station looks for tablets or strips left on the line and records who confirmed it. A batch cannot start until the last clearance is clear, no more than 30 minutes old and made for that batch.
2. **Batch start.** QA enters batch, MFG, EXP, MRP, product name and strength. These are the values the foil print is compared with. Strength is left blank when the strip prints none; it is then not compared, and the record says "strength: not printed". A combination is compared part by part, in order, whatever joins the parts ("12.5 mg & 50 mg", "400 mg, 325 mg", "Ibuprofen 400 mg & Paracetamol 325 mg").
3. **Release gate.** Inspection is refused until the batch has a passed self-test run after the batch started, a passed physical challenge run for this batch (when a challenge set is registered), a start-up print check that matches the batch record (PRINT OK, or QA's "Print matches" after a check by eye), and QA's signature on that start-up specimen.
4. **Hourly checks.** A leak test and a print check are due every hour (the leak test is done by a person and its result entered); the QA screen shows what is due. A missed hour is listed in the record and needs a QA review note (password re-entry) before that batch's record can be signed, also after the next batch has started. The QA screen's "Missed hourly checks: QA review note" lists the missed hours of the current batch, or of an earlier batch typed in its batch field; the record shows each note with who wrote it and when.
5. **Foil change.** "Foil roll changed" starts the hourly print clock again; the next print check is due after it.
6. **Drift rule.** Three rejects in a row hold the line, and so does any critical reject, a wrong print (QA's "Wrong print") or a failed leak test (a failed leak test also stays due until one passes). QA resumes the line with the QA password and a reason.
7. **Re-check window.** A critical hold (wrong tablet, wrong print) lists the strips passed since the last good check (batch release, the last passed print check or the last QA resume) as a re-check (`d2i/recheck.py`). While it is open, each "Inspect strip" re-checks one of them and links its new verdict; the line resumes only when the re-check is done or QA gives a waiver reason. The Shift Supervisor can request a re-check; QA approves it.
8. **Reconciliation.** "End batch" takes counts typed at the bench: strips received, good tray, reject box, samples taken, unused strips, and foil issued, used, returned and destroyed (in metres), plus an after-batch clearance tick. Each line is compared with the verdict log against limits QA sets (site policy). Outside a limit, the batch ends only with the QA password and an investigation reason; the record shows each line against its limit, and who investigated and why.
9. **Line board (read-only).** `/board` is the stack light with reasons, for a screen beside the line: the PackML-style state (IDLE, STARTING, RUNNING, HELD, SUSPENDED, STOPPED, COMPLETE), why, since when, what waits for a person and who must act, what is due next, the batch counts, rejects by rule and today's timeline. It refreshes every second, needs no login and has no buttons: every action stays on the station behind a personal login. Each state change is appended to `data/<product>/states.jsonl` (time, from, to, cause, by, rule, strip). It is also linked from the System tab.
10. **Sign-off.** QA signs the shift record with the password and a meaning, "Reviewed" or "Approved". Signing is refused while anything still waits for a person (a held line, open quarantine or re-check, an agent request, a reconciliation outside its limit with no reason, a missed check with no review note in the current batch or in the record's own batch, a failed challenge; a failed challenge of a batch that has ended is closed by QA with the password and a reason, "Close failed challenge of an ended batch (QA)", and is audited); the refusal names the first five and how many more. The check and the signature are one step under the station lock, so a signature waits while an inspection or a self-test runs. The signature manifest keeps the name, the local time with its time zone and the meaning; the signed audit entry pins `log.jsonl`, `selftests.jsonl` and `states.jsonl` by SHA-256. A re-opened record shows "Signed by ..." only while its content is unchanged; otherwise it says it changed since signing and needs a new signature.

## Run it

Python 3.12 (numpy 1.26.4 has no wheel for 3.13).

```
pip install -r requirements.txt
python app.py --add-user qa1 --role qa          # asks for a password; repeat with --role operator
python app.py --source 0 --product blister --port 8000
```

Steps on a new machine, in order (the screens are the tabs Run / Setup / QA / Record / System; Setup, QA and System show after a QA login):

1. Put the Meta Model API key in `MODEL_API_KEY` or `~/.secrets/meta_model_api_key.txt`. It is needed to make a checklist, to find pockets with SAM 3.1 and to read the foil print. With no key, no network or the $50 stop reached, no batch can be started or released (line clearance needs SAM 3.1, the start-up print check needs Muse Spark). A product taught with SAM 3.1 needs SAM 3.1 to inspect: when SAM does not answer (after one retry), each strip is HELD for a hand check, never judged with a different pocket finder. A product taught offline (no key) inspects offline with the checklist grid.
2. Start the app. DINOv2 (`facebook/dinov2-small`) downloads on the initial start.
3. Open http://localhost:8000 and log in as the QA user in the header.
4. Setup, step 1: describe the good strip (type it and press "Make checklist", or "Speak rule"). Step 2: check the rules and press "Approve".
5. Step 3: put a good strip under the camera and press "Save this strip" five times, moving the strip a little each time. The fifth press teaches the station.
6. Optional, QA screen, "Physical challenge set": put each known-bad strip under the camera, give it a label (for example C1), pick its defect kind (the expected rule fills in) and press "Register challenge strip (photo from the camera)". Once a set is registered, every batch needs a passed challenge run.
7. QA screen: press "Line clearance" (the station looks for anything left on the line), enter the batch number, MFG, EXP, MRP, product name and strength (for example 500 mg; blank if the strip prints none) and press "Start batch".
8. Run the self-test after Start batch (a self-test from before the batch started does not release it): Setup, step 4, press "Run self-test" with a good strip under the camera. The station unlocks only when it passes.
9. If a challenge set is registered: Run screen, "Challenge run": place each strip as the card says, press "Inspect", take the rejected strip out, tick "Ejected" and press "Record result".
10. Release the batch (the ticks under "Batch release"): the self-test from step 8, the challenge run from step 9 (when a set is registered), "Check print (foil side up)" on a start-up strip (if it says "HOLD — Check the print by eye", QA looks at the strip and presses "Print matches" with the QA password and a note, or "Wrong print"), and "Sign start-up specimen (QA)" with the QA password. Strips cannot be inspected until all are done; the Run screen says "NOT RELEASED" until then.
11. Run screen: press "Inspect strip".

`--source` is a camera index, a video path or a photo folder (a folder cycles one photo every 2 s; the tray clip needs a camera or video, and the challenge run needs the live source). Product data is kept in `data/<product>/`. The app listens on 127.0.0.1; add `--host 0.0.0.0` to open http://<laptop-ip>:8000/lamp (the stack-light page) or http://<laptop-ip>:8000/board (the line board) from a phone or a second screen on the same network.

Keys: the Meta Model API key as above; the Baseten key from `BASETEN_API_KEY` or `~/.secrets/baseten_meta_hackathon_key.txt`. The only paid-call limit is a hard $50.00 spend stop built into the Meta client (there is no date stop): the app and the tools all count spend in one file, `meta_spend.json` at the repo root. If that file cannot be read, or the $50 stop is reached, paid calls are refused: no new batch can be started or released, and a product taught with SAM 3.1 holds each strip for a hand check (a product taught offline keeps inspecting with the checklist grid). A busy or unreachable Meta API (429, 5xx, timeout, no connection) is tried once more after 1 s; each try is counted against the stop.

## Tests

510 tests (count them with `python -m pytest --collect-only -q`). None makes a paid call: the model calls go to fakes, and the test strips are drawn in code (`tests/conftest.py`). Run them all with:

```
python -m pytest -q -p no:warnings
```

They cover the verdict path, the self-test and challenge locks, quarantine, the release gate, hourly checks, reconciliation limits, the re-check window, the print compare, the line-board states and `states.jsonl`, the sign gate and signature manifest, the audit chain, the agent's tools and approvals, the spend stop, and the screens' scripts (these parse checks need Node.js and are skipped without it).

## Models and licences

| Model | Used for | How |
|---|---|---|
| DINOv2 (`facebook/dinov2-small`) | Pass/fail judge per pocket and strip border | Runs locally, frozen, offline |
| SAM 3.1 | Strip, pocket and tablet outlines; line clearance; tray clip video | Meta Model API |
| Muse Spark 1.3 | Checklist from a sentence, foil print read, shift-record summary, answers about the records, the pocket count for the cross-count; fallback for the agent | Meta Model API |
| Muse Voice Transcribe | Spoken setup and hands-free commands | Meta Model API |
| Muse Spark Open (`meta-models/muse-spark-open-1026`) | Shift Supervisor agent and its draft deviation notes | Open weights, served on Baseten |

Built with Muse Spark Open. The API models are used under the Meta Model API terms.

## Results

No accuracy figures are claimed in this README. The only source of numbers is the two eval scripts, which run on real strips; their output is saved to `EVAL.md` and `EVAL_AGENT.md`:

```
python tools/eval_strips.py photos/good photos/bad --sam --rows 2 --per-row 3 > EVAL.md
python tools/eval_agent.py data/blister --bad photos/bad --runs 3 --yes-live
```

`eval_strips.py` prints its results (the `>` saves them as `EVAL.md`; `--sam` makes paid SAM calls). `eval_agent.py` calls the paid model, refuses to run without `--yes-live`, and writes `EVAL_AGENT.md`. Both count their spend in `meta_spend.json`. Numbers measured on painted defects are never quoted.

## Honest limits

- This is a bench, not a line: it works at bench speed, not line speed. One inspection takes about 12 s per strip, measured on a home connection (most of it is the SAM 3.1 calls). A 60-frame SAM video call takes about 2 minutes.
- The self-test is a per-shift functional check that runs on top of the physical challenge strip. It adds to the physical challenge test; it does not replace it.
- The shift record is a draft ("DRAFT — needs QA signature"). Your own QA decides whether to use this.
- The SAM / Muse Spark cross-count is a code option: the station screen does not switch it on.
- The tray clip opens the camera again while the stream is running; if the camera driver allows only one user, the clip fails with an error and nothing is judged.
- The SAM prompt words were measured on real photos and may need tuning per product.
- With `--host 0.0.0.0` anyone on the network can call the paid endpoints (`/describe`, `/command`, `/ask`, `/investigate`); keep the default 127.0.0.1 for a bench.

## Licence

PolyForm Noncommercial License 1.0.0 (see `LICENSE`). You are free to read, run and change this code for non-commercial use; commercial use needs permission from the owner.
