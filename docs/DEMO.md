# 3-minute demo script

Packaging-line QA on a station screen. The quoted banner and button text is exact (it is what the running bench shows). The rule text, the heard transcript, the agent's tool calls and its reasons are examples: they depend on what is spoken and on what the model chooses. The footer "Built with Muse Spark Open" is always on the screen.

Before filming: start the app fresh, then QA logs in through the header ("Name", "Password", "Log in"); the header then shows the QA user's name and "(qa)". The station is already taught for this product.

| Time | Shot | On screen / spoken |
|---|---|---|
| 0:00-0:05 | Title card | "Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo." |
| 0:05-0:20 | A real damaged strip goes under the camera; QA presses "Inspect strip"; the lamp page on a phone turns red | Screen: banner "REJECT" with "Rule 2 failed: pocket 7 is empty". Voice: "A real strip, rejected, and it says which rule broke." (Filmed after the 0:55 shot, see the notes.) |
| 0:20-0:40 | Tamil moment. QA presses "Speak rule", speaks the rule in Tamil, presses "Stop" | Subtitle (added in the edit): the rule was spoken in Tamil. Screen: the numbered checklist, e.g. "Rule 1: exactly 6 tablets in 2 rows of 3", "Rule 2: no pocket may be empty", and "Waiting for approval". |
| 0:40-0:55 | The logged-in QA user presses "Approve" (the login is the approval; no password box) | Screen: the approved checklist with "approved_by" set to the QA user's name; the banner "LOCKED" with "No self-test this shift". Pockets are outlined on the strip. |
| 0:55-1:15 | A good strip under the camera. QA presses "Voice command", says in English "start the self test", presses "Stop" | Screen: "Heard: start the self test — Command: self_test", then the self-test result with each painted defect "caught": true, then the banner "READY". |
| 1:15-1:35 | Cover the lens and press "Self-test" | Screen: banner "LOCKED" with "Self-test missed: ..." and the bar "Quarantine: 1 strips since the last passed self-test — QA release needed". Lamp page turns red. |
| 1:35-2:10 | Uncover the lens. Type "self-test failed" into "Why? e.g. rejects rose" and press "Investigate" | Screen: the tool calls the agent chose, e.g. image_health, camera_shift, run_self_test; the banner "HOLD" with "Line held by the Shift Supervisor — QA must resume."; a request card: bold "reteach", the text "Agent request: <reason>", a "QA password" field and an "Approve (QA)" button. |
| 2:10-2:25 | QA types the QA password in that card and presses "Approve (QA)", then presses "Self-test" with the good strip | Screen: "Re-taught; the station stays locked until a self-test passes", then the self-test passes. The banner stays "HOLD": the line is still held. |
| 2:25-2:35 | QA types the QA password and a reason into "Reason to resume" and presses "Resume line (QA)"; then the QA password and a reason in the quarantine bar and "Release quarantine (QA)" | Screen: "Line resumed", then the quarantine bar goes away. |
| 2:35-2:48 | A good strip, then real damaged strips, each with "Inspect strip" | Screen: banner "PASS" and the lamp green on the good strip; banner "REJECT" with "Rule N failed: ..." and the lamp red on each damaged one. |
| 2:48-3:00 | QA opens "Shift record", types the QA password and presses "Sign record (QA)"; then the audit log | Screen: "DRAFT — needs QA signature". Voice: "Tamper-evident, and QA signs it." |

Notes
- Filming order: 0:20-1:15 first, right after the fresh start (the banner shows "READY" only while no strip has been judged in this session); then the 0:05-0:20 reject (that strip is the one the quarantine bar counts); then 1:15 onward.
- The demo is in English; only the 0:20-0:40 shot is Tamil, with English subtitles.
- Say "packaging-line QA" and "tamper-evident". Give no accuracy number except what `EVAL.md` holds.
- Say "an automatic self-test every shift, on top of the physical challenge strip".
- Check the exact on-screen wording against the running bench before filming; the lines above follow the code but were not filmed.
