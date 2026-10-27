# 3-minute demo script

Packaging-line QA on a station screen. The quoted banner and button text is exact (it is what the running bench shows). The rule text is an example: it depends on what is spoken. Whenever Muse Spark Open output is on screen, the footer "Built with Muse Spark Open" must be visible.

| Time | Shot | On screen / spoken |
|---|---|---|
| 0:00-0:05 | Title card | "Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo." |
| 0:05-0:20 | A real damaged strip goes under the camera; the lamp page on a phone turns red | Screen: "Rule 2 failed: pocket 7 is empty". Voice: "A real strip, rejected, and it says which rule broke." |
| 0:20-0:40 | Tamil moment. Speak the rule in Tamil into the mic | Subtitle (added in the edit): the rule was spoken in Tamil. Screen: the numbered checklist, e.g. "Rule 1: exactly 6 tablets in 2 rows of 3", "Rule 2: no pocket may be empty". |
| 0:40-0:55 | QA user approves the checklist with a password | Screen: the approved checklist with the QA user's name. Pockets are outlined on the strip. |
| 0:55-1:15 | Voice command in English: "start the self test" | Screen: painted defects caught one by one, then the banner "READY". |
| 1:15-1:35 | Cover the lens or change the lamp brightness | Screen: self-test fails, then the banner "LOCKED". Lamp page turns red. |
| 1:35-2:10 | Shift Supervisor investigates (footer "Built with Muse Spark Open" visible) | Screen: tool calls image_health, camera_shift, run_self_test, then the banner "HOLD" with "Line held by the Shift Supervisor — QA must resume." In the supervisor panel, a request card: bold "reteach", the text "Agent request: <reason>", a "QA password" field and an "Approve (QA)" button. |
| 2:10-2:25 | QA types the QA password in that card and presses "Approve (QA)" | Screen: the approval result; the self-test passes again and the banner shows "READY". |
| 2:25-2:45 | Real damaged strips rejected, each with its rule | Screen: "Rule N failed: ..." as the bench shows it; lamp red on each reject, green on a good strip. |
| 2:45-3:00 | The shift record opens, then the audit log | Screen: "DRAFT — needs QA signature". Voice: "Tamper-evident, and QA signs it." |

Notes
- The demo is in English; only the 0:20-0:40 shot is Tamil, with English subtitles.
- Say "packaging-line QA" and "tamper-evident". Give no accuracy number except what `EVAL.md` holds.
- Say "an automatic self-test every shift, on top of the physical challenge strip".
- Check the exact on-screen wording against the running bench before filming; the lines above follow the code but were not filmed.
