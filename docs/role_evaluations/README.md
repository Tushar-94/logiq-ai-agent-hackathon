# LogIQ — Role Evaluation Reports

> Historical evaluation record: these results may describe a different implementation revision. See the [current test limitations](../../README_full_reference.md#tests); no new live evaluation was performed for this publication copy.

These reports explain how LogIQ was tested, what failed, what changed, and which results were verified.
They are written for teammates who are new to the project.

## Available reports

| Role | Report | Current evaluation status |
|---|---|---|
| Engineer | [Engineer Role Evaluation](engineer.md) | 52 offline tests passed. Live severity and causal-reasoning retests passed. Neighboring-event tool evidence passed; the final written answer from that last retest has not been reviewed. |
| Team Lead | [Team Lead Role Evaluation](team_lead.md) | Initial live evaluation completed. Three problems identified; code investigation, fixes, and retests have not started. |

This directory does not contain a completed Manager evaluation. Engineer and Team Lead results should not be treated as proof that every other role or external client has been evaluated.

## How to read the results

| Label | Meaning |
|---|---|
| **PASS** | The stated check has supporting test evidence. Its scope matters. |
| **FAIL** | The observed behavior did not meet the expected behavior. |
| **NEEDS IMPROVEMENT** | The answer needs better reasoning or wording, as described in the report. |
| **NOT YET FIXED** | A known problem remains open; no completed fix is recorded. |
| **PARTIALLY VERIFIED** | Some checks passed, but a required part remains unreviewed. |
| **NOT VERIFIED / NOT YET REVIEWED** | No completed verification is recorded for that behavior or answer. This is not the same as a demonstrated failure. |

The Engineer report separates three kinds of evidence:

1. **Code inspection:** what the current implementation is designed to do.
2. **Offline tests:** checks using small, simulated data and mocked database connections.
3. **Live retests:** checks against the running application and existing log data, followed by a separate review of the AI's written answer.

The Team Lead report records initial live observations, suspected causes, and what is not yet verified. Its planned investigation steps are not completed work.

A correct tool result does not automatically mean the final AI explanation is correct.

## Documentation ownership

[engineer.md](engineer.md) is the canonical Engineer evaluation. The legacy singular `role_evaluation/` redirect directory is not included in this publication; use this canonical report.

[team_lead.md](team_lead.md) is the canonical Team Lead evaluation. It records the initial findings without duplicating the Engineer report.

For installation and general usage, see the [project README](../../README.md). For the implementation discussed in these reports, see [agent.py](../../agent.py), [tools.py](../../tools.py), and the [offline tests](../../tests/test_severity_filters.py).

Updating an evaluation report does not require running loaders, changing the database, changing credentials, or restarting the server.
