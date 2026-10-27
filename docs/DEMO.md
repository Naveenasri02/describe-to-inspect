# 3-minute demo script

Packaging-line QA on a station screen. The quoted words are spoken; the screen text is exact. Whenever Muse Spark Open output is on screen, the footer "Built with Muse Spark Open" must be visible.

| Time | Shot | On screen / spoken |
|---|---|---|
| 0:00-0:05 | Title card | "Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo." |
| 0:05-0:20 | A real damaged strip goes under the camera; the lamp page on a phone turns red | Screen: "Rule 2 failed: pocket 7 is empty". Voice: "A real strip, rejected, and it says which rule broke." |
| 0:20-0:40 | Tamil moment. Speak the rule in Tamil into the mic | Subtitle: "Rule spoken in Tamil". Screen: the numbered checklist, e.g. "Rule 1: exactly 12 tablets in 2 rows of 6", "Rule 2: reject if a pocket is empty". |
| 0:40-0:55 | QA user approves the checklist with a password | Screen: the approved checklist with the QA user's name. Pockets are outlined on the strip. |
| 0:55-1:15 | Voice command in English: "start the self test" | Screen: painted defects caught one by one, then "Station ready". |
| 1:15-1:35 | Cover the lens or change the lamp brightness | Screen: self-test fails, then "LOCKED". Lamp page turns red. |
| 1:35-2:10 | Shift Supervisor investigates (footer "Built with Muse Spark Open" visible) | Screen: tool calls image_health, camera_shift, run_self_test, then the line is held and "Re-teach requested - waiting for QA". |
| 2:10-2:25 | QA re-types the password to approve the re-teach | Screen: re-teach approved; the self-test passes again, "Station ready". |
| 2:25-2:45 | Real damaged strips rejected, each with its rule | Screen: "Rule N failed: ..." as the bench shows it; lamp red on each reject, green on a good strip. |
| 2:45-3:00 | The shift record opens, then the audit log | Screen: "DRAFT — needs QA signature". Voice: "Tamper-evident, and QA signs it." |

Notes
- English first; only the 0:20-0:40 shot is Tamil, with English subtitles.
- Say "packaging-line QA" and "tamper-evident". Give no accuracy number except what `EVAL.md` holds.
- Say "an automatic self-test every shift, on top of the physical challenge strip".
- Check the exact on-screen wording against the running bench before filming; the lines above follow the code but were not filmed.
