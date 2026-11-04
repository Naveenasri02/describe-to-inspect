# User Guide

## Blistrue blister-strip inspection station

This guide is for the people who run the station: operators and QA. For installation see the [README](../README.md); for internals see the [Developer Guide](DEVELOPER_GUIDE.md).

---

## 1. What the station does

A camera above the line looks at one blister strip at a time. For each strip the station:

1. Finds the strip, every pocket and every tablet (SAM 3.1).
2. Scores each pocket against good strips it was taught (DINOv2, offline).
3. Measures each tablet against the taught tablet (size, shape, colour).
4. Applies the approved rules and gives one verdict: **PASS**, **REJECT** or **HOLD**, naming the rule and the pocket.
5. Sends the verdict to the machine (Modbus TCP) and writes it to the batch record and the audit log.

The batch is the unit of work. A batch is released for inspection only after line clearance, a passed self-test, a passed challenge run (when challenge strips are registered), a matching start-up print check and QA's signature on the start-up specimen.

## 2. Screens

| Screen | Who | What it is for |
|---|---|---|
| **Run** | Everyone | Live camera with pocket numbers, last verdict, strip counters and yield, checks on this strip, last 20 strips, status lamps, challenge run |
| **Setup** | QA | Describe the good strip, approve the rules, teach, self-test |
| **Quality (QA)** | QA | Line clearance, batch start, print check, start-up specimen, challenge set, leak test, holds and resume, quarantine, agent requests, end batch |
| **Record** | QA | Batch record, sign-off, missed-check review notes |
| **System** | QA | The models, the latest model calls, spend |
| `/board` | Anyone, read only | Line board for a screen beside the line: line state, why, since when, who must act, what is due next, counts, rejects by rule, timeline |
| `/lamp` | Anyone, read only | Red/green stack light for a phone or tablet |

Function keys on the Run screen:

| Key | Action |
|---|---|
| F1 | Inspect strip |
| F2 | Tray clip (one short video, one verdict per strip) |
| F3 | Hold line (anyone may hold; the hold reaches the machine at once) |
| F4 | Voice command (for example "inspect") |
| F5 | Last rejects |

## 3. Roles and login

Each person has a personal login. Create users from the command line:

```bash
python app.py --add-user qa1 --role qa
python app.py --add-user op1 --role operator
```

- **Operator:** inspect strips, tray clip, voice commands, challenge run, hold the line.
- **QA:** everything an operator can do, plus Setup, Quality (QA), Record and System. Approvals, releases, resumes and signatures ask for the QA password again.

Every verdict and every action carries the name of the logged-in person.

## 4. Setting up a product

Start the station with the product name: `python app.py --source 0 --product <name>`. Each product keeps its own rules, teaching and records in `data/<name>/`.

### 4.1 Describe the good strip (Setup, step 1)

Say or type one sentence about a **good** strip and the defects to reject, for example:

> "A good strip has fifteen white round tablets, three rows of five. Reject an empty pocket, a broken tablet, two tablets in one pocket, a different tablet or a spot."

Press **Speak rule** (then speak) or type it and press **Make checklist**. Muse Spark 1.3 turns it into numbered rules.

### 4.2 Approve the rules (Setup, step 2)

Check the numbered rules and press **Approve**. Each approved checklist gets a version number; the version in use is shown on the Run screen and in the batch record.

### 4.3 Teach (Setup, step 3)

Place a good strip under the camera and press **Save this strip** five times, moving the strip slightly each time. Using two different good strips (for example three photos of one and two of another) teaches normal strip-to-strip variation. The fifth press teaches the station.

A photo is refused, with its number, when it does not look like the others or when the pockets were not all found. Teach under the lighting the station will run in.

### 4.4 Register physical challenge strips (Quality (QA), optional)

Challenge strips are real known-bad strips that must be rejected for the right rule before each batch is released.

