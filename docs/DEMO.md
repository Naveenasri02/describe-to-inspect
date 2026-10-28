# 3-minute demo script

Packaging-line QA on a station screen (tabs Run / Setup / QA / Record / System). The quoted banner, button and tab text is exact (it is what the running bench shows). The rule text, the heard transcript, the agent's tool calls and its reasons are examples: they depend on what is spoken and on what the model chooses. The footer "Built with Muse Spark Open" is always on the Run screen.

Before filming: start the app fresh, then QA logs in through the header ("Name", "Password", "Log in"). The Setup, QA, Record and System tabs appear after the login. Have a good strip, a strip with an empty pocket, a strip with a wrong tablet, and a strip with a printed foil that matches the batch.

| Time | Shot | On screen / spoken |
|---|---|---|
| 0:00-0:05 | Title card | "Pre-existing work: the idea and a private prototype made on 25–26 Oct 2026 before kickoff; none of its code is in this repo." |
| 0:05-0:20 | Setup tab, step 1 "Describe the good strip". QA presses "Speak rule", speaks the rule in Tamil, presses stop; then step 2 "Approve the rules" and "Approve" | Subtitle (added in the edit): the rule was spoken in Tamil. Screen: the numbered rules under "Rules for a good strip", then "Checklist version 1 approved." |
| 0:20-0:35 | Step 4 "Self-test": a good strip under the camera, "Run self-test" | Screen: each painted defect listed as ": caught and named". Voice: "It tests itself with painted defects, on top of the physical challenge strip." |
| 0:35-0:40 | QA tab: "Line clearance", then the batch fields (the batch number, MFG, EXP, MRP) and "Start batch" | Screen: "Line clearance: clear"; the batch bar at the top shows the batch. |
| 0:40-0:50 | QA tab: the start-up strip, foil side up, "Check print (foil side up)" | Screen: the print read from the foil matches the batch record. Voice: "It reads the foil print and compares it with the batch record." |
| 0:50-0:55 | QA types the QA password in the specimen box and presses "Sign start-up specimen (QA)" | Screen: "Start-up specimen signed." The three lines under "Batch release" show "Done": "Self-test for this batch", "Start-up print check", "QA signature on the start-up specimen". |
| 0:55-1:15 | Run tab. The strip with an empty pocket under the camera, "Inspect strip"; the lamp page on a phone turns red | Screen: band "REJECTED" with "Rule 1 failed: pocket 7 is empty" (example) and the severity "major". Voice: "A real strip, rejected, and it says which rule broke." |
| 1:15-1:35 | The strip with a wrong tablet, "Inspect strip" | Screen: band "HELD", the line "Critical: Rule 5 failed: ..." (example) and "Line held — QA must resume". Voice: "A critical defect holds the line." |
| 1:35-2:00 | QA tab: type "self-test failed" into "Why? e.g. rejects rose" and press "Investigate" | Screen: "What the supervisor checked" with the tool calls the agent chose, e.g. image_health, camera_shift; "Written by Muse Spark Open. Check before acting."; a request card with "Approve (QA)" and the QA password field. |
| 2:00-2:15 | QA types the QA password and presses "Approve (QA)"; then the QA password and a reason in "Reason to resume" and "Resume line (QA)" | Screen: "Line resumed". |
| 2:15-2:25 | Run tab: "Voice command", says in English "inspect", with a good strip | Screen: "Heard: inspect — Command: inspect", then the band "PASSED". |
| 2:25-2:35 | Record tab: "Open shift record" | Screen: "DRAFT — needs QA signature", the batch with its release lines, the in-process checks and the reconciliation. |
| 2:35-2:40 | Scroll to the reject photos | Screen: "Reject photos": the annotated photo of the empty-pocket strip with its reason. |
| 2:40-3:00 | QA types the QA password and presses "Sign record (QA)"; then the audit trail in the record | Screen: the audit trail with its chain line. Voice: "Tamper-evident, and QA signs it." |

Notes
- Only the shot right after the title is in Tamil; the rest is English, with English subtitles on the Tamil one.
- Say "packaging-line QA" and "tamper-evident". Give no accuracy number except what `EVAL.md` holds.
- Say "an automatic self-test every shift, on top of the physical challenge strip".
- Check the exact on-screen wording against the running bench before filming; the lines above follow the code but were not filmed.
