# Demo script (3 minutes)

Setup before recording: server deployed (or `make server`), `make sim` with `BEDROCK_MODEL_ID` set, a phone on
the same network for the photo, browser at http://127.0.0.1:8080, "MCP calls this turn" panel open.

| Time | Say / do | What the judges see |
|---|---|---|
| 0:00 | "Families who share a cat lose the details. Cat Nanny is an Alexa+ add-on: an MCP server on AWS. The page is our Alexa+ stand-in; it is a real MCP client." | Title, the architecture line from the README |
| 0:20 | **Alex:** "Alexa, Mochi just threw up." | Alexa: "Logged for Mochi. Was it food, a hairball, liquid, or foam?" Calls panel: `log_observation` → `follow_up`, `assistant_guidance` |
| 0:35 | "It was food." / "I'm not sure." (about blood) | Same `event_id` reused; `unknown_fields: ["has_blood"]`; the record card shows "Not sure" and that question is never asked again |
| 0:55 | "Send me the photo link." Open the link on the phone, take a photo. | Link on the Echo Show, phone uploads straight to S3 (presigned PUT), the photo appears on the record card |
| 1:15 | "No, actually that was Jeremy." | Alexa: "Corrected. This same observation is now recorded for Jeremy, not Mochi. Its details, time and photos were kept." `correct_cat` in the panel |
| 1:30 | Switch to **Sam**: "Alexa, Mochi hasn't eaten since yesterday." | Alexa: "Please call your vet today. ..." first, then the next question. `red_flags[0].rule_id = not_eating_24h` |
| 1:50 | "Show Mochi's summary for the vet." | The MCP Apps view renders on the Echo Show: counts vs. the previous 7 days, timeline with photos. `_meta.ui.resourceUri` in the result |
| 2:15 | Show `make logs` (CloudWatch JSON lines) and the SAM template's resources: Lambda, DynamoDB, S3, Budgets. | "The server is stateless on Lambda; every record is in DynamoDB, every photo in S3, every call logged." |
| 2:40 | "The rules are a separate open-source package, cat-health-signals, with no I/O, so a vet can review them as plain Python." | The repo page |
| 2:55 | Close: "Observations for a vet, not a diagnosis." | |

Offline fallback (no Bedrock): the same lines work with the scripted agent, except free-form phrasing.