1. Place the known-bad strip under the camera.
2. Leave the label blank (the station names it C1, C2, ...) and leave the kind blank to let the station detect the defect, pocket and rule, or pick the kind yourself.
3. Press **Register challenge strip (photo from the camera)**.

A registered empty-pocket, broken-tablet or different-tablet strip also teaches the station what that defect looks like on this product, so later strips are named correctly. Register one real empty-pocket strip for each new product.

## 5. Starting a batch

### 5.1 Line clearance (Quality (QA))

Remove everything from the line and press **Line clearance**. The station looks for tablets or strips left in view and records who confirmed it. A batch can only start after a clear result that is less than 30 minutes old.

### 5.2 Batch start (Quality (QA))

Enter batch number, MFG, EXP, MRP, product name and strength, and press **Start batch**. These are the values the foil print is compared with.

- An EXP that is already past, or not after MFG, is refused.
- Leave strength blank when the strip does not print one.
- Combinations are compared part by part ("12.5 mg & 50 mg").

### 5.3 Self-test (Setup, step 4)

Place a good strip under the camera and press **Run self-test**. The station paints defects (empty pocket, broken tablet, different tablet, spot) onto the photo of the good strip and must catch each one and name it correctly, and the good strip must pass. The result shows "Good strip under the camera: passed" and "Built-in defect drill: N of N found".

The station stays locked until a self-test passes. The self-test must run after **Start batch**.

### 5.4 Challenge run (Run)

When challenge strips are registered, the Run screen shows a challenge card, for example "Place C1 (wrong tablet, expect Rule 5)":

1. Place the strip and press **Inspect**.
2. The station must reject it for the expected rule.
3. Take the strip out, tick **Ejected** and press **Record result**.

A miss, a wrong rule or a strip left on the line locks the station and opens quarantine. Challenge strips are never counted in the batch record.

### 5.5 Start-up print check (Quality (QA))

Place a start-up strip **foil side up** and press **Check print (foil side up)**. Muse Spark reads the six printed fields and the station compares them with the batch record:

- **PRINT OK:** every field matches.
- **HOLD — Check the print by eye:** a field differs or could not be read. QA looks at the strip and presses **Print matches** (QA password and a note) or **Wrong print** (holds the line as critical). "Print matches" is refused for a strip past its expiry.

### 5.6 Sign the start-up specimen (Quality (QA))

Enter the QA password and press **Sign start-up specimen (QA)**. When all release items show "Done", the line state becomes **RUNNING** and inspection is allowed.

## 6. Inspecting strips

Place a strip and press **Inspect strip** (F1), or say "inspect" with **Voice command** (F4). The Run screen shows:

- **Last verdict:** PASSED, REJECTED or HELD, with the reason, the rule and the pocket.
- **Camera:** the failing pocket boxed in red with its label.
- **Checks on this strip:** OK / NG for each check, with batch totals.
- **Strip counters:** good, rejected, held, total and yield.

### 6.1 What is checked

| Check | Severity | Decided by |
|---|---|---|
| Empty pocket | major | DINOv2 judge |
| Broken or chipped tablet | major | DINOv2 judge |
| Two tablets in one pocket | major | SAM 3.1 outlines + measurement |
| Different tablet (shape or colour) | critical | SAM 3.1 outlines + measurement |
| Spot on a tablet | minor | SAM 3.1 outlines + measurement |
| Foil damage at the strip border | major | DINOv2 judge on the border |
| Strip cut wrong or wrong size | major | SAM 3.1 strip outline + measurement |
| Pocket count differs from the rules | hold | SAM 3.1 pocket count |
| Foil print (batch, MFG, EXP, MRP, product, strength) | hold; critical after "Wrong print" | Muse Spark reads, code compares, QA decides |

A finding the rules do not list is held for QA ("... looks unusual — check by hand"), never passed. A pocket where no tablet was outlined is held for a hand check. Every REJECT or HOLD saves an annotated photo, shown in the batch record.

### 6.2 Safe by default

