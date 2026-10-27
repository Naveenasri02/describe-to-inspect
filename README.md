# Describe-to-Inspect

A webcam + laptop packaging-line QA bench for tablet blister strips. One spoken or typed sentence sets up the rules, the bench checks every pocket and names the rule that broke, tests itself every shift with painted defects, signals the machine and drafts the shift record.

Built for the Meta AI Developer Hackathon (Startup track).

Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo — everything here was written during the event (from the 26 Oct kickoff to 4 Nov).

## The problem

On a packaging line, QA runs a physical challenge test: a strip with known defects is put through the line to show the inspection catches it, and the result is written into the shift record by hand. The check and the paperwork both depend on someone remembering to do them, every shift.

## What it does

1. **Teach by sentence.** Say or type the rule in English or Tamil (for example "6 tablets in 2 rows of 3, reject empty pockets"). Muse Spark 1.3 turns it into a numbered checklist (`d2i/checklist.py`); a QA user approves it, and each approved checklist is versioned.
2. **Find the pockets.** SAM 3.1 outlines the strip and each pocket (`d2i/pockets.py`), with prompt fallbacks. A frozen, offline DINOv2 judge learns what a good pocket looks like from good strips (`d2i/judge.py`).
3. **Self-test every shift.** The bench paints fake defects onto a known-good strip and must catch all of them before it is allowed to inspect (`d2i/defects.py`, `d2i/station.py`). If it misses any, the station LOCKS.
4. **Inspect and reject.** Each strip gets one verdict that names the rule, for example "Rule 2 failed: pocket 7 is empty". The reject, hold and lock signals go out as Modbus TCP (`d2i/plc.py`); `/lamp` on a phone works as a red/green stack light. A tray clip can be judged in one SAM 3.1 video call, one verdict per strip (`d2i/tray.py`).
5. **Draft the record and hold the line.** Every verdict goes into a hash-chained, tamper-evident audit log with QA roles and quarantine (`d2i/audit.py`, `d2i/record.py`), and the shift record is drafted for QA sign-off. A Shift Supervisor agent (`d2i/supervisor.py`) investigates a failed self-test or a spike in rejects using measuring tools (image health, camera shift, reject stats, per-pocket scores), may hold the line, and can only request a re-teach, resume or quarantine release; QA approves with a password re-entry.

The frozen vision model decides pass or fail. The language model only drafts the checklist, the record and the agent's explanation.

## Run it

```
pip install -r requirements.txt
python app.py --add-user qa1 --role qa          # asks for a password; repeat with --role operator
python app.py --source 0 --product blister --port 8000
```

Open http://localhost:8000 for the bench screen and http://localhost:8000/lamp for the stack-light page. `--source` is a camera index or a video path; product data is kept in `data/<product>/`.

Keys: the Meta Model API key is read from `MODEL_API_KEY` or `~/.secrets/meta_model_api_key.txt`; the Baseten key from `BASETEN_API_KEY` or `~/.secrets/baseten_meta_hackathon_key.txt`. A hard $50 spend stop is built into the Meta client.

Tests (no paid calls): `python -m pytest -q -p no:warnings`

## Models and licences

| Model | Used for | How |
|---|---|---|
| DINOv2 (`facebook/dinov2-small`) | Pass/fail judge per pocket and strip border | Runs locally, frozen, offline |
| SAM 3.1 | Strip and pocket outlines; tray video | Meta Model API |
| Muse Spark 1.3 | Checklist from a sentence, record drafting, fallback for the agent | Meta Model API |
| Muse Voice Transcribe | Spoken setup and hands-free commands | Meta Model API |
| Muse Spark Open (`meta-models/muse-spark-open-1026`) | Shift Supervisor agent and model explanations | Open weights, served on Baseten |

Built with Muse Spark Open. The API models are used under the Meta Model API terms. This repository carries no licence file; the licence is the owner's choice.

## Results

No accuracy figures are claimed in this README. The only source of numbers is the two eval scripts, which run on real strips; their output is saved to `EVAL.md` and `EVAL_AGENT.md`:

```
python tools/eval_strips.py photos/good photos/bad --sam --rows 2 --per-row 3 > EVAL.md
python tools/eval_agent.py data/blister --bad photos/bad --runs 3 --yes-live
```

`eval_strips.py` prints its results (the `>` saves them as `EVAL.md`; `--sam` makes paid SAM calls). `eval_agent.py` calls the paid model, refuses to run without `--yes-live`, and writes `EVAL_AGENT.md`. Numbers measured on painted defects are never quoted.

## Honest limits

- This is a bench, not a line: it works at bench speed, not line speed. A 60-frame SAM video call takes about 2 minutes.
- The self-test is a per-shift functional check that runs on top of the physical challenge strip. It does not replace it.
- The shift record is a draft ("DRAFT — needs QA signature"). Validation of the system is for the user's own QA to do on site.
- The SAM prompt words were measured on real photos and may need tuning per product.