- If SAM 3.1 or the network does not answer (after one retry), the strip is **held** for a hand check: "Pocket finder (SAM 3.1) did not answer — check this strip by hand". The line board shows SUSPENDED.
- An inspection error is a HOLD that still reaches the machine.

## 7. When the line holds

The line state is shown on every screen and on `/board`.

| Cause | What happens | How it is cleared |
|---|---|---|
| Three rejects in a row | Line HELD (drift rule) | QA: **Resume line** with password and reason |
| Critical reject (different tablet) | Line HELD; strips passed since the last good check are listed for re-check | Re-check each listed strip, or QA gives a waiver reason; then resume |
| Wrong print | Line HELD (critical) | As above |
| Failed leak test | Line HELD; the leak test stays due until one passes | Pass a leak test; QA resumes |
| Failed self-test or missed challenge strip | Station LOCKED; strips since the last passed self-test are quarantined | Passing self-test unlocks; **Release quarantine** (QA password and reason) |
| Anyone presses **Hold line** (F3) | Line HELD at once | QA resumes |

### 7.1 Shift Supervisor agent

When rejects rise or a self-test fails, QA can press **Investigate** on the Quality (QA) screen. The Shift Supervisor (Muse Spark Open) uses measuring tools: image health, camera shift, reject statistics, comparison with the last good strip and per-pocket scores. It may ask the operator a question on screen, hold the line and draft a deviation note (headed "DRAFT — needs QA review").

The agent can only **request** a resume, re-teach, re-check or quarantine release. Each request waits on the Quality (QA) screen until QA presses **Approve (QA)** or **Decline (QA)** with the password. Both are audited.

## 8. During the batch

- **Hourly checks:** a leak test and a print check are due every hour; the QA screen shows what is due. A missed hour needs a QA review note before the record can be signed.
- **Foil change:** press **Foil roll changed**; the next print check is due after it.
- **Leak test:** done by a person; enter the result (pass/fail, samples, note).

## 9. Ending the batch and signing the record

### 9.1 End batch (Quality (QA))

Press **End batch** and enter the counts from the bench: strips received, good tray, reject box, samples, unused strips, and foil issued, used, returned and destroyed (metres), plus the after-batch clearance tick. Each line is compared with the verdict log against limits QA sets. Outside a limit, the batch ends only with the QA password and an investigation reason.

### 9.2 Sign the record (Record)

Press **Open shift record**. The record holds the batch details, release items, verdicts with photos, holds, agent steps, hourly checks, reconciliation and the configuration (model and checklist fingerprints, thresholds, camera). Muse Spark drafts a short summary.

Sign with the QA password and a meaning, **Reviewed** or **Approved** (**Sign record**). Signing is refused while anything still waits for a person; the screen names what is open. The signature keeps the name, local time with time zone and meaning. If the record changes after signing, it says so and needs a new signature.

## 10. Scope

The station checks the tablet side of the strip and the printed foil fields. Seal integrity is covered by the person's leak test, which the station logs. 2D codes, artwork wording, cartons and leaflets are outside its scope. Validation and calibration are done on site by the user's QA.

## 11. Troubleshooting

| Symptom | What to do |
|---|---|
| Camera pane is black | Check the camera cable and that no other program uses it; the station reopens a stalled camera by itself |
| Self-test: "Good strip not passed" after a change of light | Restore the lighting used at teaching, or teach again under the new light |
| False "no tablet" or wrong pocket numbers | Place the strip the same way round and in the same spot as at teaching |
| "Pocket finder (SAM 3.1) did not answer" | Check the network; strips are held, never passed, until it answers |
| Batch cannot start | Run line clearance first (valid for 30 minutes); check the API key and network |
| "NOT RELEASED" on the Run screen | Complete the release items on the Quality (QA) screen: self-test, challenge run, print check, specimen signature |
| Record cannot be signed | The screen lists what is still open (held line, quarantine, agent request, missed-check note) |
