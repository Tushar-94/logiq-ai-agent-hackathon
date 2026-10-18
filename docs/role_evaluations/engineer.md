# LogIQ — Engineer Role Evaluation

> Historical evaluation record: these results may describe a different implementation revision. See the [current test limitations](../../README_full_reference.md#tests); no new live evaluation was performed for this publication copy.

**Context:** National AI Hackathon 2026.
**Role evaluated:** Engineer.
**Recorded automated result:** **52 passed, 0 failed.**
**Current review boundary:** The final written answer from the last neighboring-events retest has **not** been reviewed.

## 1. Executive Summary

LogIQ helps people investigate large collections of system logs. A log line is one recorded message from a system. Instead of sending millions of messages to an AI model, LogIQ asks SereneDB to search and count them. The AI chooses tools and explains the returned evidence.

We tested the Engineer role because an engineer needs more than a plausible explanation. They need the correct errors, exact counts, source information, timestamps, and a clear distinction between observations and possible causes.

The initial evaluation found four problems. The AI used an unsupported tool argument, ranked mixed-severity messages as critical errors, overinterpreted neighboring messages, and suggested a failure cascade without enough evidence.

All four fixes have been implemented. Automated checks pass. Live severity and causal-reasoning checks passed. The final neighboring-events tool evidence also passed, but its last written AI answer remains unreviewed.

| Problem | Initial Status | Fix | Retest Status |
|---|---|---|---|
| 1. Incorrect tool arguments and severity text search | **FAIL** | Structured `level` and `severity` filters; clearer tool definitions and Engineer instructions | **PASS** for recorded tool and live severity checks |
| 2. Mixed-severity ranking described as critical-only | **FAIL** | Apply `severity=3` before counting and ranking; request the stated number of results | **PASS**; five verified templates with exact filtered counts |
| 3. Misleading neighboring events | **FAIL** in precision/reference handling; interpretation was unreliable | Preserve fractional seconds, show signed gaps and source comparison, explain log order, retain the target under truncation | **PARTIALLY VERIFIED** overall: tool evidence **PASS**; last written answer **NOT YET REVIEWED** |
| 4. Unsupported failure cascades | **FAIL** | Bound local context; separate preceding observations from supported causal leads; expose association evidence | **PASS** for the recorded line-17742 causal-reasoning retest; no established cause was claimed |

### Terms used in this report

| Term | Simple explanation |
|---|---|
| Severity | A number representing how serious a message's recorded level is. It is not a measure of business impact. |
| Level | The original label, such as `FATAL`, `ERROR`, or `INFO`. |
| Template | A group of messages with the same general pattern. Variable values may be replaced with `<*>`. |
| Source | Where a message came from. Here it is a BGL node identifier or an OpenStack log-record/file identifier. It is not automatically a unique job or process. |
| Component | The part of the system that logged a message, such as `KERNEL` or `APP`. |
| Owning team | The team mapped to a component by the project's ownership table. |
| Causality | Evidence that one event actually contributed to another. Events appearing together is not enough. |
| Hypothesis | A possible explanation that still needs confirmation. |
| Failure cascade | A sequence in which one failure contributes to additional failures. A list of errors is not automatically a cascade. |
| SQL | The query language used to retrieve and count database records. |
| Evidence envelope | The tool's result package: data, scope, filters, SQL, parameters, row counts, and warnings. |
| Truncation | Only part of a result list is returned. An exact total may still cover every matching line. |

## 2. System Architecture

### Web application path

```text
User Question
    ↓
FastAPI Application (app.py)
    ↓
AI Agent (agent.py)
    ↓
Tool Functions (tools.py)
    ↓
SQL Queries
    ↓
SereneDB
    ↓
Evidence + AI Answer
```

FastAPI is the Python framework serving the web application. The flow can repeat: the AI may request another tool after reading a result.

1. The browser sends the question and selected role to `/api/ask` in `app.py`.
2. The application creates or reuses an in-memory `agent.Session`.
3. `agent.py` supplies the role instructions, conversation history, and published tool definitions to the model.
4. The model selects a tool name and its arguments. The Python tool, rather than the model, constructs the executed SQL.
5. `tools.py` runs parameterized queries. Parameters are values supplied separately from the SQL, such as `"bgl"` or `3`.
6. The tool returns an evidence envelope. `agent._execute()` adds a conversation-local identifier such as `Q1`.
7. The application streams tool calls, results, and the final answer to the browser. The evidence trail lets a reader inspect the SQL.

The model is instructed to cite evidence and separate observations from hypotheses. These instructions guide its behavior; they are not a guarantee that every generated sentence is correct.

### File responsibilities

| File | Responsibility |
|---|---|
| [app.py](../../app.py) | Serves the web interface, manages sessions, streams `/api/ask`, and exposes direct tool requests through `/api/tool/{name}`. |
| [agent.py](../../agent.py) | Builds role instructions, calls the configured model, executes requested tools, numbers evidence, and returns answer events. |
| [tools.py](../../tools.py) | Defines supported arguments, validation, fixed query construction, time/source comparisons, and evidence envelopes. |
| [mcp_server.py](../../mcp_server.py) | Registers the shared tools for external MCP clients. |
| [static/index.html](../../static/index.html) | Displays the answer, tool inputs, SQL, results, and warnings in the browser. |
| [tests/test_severity_filters.py](../../tests/test_severity_filters.py) | Contains the offline severity, investigation, and neighbor-interpretation regressions. |
| [sql/schema.sql](../../sql/schema.sql) | Describes the existing shared log fields, numeric severity mapping, and ownership views. It was inspected, not executed, for this report. |
| [agent_tools.py](../../agent_tools.py) | A separate command-line inspection utility. It is not the tool layer called by the web Engineer agent. |

### MCP is a separate execution path

MCP, or Model Context Protocol, lets an external AI client discover and call tools. `mcp_server.py` registers functions from `tools.TOOLS` and supplies tool descriptions and server instructions.

An external MCP client does **not** automatically run through `app.py` or receive `agent.py`'s Engineer prompt. The client controls its own conversation and reasoning. Shared tool behavior is tested, but a successful web answer does not prove every external MCP client's answer is correct.

## 3. Test Environment

### Dataset facts

These describe the loaded evaluation corpus. They are not measures of AI quality.

| Dataset | System represented | Log lines | Recorded date range |
|---|---|---:|---|
| BGL | Supercomputer logs | 4,747,963 | 2005-06-03 to 2006-01-04 |
| OpenStack | Cloud-system logs | 207,820 | 2017-05-14 to 2017-05-17 |
| **Full corpus** | Both datasets | **4,955,783** | Two different periods; not one shared incident timeline |

The evaluated questions focused on BGL in the Engineer role. OpenStack remains part of the corpus and shared tool API. This is not a separate full OpenStack evaluation.

### Severity mapping

The mapping is defined in `sql/schema.sql` and explained in the current tool definitions:

| Numeric severity | Recorded levels |
|---:|---|
| 3 | `FATAL`, `SEVERE`, `FAILURE`, `CRITICAL` |
| 2 | `ERROR` |
| 1 | `WARNING`, `WARN` |
| 0 | Other levels, commonly `INFO` |

An explicit `level="FATAL"` request selects that level only. A `severity=3` request includes all four labels mapped to category 3 when they exist in the selected dataset.

### Questions and tools

| Question or check | Purpose | Main tools |
|---|---|---|
| Original critical-error investigation, quoted in Section 4 | Evaluate ranking, ownership, context, explanations, and debugging advice together | `count_problems`, `search_logs`, `investigate_event` |
| Explicit top-five severity-3 question, quoted in Test A | Isolate ranking and exact filtered counts | `count_problems` |
| “Investigate BGL line 17742.” | Evaluate local context and causal reasoning | `investigate_event` |
| Neighbor evidence check for BGL line 17742 | Check timestamps, source relationships, and matching templates | `get_nearby_events`, `investigate_event` |
| Offline fixture checks | Exercise specific edge cases without the real database | Shared functions with mocked connections |

The supplied neighbor-retest record identifies the target and results, but does not contain its full final prompt or written answer. This report does not invent that missing transcript.

### Evidence references and their limits

| Reference used here | Evidence available | What it supports |
|---|---|---|
| Original evaluation record | Earlier Engineer draft, consolidated into this report | Initial question, reported E19 findings, and the four observed problems |
| Live Test A | Recorded direct database checks and final live ranking answer, including its `Q1` envelope | Exact severity-3 counts, correct tool arguments, SQL parameters, concise answer, and citations |
| Live Test B | Recorded line-17742 findings and the team's confirmation that the causal-reasoning retest passed | Target details, bounded local context, unrelated predecessor, absent supported candidate, and nearby matching event |
| Final neighbor evidence retest | Team-confirmed tool verification | Correct neighbor evidence; **not** a reviewed final written answer |
| Current source and offline tests | Files linked in this report | Implemented behavior and the scope of automated checks |

`[Q1]` is an evidence identifier inside one conversation. It is not a permanent database record ID or a citation shared by every run. No `Q` number was supplied for the last neighbor retest, so none is assigned here.

SQL excerpts below are copied from current query construction. Where the raw live envelope was not supplied, they are labelled implementation excerpts rather than presented as a complete captured live transcript.

## 4. Original Engineer Test

### Original question

> Investigate the most frequent critical errors in the BGL dataset. Identify the affected components, show the events immediately before and after the errors, explain possible causes, and recommend specific debugging steps. Include SQL evidence.

### Expected behavior

1. Explain what “critical” means and filter to the intended severity before ranking.
2. Count all matching log lines, rather than counting a small displayed sample.
3. Identify each template, affected components, and owning teams.
4. Show key messages and their recorded timestamps and sources.
5. Distinguish file order from timestamp order, including ties.
6. Treat explanations as hypotheses unless there is evidence linking events.
7. Suggest concrete checks that could confirm or rule out those hypotheses.
8. Include SQL evidence and state what remains unknown.

### What LogIQ returned

The original evaluation identified E19, `data TLB error interrupt`, with **152,734 matching lines**, component **KERNEL**, owning team **compute-kernel**, and a representative **FATAL** message.

The earlier draft also recorded **541 sources** and **11 active days** for that finding. These are preserved as historical evaluation observations, not newly rechecked measurements from this documentation task.

The useful parts were full-corpus counting, a recognizable message pattern, team ownership, real log excerpts, and suggested diagnostic directions. However, the query did not establish a critical-only ranking, and parts of the surrounding-event interpretation were unsupported.

For example, same-source messages from June 14 were discussed alongside a June 5 error. Nearby records from different sources were also treated too strongly as a sequence.

**Initial result: FAIL for the complete requested investigation.** Some retrieved facts were useful, but the severity scope and causal conclusions were not adequately supported.

## 5. Four Original Failures and Their Fixes

The improvement numbers below follow the **original four problems**. Implementation happened in stages: severity work addressed Problems 1 and 2 together; temporal and causal work addressed Problem 4; the final neighbor audit completed Problem 3.

### Improvement 1 — Incorrect Tool Arguments and Severity Text Search

#### A. What were we trying to achieve?

Allow the Engineer to request FATAL records using a real level filter. Valid calls should match the tool's documented inputs.

#### B. What actually happened?

The AI attempted this originally unsupported call:

```python
count_problems(dataset="bgl", group_by="template", level="FATAL")
```

The original evaluation recorded the error as:

```text
TypeError: unexpected keyword argument 'level'
```

It also tried `search_logs(text="FATAL")`. The original test reported zero text matches. That call searched message content, not the level field. A FATAL message can say only `data TLB error interrupt`, without the word “FATAL.”

A text search for “FATAL” is not always empty: it can also match an INFO message that merely mentions that word. The offline tests deliberately include this counterexample.

#### C. Where did the failure originate?

| Location | Relevant behavior | Effect |
|---|---|---|
| `tools.py` → original `count_problems()` and `TOOLS` entry | Neither the function signature nor its published input definition supported `level` | Python rejected the supplied keyword argument |
| `tools.py` → original `search_logs()` | Required text and searched `content` through the full-text index | A severity word was treated as message text |
| `agent.py` → Engineer instructions | Did not explain the distinction strongly enough or provide a clean ranking-first path | The model chose an invalid argument and then an unsuitable search |

The live follow-up exposed another layer: the disk files had been corrected, but the running web process still advertised the old arguments. That stale-process incident is documented in Section 6.

#### D. Why was this a problem?

The engineer could receive an error or incorrectly conclude that no FATAL records existed. Repeated recovery attempts also made the investigation slower and more expensive.

#### E. Root cause

| Type | Explanation |
|---|---|
| Coding/API problem | The tool lacked the filter the question required. An API is the set of supported inputs and outputs. |
| AI instruction problem | Message text and severity metadata were not clearly separated in tool selection. |
| Data/query problem | Searching `content` does not select a value stored in `level`. The log data was not missing. |
| Evidence interpretation problem | Zero text matches were not evidence of zero FATAL records. |

#### F. How did we fix it?

In `tools.py`, `count_problems()` gained optional `level` and `severity` arguments. `_severity_filters()` applies the structured predicates. Numeric severity must be an integer from 0 to 3; invalid types and values are rejected.

`search_logs()` now allows omitted or empty text when a level or severity filter is supplied. Without text, it reads structured records in time order, does not use full-text matching or BM25, and returns `score: null`. BM25 is the relevance score used for actual text searches.

The `TOOLS` definitions advertise the inputs. `claude_tools()`, the compatible API definitions in `agent.py`, and MCP registration use that shared registry or the function signatures.

`agent.py` now explicitly distinguishes `text`, `level`, and numeric `severity`.

```python
# Message search: find these words in content.
search_logs(text="data TLB error interrupt", dataset="bgl")

# Structured selection: the message need not contain "FATAL".
search_logs(dataset="bgl", level="FATAL")
```

If both `level` and `severity` are supplied, both must match. Existing text searches and positional argument order were preserved.

#### G. How did we test the fix?

**Test question:** the original question in Section 4 exercised the failure. The explicit top-five question in Test A isolated the corrected severity path.

**Expected:** valid structured arguments; FATAL selection independent of message wording; no BM25 for severity-only retrieval.

**Actual recorded result:** direct updated-code and restarted-server checks found **855,501 BGL FATAL lines** with empty-text structured retrieval. The old empty full-text query returned zero. This demonstrates a query-path problem rather than absent data.

Automated examples include:

| Fixture | Expected result | Test evidence |
|---|---|---|
| FATAL message: `data TLB error interrupt` | Included by `level="FATAL"` | `test_level_filters_structured_field_not_content` |
| INFO message: `FATAL is the configured threshold` | Excluded by the FATAL filter; found by the text search | `test_text_search_still_finds_info_containing_fatal`, `test_count_level_excludes_info_containing_fatal` |
| Empty text with a FATAL level | Structured retrieval, without `@@` or BM25 | `test_empty_text_with_level_uses_structured_retrieval` |
| Invalid numeric severity | Rejected before a connection is made | `test_invalid_severity_rejected_before_connection` |

The structured SQL predicate is `upper(level) = upper(%s)`, with `"FATAL"` supplied as a parameter.

#### H. Before vs After

| Before | After | Why It Is Better |
|---|---|---|
| `count_problems(level="FATAL")` failed | The argument is supported and published | The requested filter can actually be applied |
| “FATAL” was searched as message text | Level is compared with the structured field | Message wording cannot substitute for severity |
| Text was mandatory | Structured-only retrieval is supported | No fake or empty full-text query is needed |

#### I. Final status

**PASS** for recorded automated checks, direct tool verification, and the successful live severity retest. This does not guarantee that every future model response will choose the correct tool without review.

### Improvement 2 — Incorrect Severity Ranking

#### A. What were we trying to achieve?

Rank the most frequent highest-severity BGL templates using **only severity-3 lines**, with exact counts and the requested number of results.

#### B. What actually happened?

The original ranking used:

```python
count_problems(dataset="bgl", group_by="template", problems_only=True)
```

The AI called E19 the most frequent critical error, although the query ranked a broader collection of problem lines. A later stale-server run again omitted `severity=3`, requested 20 and then 100 groups, and included lower-severity templates.

E19 eventually proved to be first in the corrected ranking too. That does not make the original reasoning valid: the earlier query did not establish the claimed ranking scope.

#### C. Where did the failure originate?

`tools.py` → `_filters()` defines problem lines as `severity >= 1 OR COALESCE(is_alert, false)`. `count_problems()` orders those grouped line counts from highest to lowest.

That definition deliberately includes warnings, errors, severity-3 messages, and labelled alerts. The returned `max_severity` describes the highest value present in a group; it does not mean every counted line has that value.

`agent.py` also needed a clear distinction between a simple ranking question and an investigation asking about causes or surrounding events.

#### D. Why was this a problem?

An engineer deciding which critical problem to inspect could instead be looking at a template made frequent by warnings. A count can be mathematically correct while answering the wrong question.

#### E. Root cause

| Type | Explanation |
|---|---|
| Coding/API problem | The original aggregation lacked exact severity filtering. |
| AI instruction problem | The model did not consistently request the intended category or the requested top-five limit. |
| Data/query problem | `severity >= 1` and `severity = 3` select different populations. A labelled INFO alert can also be a problem line. |
| Evidence interpretation problem | A mixed-severity count or `max_severity=3` was presented as a critical-only count. |

#### F. How did we fix it?

`tools.py` → `count_problems()` applies `_severity_filters()` to the grouped query and the totals query. Percentages use that same filtered total. Filters also appear in the evidence metadata and scope.

The corrected call is:

```python
count_problems(dataset="bgl", group_by="template", severity=3, limit=5)
```

The executed query includes this filter excerpt:

```sql
WHERE TRUE
  AND (severity >= 1 OR COALESCE(is_alert, false))
  AND dataset = %s
  AND severity = %s
```

The values are `"bgl"` and `3`. The additional exact predicate prevents lower severities from entering this ranking, even though the original problem-only condition remains for compatibility.

Template results also include all filtered `components` and `teams`, reducing the need for extra history calls. The legacy representative `team` field remains available.

The Engineer prompt now maps “critical” and “highest-severity” to category 3, requires that interpretation to be stated, and directs a simple top-N ranking to one successful aggregation.

#### G. How did we test the fix?

**Exact live question:**

> Find the five most frequent highest-severity errors in the BGL dataset. Use severity 3 (FATAL, SEVERE, FAILURE, CRITICAL), show the exact number of matching log lines for each error template, identify the affected components and responsible teams, and include SQL evidence. Do not count lower-severity messages.

**Expected:** `severity=3`, `limit=5`, five exact template counts, owners, and SQL evidence; no lower-severity messages.

**Actual verified ranking:**

| Template | Matching severity-3 log lines |
|---|---:|
| E19 | 152,734 |
| E146 | 63,491 |
| E20 | 47,563 |
| E10 | 45,824 |
| E421 | 42,528 |

**These are matching log lines, not independent incidents.**

The final successful live run used one `count_problems` call and two model calls. Its Q1 evidence contained the filtered SQL and actual parameters. Section 8 gives the full result table and explains the internal extra-row limit.

Automated coverage includes `test_mixed_template_counts_only_matching_severity`, `test_count_queries_and_metadata_use_identical_filters`, `test_search_totals_and_rows_use_identical_filters`, and `test_truncation_keeps_full_filtered_totals_and_shares`.

The mixed-template fixture gives E19 one FATAL, one WARNING, and one ERROR line. A severity-3 count must return **one**, not three, for that template.

#### H. Before vs After

| Before | After | Why It Is Better |
|---|---|---|
| Mixed problem lines ranked as critical-only | Exact `severity=3` predicate before aggregation | Count scope matches the question |
| Larger group requests and unrelated history calls | Requested `limit=5` and one successful ranking call in the verified run | The answer gathers only the evidence it needs |
| Group severity summary interpreted as a filter | Counts and totals share the actual filter | Mixed templates cannot inflate critical counts |

#### I. Final status

**PASS.** Automated filtering checks, direct database verification, and the final live ranking answer agree on the five counts. This is a recorded successful run, not a guarantee of a fixed call count for every future question.

### Improvement 3 — Misleading Neighboring Events

#### A. What were we trying to achieve?

Show what is near a target in the log while making clear whether it is actually earlier, later, simultaneous at recorded precision, or from another source.

#### B. What actually happened?

Neighboring records could be treated as one sequence even when they came from different sources. Whole-second formatting could hide real fractional differences. File position could be confused with timestamp order.

The real target and one nearby event are:

| Line | Timestamp | Template | Source relationship |
|---:|---|---|---|
| 17742 | `2005-06-05 00:08:13.410695` | E19 | Target source: `R20-M0-N2-C:J10-U11` |
| 17745 | `2005-06-05 00:08:13.577322` | E19 | Different source; its full identifier is not reproduced in the supplied record |

The difference is **0.166627 seconds**, approximately **0.167 seconds later**. At whole-second display precision, both appear to be `00:08:13`. They are not identical timestamps when the supplied fractional values are retained.

True timestamp ties were tested separately. A tie does not establish which event happened first.

The final audit also demonstrated that a busy time window could return 60 earlier records, omit the target from the displayed sample, and incorrectly report that target as missing.

#### C. Where did the failure originate?

| Location | Relevant code behavior before the neighbor fix | Consequence |
|---|---|---|
| `tools.py` → `get_nearby_events()` | Used ordinary second-level serialization and converted the time gap to `int` | Fractional gaps could become zero or lose precision |
| Same function → `position` | Assigned `before` or `after` using `line_id` | Position could be mistaken for chronological order without an explicit explanation |
| Same function → target lookup after a limited query | Looked for the target only inside returned rows | A truncated sample could lose the reference timestamp and trigger a false missing-target note |
| Same function → source output | Returned source IDs but lacked the investigation tool's explicit comparison flag | The AI had to infer the relationship itself |
| `agent.py` → Engineer instructions | Already warned about source differences, but needed to explain `get_nearby_events.position` explicitly | A model could still overread file adjacency |

`investigate_event()` already handled the new time/source matrix correctly at the final audit. Its established candidate and time-window logic was left unchanged during this improvement.

#### D. Why was this a problem?

An engineer could investigate the wrong sequence, infer a causal link between unrelated machines, or miss a real subsecond pattern. A falsely “missing” target also weakens trust in an otherwise valid query result.

#### E. Root cause

| Type | Explanation |
|---|---|
| Coding/API problem | Precision and target-reference handling in `get_nearby_events()` were incomplete. Existing API arguments did not need replacement. |
| AI instruction problem | File position needed an explicit interpretation separate from timestamp order. |
| Data/query problem | A global log can interleave different sources. A limited sample may exclude the target even when the target exists. |
| Evidence interpretation problem | Adjacency, equal displayed seconds, or the same template was treated as stronger evidence than it was. |

#### F. How did we fix it?

`get_nearby_events()` now uses the existing precision-preserving `Evidence` option and returns the fractional result of `.total_seconds()` without integer conversion.

In time-window mode it retains the independently fetched target, even if the later sample omits that row. It adds `same_source_as_target` and clear notes about ordering and causality. `agent.py` explains the same rules to the Engineer model.

| Field | Meaning | Example |
|---|---|---|
| `position` | `before`, `target`, or `after` **in line-ID order** | A lower line ID is `before`, even if its timestamp is later |
| `seconds_from_target` | Neighbor timestamp minus target timestamp | `-300` is five minutes earlier; `0` is tied; `0.166627` is later |
| `same_source_as_target` | Whether the recorded source matches a known target source | `false` for the different-source E19 neighbor |

Existing result keys, tool arguments, SQL evidence, and the meaning of `position` were preserved. The new source flag adds an explicit comparison rather than replacing the original source IDs.

#### G. How did we test the fix?

**Live target/check:** investigate BGL line 17742 and verify neighboring-event evidence. The full wording of the last neighbor retest was not supplied.

**Expected:** retain the fractional timestamp difference, show the source difference, and do not turn a matching template into a shared-cause claim.

**Actual live tool result:** the team confirmed that line-17742 evidence verification passed. Line 17745 is another E19 FATAL message about 0.167 seconds later on another source.

**Final written AI answer:** **NOT YET REVIEWED.** Tool verification is not a substitute for reviewing that explanation.

Nine new offline regressions checked:

1. True timestamp ties on both sides of a target in line order.
2. Lower line ID with a later timestamp, and higher line ID with an earlier timestamp.
3. Explicit same-source and different-source comparisons without causal claims.
4. Matching templates at fractional gaps on different sources.
5. Nine-day-distant line-order neighbors with actual signed gaps and warnings.
6. A time-window sample that truncates out the target.
7. Existing arguments and Q1 evidence shape.
8. Time-window same-source filtering and a genuinely missing target.
9. Engineer instructions explaining neighbor order and sources.

The tests are in `NeighborInterpretationTests`. They run against mocked connections, not SereneDB.

#### H. Before vs After

| Before | After | Why It Is Better |
|---|---|---|
| Fractional difference could become zero | Actual signed fractional seconds are returned | Millisecond relationships stay visible |
| `before` could sound like “earlier in time” | `position` is explicitly log order | File position cannot silently stand in for chronology |
| Different source IDs required manual comparison | IDs plus `same_source_as_target` | Source relationships are easier to check |
| Truncated sample could lose the target reference | Independently fetched target is retained | Time gaps remain available and no false missing-target note is added |
| Generic neighbor caution | Explicit order, distance, tie, and causality notes | The reader knows exactly what adjacency does and does not establish |

#### I. Final status

**PARTIALLY VERIFIED overall.** Automated regressions **PASS** and final live tool evidence **PASS**. The final written AI answer from the last neighboring-events retest is **NOT YET REVIEWED**. No failure of that unseen answer is asserted either.

### Improvement 4 — Unsupported Failure Cascades

#### A. What were we trying to achieve?

Find useful local evidence and possible causal leads without inventing a relationship between errors that merely share a source or occur near each other.

#### B. What actually happened?

The original investigation associated a June 5 TLB error with June 14 messages on the same source, including data storage interrupts, instruction-address errors, and machine checks. The roughly nine-day gap did not establish a local failure cascade.

After the first time-window fix, another live retest still exposed a reasoning problem:

| Event | Recorded observation |
|---|---|
| Target line 17742 | E19, KERNEL, FATAL |
| Preceding line 17466 | E10, APP, FATAL; missing-program-image error; different source |
| Time difference | 1,055.35 seconds earlier, about 17.6 minutes |
| Initial explanation | The APP error might have caused the KERNEL TLB interrupt through malformed execution |
| Missing evidence | No shared job, process, or request ID was established |

The time limit was working in that retest. The error was promoting an unrelated predecessor to `candidate_cause` and then inventing a mechanism between the messages.

#### C. Where did the failure originate?

`tools.py` → original `investigate_event()` retrieved other same-source problems from the whole dataset, without a local time bound. Its original candidate heuristic used surrounding line IDs and allowed same-source candidates to bypass the one-hour gap check.

The first temporal revision corrected the timestamp rules but still selected an earlier different-source problem as a fallback candidate. That label encouraged the AI to treat a contextual observation as a possible cause.

`agent.py` needed an explicit rule against connecting unrelated errors through an invented mechanism. A generic “hypothesis” label was not enough.

#### D. Why was this a problem?

The engineer could spend time checking an APP startup problem instead of investigating the KERNEL error on the target node. Calling an unsupported mechanism a hypothesis does not make it evidence-based.

#### E. Root cause

| Type | Explanation |
|---|---|
| Coding/API problem | Unbounded same-source history and the old fallback populated a field named `candidate_cause` too freely. |
| AI instruction problem | The model was not explicitly prevented from inventing a bridge between unrelated components. |
| Data/query problem | Same source across nine days, or different source within 17.6 minutes, does not establish a shared operation. |
| Evidence interpretation problem | Recurrence and temporal association were presented as support for a cascade. |

#### F. How did we fix it?

The fixes are in `tools.py` → `investigate_event()`, `_event_links()`, the published tool description, and the Engineer prompt in `agent.py`.

| Field or rule | Current behavior |
|---|---|
| `same_source_minutes=60` | Default local same-source context is target time ±60 minutes. Smaller positive windows are allowed, up to 60. |
| Strict candidate timing | A preceding observation or causal lead must be more than zero and no more than 3,600 seconds earlier. Timestamp order, not line ID, determines eligibility. |
| `preceding_event` | An observed earlier problem, with same-source observations preferred. It is not automatically a causal candidate. |
| `candidate_cause` | A lead only when there is a same-source association or an exact supported shared identifier. It may be `null`. |
| `candidate_relationship` | States the association basis and its caveat. It does not assert a mechanism. |
| Explicit shared identifiers | Conservative matching of labelled `request_id`, `job_id`, `trace_id`, or `correlation_id` values in the original messages. |
| `nearby_same_template_events` | Matching messages from returned local context, including events after the target. These are observations, not proof of a common cause. |
| `recurrence_in_dataset` and `recurrence_scope` | Historical whole-dataset recurrence, clearly separate from the local window. |

Same-source candidates remain leads for investigation, not proof. Matching bare process IDs on different sources is not enough: such numbers can be reused. Supported identifier names and exact values are checked, so partial IDs or different identifier fields do not count as a link.

The distinction can be expressed simply:

```text
Earlier event + time proximity only → preceding_event
Earlier event + same source or supported explicit link → possible lead
Possible lead → still not an established cause or cascade
```

The tool returns actual signed time differences, and a positive `seconds_before_target` for earlier observations and leads. When no supported candidate is found, the Engineer is instructed to say: “No supported causal candidate has been identified.”

#### G. How did we test the fix?

**Exact recorded live question:** “Investigate BGL line 17742.”

**Expected:** bound local same-source context; keep line 17466 as an observation; do not infer APP-to-KERNEL causation without a link; highlight the nearby E19 message without claiming a common cause.

**Actual successful retest:** no same-source problems were found within ±60 minutes. Line 17466 remained contextual evidence. No supported causal candidate was identified. The later same-template event remained visible, with no established causal relationship. The team confirmed that this causal-reasoning live investigation passed.

Automated coverage includes nine-day gaps, five-minute same-source leads, strictly earlier timestamps, the inclusive one-hour boundary, linked different-source events, rejection of partial IDs and placeholder values, later same-template observations, and absent predecessors.

`test_live_17742_scenario_and_evidence_citation_regression` reproduces the reported relationships with a small fixture and checks Q1 evidence. Despite its name, it is an **offline simulation**, not a live database test.

The governing implementation predicates are:

```sql
WHERE dataset = %s AND is_problem
  AND ts >= %s AND ts < %s
```

The bounds are target time minus one hour, and target time. Causal eligibility additionally requires the association rule; the time predicate alone is insufficient.

#### H. Before vs After

| Before | After | Why It Is Better |
|---|---|---|
| Nine-day-separated same-source history suggested a cascade | Bounded local context; history labelled separately | Long-term recurrence is not presented as an immediate sequence |
| Any suitable-time predecessor could become a causal candidate | Unrelated predecessor stays in `preceding_event` | Context remains visible without receiving a causal label |
| APP-to-KERNEL mechanism invented from timing | No supported causal candidate identified | The answer reflects missing evidence instead of filling the gap |
| Later matching messages could be overread | Later same-template observations are highlighted with caveats | A useful pattern is visible without claiming propagation |

#### I. Final status

**PASS** for the recorded offline checks and the team's successful causal-reasoning live retest of line 17742. **The cause itself remains unknown.** This pass must not be confused with review of the later neighboring-events written answer, which remains pending.

## 6. Testing History

This is the sequence of evaluation stages, not a new set of tests run while writing the report.

| Stage | What Was Tested | Result | What Changed |
|---|---|---|---|
| 1. Initial Engineer evaluation | Broad critical-error investigation | **FAIL** for the complete question; useful retrieved facts but four reasoning/API problems | Read-only audit identified the required fixes |
| 2. First severity implementation | Structured filters, compatibility, counts, schemas | **PASS:** 18 offline tests | Added level/severity filtering and structured-only retrieval |
| 3. Live severity test against stale server | Explicit severity-3 ranking through the browser | **FAIL:** old schema, missing severity filter, excessive calls, incomplete answer | Investigated the running process before replacing it |
| 4. Successful severity retest | Correct top-five ranking, owners, exact SQL and parameters | **PASS:** final run used one tool call and two model calls; 22 offline tests passed at this stage | Loaded new code; clarified ranking format, citations, and tool limit versus SQL lookahead |
| 5. Time-window implementation | Local same-source context and strict preceding timestamps | **PASS:** 37 offline tests; live temporal filtering passed | Added bounded context, measured gaps, precision, and historical labels |
| 6. First causal-reasoning retest | Interpretation of line 17466 relative to line 17742 | **FAIL** on causal interpretation, despite working time filters | Found that an unrelated APP predecessor was still labelled a candidate |
| 7. Causal-reasoning refinement | Context versus supported association, exact identifiers, later matching events | **PASS:** 43 offline tests | Added `preceding_event`, `candidate_relationship`, and conservative linkage rules |
| 8. Successful causal-reasoning retest | Line-17742 investigation | **PASS**, as confirmed in the live evaluation record | Unrelated predecessor stayed contextual; no supported cause was asserted |
| 9. Neighboring-event regression tests | Ties, reversed line/time order, sources, milliseconds, distance, truncation | New checks first exposed nearby-tool gaps; **PASS: 52 tests** after fixes | Changed only the demonstrated nearby-tool gaps and clarified instructions |
| 10. Successful neighboring-event evidence test | Live tool evidence for line 17742 | **PASS** for evidence; written answer **NOT YET REVIEWED** | Preserved this distinction in the final evaluation status |

### The stale-server incident

The files on disk had been updated, but the FastAPI process had started before those edits and was not running with automatic reload. The browser therefore reached old tool definitions and old behavior.

The team identified this by:

1. Checking the listener on port 8000 and confirming its command and project working directory.
2. Comparing the process start time with the edited files.
3. Inspecting `/api/meta`: the running `count_problems` definition had no severity argument, and `search_logs` still required text.
4. Reproducing rejection of the new argument through the old server while the updated functions worked directly.
5. Reproducing the old empty full-text query's zero matches, rather than assuming the database contained no FATAL lines.

The existing safe restart procedure gracefully stopped the verified project process, checked that the port was free, and started one replacement. Subsequent HTTP checks matched the direct tool results. The final approved Improvement-3 restart also returned HTTP 200 with one listener; that is an application-availability check, not proof of answer correctness.

For scale, the failed severity run recorded **15 tool calls**, **12 model calls**, **250,317 input tokens**, and **22,140 output tokens**, with an answer cut off at its output limit. Tokens are the units the model reads and generates.

The final successful severity retry recorded **1 tool call**, **2 model calls**, **13,383 input tokens**, and **869 output tokens**, with a completed answer. These describe particular runs, not a performance guarantee or a controlled benchmark.

## 7. Automated Tests

### Recorded final result

```text
Ran 52 tests
OK
52 passed, 0 failed
```

The suite contains 22 `SeverityFilterTests`, 21 `InvestigationTimingTests`, and 9 `NeighborInterpretationTests`.

All database connections in these tests are mocked. A small in-memory SQLite fixture evaluates ordinary filtering, grouping, ordering, and limits. SereneDB-specific full-text matching and scoring are stubbed. The tests do not reload or query the real corpus.

| Test group | What is checked | Example actual test names |
|---|---|---|
| Severity filtering | Structured level matching, exact numeric categories, mixed templates, invalid values, matching grouped and total filters | `test_level_filters_structured_field_not_content`; `test_mixed_template_counts_only_matching_severity` |
| Tool compatibility | Existing positional calls, published input names, compatible-provider schemas, MCP discovery, evidence fields | `test_existing_positional_calls_and_problem_defaults`; `test_mcp_advertises_new_optional_arguments` |
| Time-window validation | Positive finite local windows up to 60 minutes, nine-day exclusion, strict preceding bounds, one-hour boundary | `test_invalid_local_window_rejected_without_database_connection`; `test_one_hour_boundary_included_and_older_excluded_on_all_sources` |
| Candidate-cause reasoning | Unrelated predecessor remains context; same-source lead; exact linked different-source event; missing candidates | `test_different_source_preceding_event_is_context_not_causal_candidate`; `test_different_source_explicit_link_is_supported_without_inventing_mechanism` |
| Neighboring-event interpretation | Line order versus timestamp order, ties, distant neighbors, actual signed gaps, instruction contract | `test_lower_line_id_can_be_later_and_higher_line_id_can_be_earlier`; `test_identical_timestamps_do_not_imply_chronological_order` |
| Timestamp precision | Fractional candidate gaps and millisecond matching-template context | `test_subsecond_order_and_actual_gaps_are_preserved`; `test_millisecond_same_template_different_source_keeps_actual_gap` |
| Source relationships | Explicit same/different-source comparison; unknown source not treated as a match | `test_source_relationship_is_explicit_without_causal_claim`; `test_unknown_source_is_not_treated_as_same_source` |
| Truncation handling | Full filtered totals, correct percentages, retained target reference, candidate retrieval independent of the local sample | `test_truncation_keeps_full_filtered_totals_and_shares`; `test_time_window_keeps_reference_when_target_is_truncated_out` |

These groups overlap. They are descriptions of coverage, not eight separate suites to add together.

### What the tests do not prove

1. They do not validate SereneDB's full-text engine, production query performance, or every possible log format.
2. The provider-loop test uses scripted model replies. It checks one-tool/two-model-call execution and evidence plumbing; it does not prove a real model will always choose that path.
3. Prompt-contract tests check that instructions are present, not that every future generated answer follows them.
4. The tests do not prove a real-world root cause, system-wide incident, or business impact.
5. The 52-pass result does not mark an unseen final written answer as reviewed.

The reported result comes from the completed implementation evaluation. No test suite or live query was rerun solely to write this documentation.

## 8. Final Live Results

### Test A: Top five severity-3 BGL templates

**Question:** the exact top-five severity-3 prompt quoted in Improvement 2, Section G.

**Tool call:**

```python
count_problems(dataset="bgl", group_by="template", severity=3, limit=5)
```

**Verified result:**

| Rank | Template | Message pattern | Severity-3 log lines | Component | Team |
|---:|---|---|---:|---|---|
| 1 | E19 | `data TLB error interrupt` | 152,734 | KERNEL | compute-kernel |
| 2 | E146 | `data storage interrupt` | 63,491 | KERNEL | compute-kernel |
| 3 | E20 | `instruction address: <*>` | 47,563 | KERNEL | compute-kernel |
| 4 | E10 | `ciod: Error loading <*> invalid or missing program image, No such file or directory` | 45,824 | APP | job-runtime |
| 5 | E421 | `ciod: Error loading <*> invalid or missing program image, Permission denied` | 42,528 | APP | job-runtime |

The full filtered population contained **876,428 severity-3 lines across 467 templates**. The table is the top five within that population. It is not five separate incidents.

**SQL evidence:** the recorded Q1 ranking query applied `dataset = %s AND severity = %s` before `GROUP BY dataset, template_id`, then ordered by `lines DESC`.

The tool argument was `limit=5`. The executed ranking query's parameters were:

```json
["bgl", 3, 6]
```

The SQL asks for one extra row to determine whether more templates exist, then returns only five groups. This is called lookahead. The tool limit should remain five; the model should not add the extra row itself.

The total-count query used `["bgl", 3]` with the same filtering conditions. Shares were calculated from that filtered total, not the entire corpus or just the displayed five groups.

| Check | Status |
|---|---|
| Correct tool, `severity=3`, and requested `limit=5` | **PASS** |
| Exact counts, components, and teams | **PASS** |
| SQL, actual parameters, and Q1 citations preserved | **PASS** |
| Concise completed answer without unrelated history calls | **PASS** in the final recorded retry |
| Independent incidents or root causes established | **NOT VERIFIED**; not measured by this ranking |

### Test B: Investigate BGL line 17742

The causal-reasoning retest and the later neighboring-event evidence retest used this same target. Their review status must be kept separate.

| Target field | Verified value |
|---|---|
| Dataset and line | BGL, line 17742 |
| Template and message | E19, `data TLB error interrupt` |
| Level and component | FATAL, KERNEL |
| Owning team | compute-kernel |
| Timestamp | `2005-06-05 00:08:13.410695` |
| Source | `R20-M0-N2-C:J10-U11` |

The local same-source window is target time ±60 minutes: `2005-06-04 23:08:13.410695` through `2005-06-05 01:08:13.410695`.

| Evidence item | Verified observation | Interpretation |
|---|---|---|
| Same-source local problems | None within ±60 minutes | No local same-source problem sequence was established |
| Line 17466 | E10 APP FATAL, about 1,055.35 seconds earlier, different source | `preceding_event`: context only |
| Supported causal candidate | None identified | `candidate_cause` is `null`; no supported APP-to-KERNEL mechanism |
| Line 17745 | E19 FATAL at `2005-06-05 00:08:13.577322`, different source | Matching pattern about 0.167 seconds later; not evidence of a shared cause |
| Historical recurrence | Available separately | Does not establish a local cascade or a system-wide incident |

**SQL evidence boundary:** no complete raw envelope or conversation-local Q number for the last neighboring-events retest was provided for this report. The following are verified **implementation excerpts**, not a claimed verbatim capture of that unseen transcript.

```sql
-- Same-source local context: target itself excluded.
WHERE dataset = %s AND source = %s AND is_problem
  AND line_id <> %s AND ts >= %s AND ts <= %s
```

Here the source is the target source, and the time bounds are the ±60-minute values above. The local sample requests 11 rows and exposes at most 10, with a truncation flag when needed.

```sql
-- Earlier observation/lead search: strict upper time bound.
WHERE dataset = %s AND is_problem
  AND ts >= %s AND ts < %s
```

The earlier-event bounds are `2005-06-04 23:08:13.410695` and `2005-06-05 00:08:13.410695`. The existing query prefers same-source observations and then the nearest earlier timestamp. A different-source result from this query alone is not enough to populate a causal candidate.

By default, surrounding context in `investigate_event()` uses line IDs. For this target and its default `window=10`, those bounds are **17732 through 17752**, ordered by line ID. That is a position range, not a guarantee that all rows are near the target in time.

The `get_nearby_events()` time-window path separately fetches the target reference and returns at most 60 rows from the requested timestamp window. Keeping that reference is what prevents the truncation-related false “target missing” result.

**Reliable interpretation:**

> Line 17466 is an earlier error on another source, with no established link to the target. No supported causal candidate has been identified. Line 17745 shows the same E19 pattern about 0.167 seconds later on a different source. These observations do not establish a shared cause or failure cascade.

This paragraph summarizes the supported conclusion. It is not presented as a quotation from the unreviewed final answer.

| Verification | Status |
|---|---|
| Local time filtering and source relationships | **PASS** in the recorded live evaluation |
| Causal-reasoning investigation of line 17742 | **PASS**, as confirmed by the team |
| Final neighboring-event tool evidence | **PASS**, as confirmed by the team |
| Final written answer from that last neighbor retest | **NOT YET REVIEWED** |
| Real causal relationship between the listed events | **NOT ESTABLISHED** |

## 9. What We Learned

1. **AI tools need clear input definitions.** A tool schema lists supported arguments. Keep the function signature and published schema aligned so the model can request a filter that actually exists.
2. **SQL filters must match the question.** Check the `WHERE` clause before accepting a ranking. “Critical-only” requires the exact severity category, not just the broad problem flag.
3. **Log lines are not independent incidents.** One fault may produce many lines. Report the measured unit rather than renaming counts as failures or outages.
4. **Log order is not necessarily time order.** Read the timestamp and signed gap. A lower line ID does not prove that the message happened earlier.
5. **Nearby events are not automatically related.** Compare sources, templates, and operation identifiers before proposing a relationship. A matching template can be a useful pattern without proving a common cause.
6. **Shared sources do not prove causality.** Two errors on one node can have different causes. Use source identity as a lead and ask what evidence would confirm the explanation.
7. **Historical recurrence does not prove a system-wide incident.** Inspect source spread and dates. A large count can be concentrated on a few sources or in a few bursts.
8. **Offline and live tests serve different purposes.** Small fixtures expose edge cases precisely. Live checks confirm the deployed tool path and real data. Written-answer review checks the model's interpretation.
9. **The running server may need a restart to load changes.** Verify which process and tool definitions the browser reaches. A passing test against disk code does not prove an old process has loaded that code. Use the approved restart procedure rather than starting a duplicate server.

## 10. Final Status and Remaining Limitations

| Improvement | Automated Tests | Live Evidence | Final Answer Review | Status |
|---|---|---|---|---|
| 1. Correct tool arguments and structured selection | **PASS** within the 52-test suite | **PASS**: direct and restarted-server severity checks | Successful severity answer reviewed | **PASS** for recorded scope |
| 2. Critical-only ranking | **PASS** within the 52-test suite | **PASS**: five exact severity-3 template counts | Final ranking, citations, and SQL parameters reviewed | **PASS** |
| 3. Neighbor interpretation | **PASS**: nine dedicated neighbor regressions plus existing coverage | **PASS**: final line-17742 tool evidence | **NOT YET REVIEWED** for the last neighbor answer | **PARTIALLY VERIFIED** |
| 4. Supported causal leads | **PASS** within the 52-test suite | **PASS**: line-17742 causal-reasoning retest | Recorded causal-reasoning retest confirmed as passed; separate last neighbor answer remains unreviewed | **PASS** for recorded causal-reasoning scope |

### Remaining limitations

1. **Neighbor context is not exhaustive.** Line-order neighbors are a positional sample. Even an explicit time-window query has a returned-row limit. `nearby_same_template_events` uses returned context rather than a complete independent search for every matching event.
2. **Associations are not proof.** A same-source message or shared identifier supports investigation, not a causal mechanism. The Engineer still needs evidence showing how one event contributed to another.
3. **Identifier matching is conservative.** It recognizes labelled `request_id`, `job_id`, `trace_id`, and `correlation_id` forms supported by `_event_links()`. Other formats may not be detected. The optional different-source lookup examines at most 100 text matches and reports truncation; it is not an exhaustive causal analysis.
4. **Historical volume can be concentrated.** Many lines may come from a small number of sources or bursts. Counts do not establish one system-wide incident, independent failures, downtime, or customer impact.
5. **The final neighboring-events answer remains unreviewed.** Do not mark its wording or reasoning as passed until someone compares that specific answer with its evidence trail.
6. **Offline model calls are scripted.** Passing the provider-loop test does not enforce a universal live tool-call budget or guarantee future model choices.
7. **Evaluation scope is limited.** This report does not constitute full Team Lead, Manager, or external MCP-client answer verification.

All four implementation changes are complete. The outstanding evaluation action is to review the last neighboring-events written answer and record its actual result. Documentation review alone does not close that action.

## 11. Quick Reference

### Key files

| File | Look here for |
|---|---|
| [agent.py](../../agent.py) | `ROLES["engineer"]`, `system_prompt()`, `_execute()`, provider loops |
| [tools.py](../../tools.py) | Tool functions, `_severity_filters()`, `_event_links()`, `Evidence`, `TOOLS` definitions |
| [app.py](../../app.py) | Browser request path, in-memory sessions, streamed results |
| [mcp_server.py](../../mcp_server.py) | External MCP registration and instructions |
| [tests/test_severity_filters.py](../../tests/test_severity_filters.py) | All 52 recorded offline regressions |
| [sql/schema.sql](../../sql/schema.sql) | Existing severity and ownership definitions; reference only for this evaluation |

### Important tools

| Tool | Use it for | Important caution |
|---|---|---|
| `count_problems` | Exact grouped counts and rankings, with optional level/severity | `problems_only=True` mixes severities unless an exact filter is also supplied |
| `search_logs` | Message searches or structured-only level/severity retrieval | `text="FATAL"` searches words; `level="FATAL"` selects the level |
| `get_nearby_events` | Positional context or a bounded timestamp window | `position` means log order; same-source filtering is supported with `minutes` |
| `investigate_event` | Target, context, local same-source sample, historical recurrence, observed predecessor, supported lead | A candidate is not a proven cause |
| `template_history` | Historical totals and spread of one template | History is not local cascade evidence |
| `preceding_patterns` | Patterns unusually common in a preceding window | Co-occurrence and a large ratio do not prove causality |

### Main questions and direct checks

1. **Broad original investigation:** use the exact question in Section 4 when checking the complete Engineer workflow.
2. **Isolated ranking:** use the exact severity-3 question in Improvement 2, Section G.
3. **Targeted causal check:** “Investigate BGL line 17742.” Compare observations with supported associations.
4. **Neighbor check:** inspect line 17742's context for the source difference and fractional gap to line 17745. Review the written answer separately.

```python
count_problems(dataset="bgl", group_by="template", severity=3, limit=5)
search_logs(dataset="bgl", level="FATAL")
investigate_event(dataset="bgl", line_id=17742)
get_nearby_events(dataset="bgl", line_id=17742, minutes=1)
```

These are live tool examples for a future authorized check, not commands executed during this documentation task.

### Important SQL filters

| Need | Filter or rule |
|---|---|
| Exact original level | `upper(level) = upper(%s)` |
| Highest numeric category | `severity = %s`, parameter `3` |
| Broad problem definition | `severity >= 1 OR COALESCE(is_alert, false)` |
| Strictly earlier within an hour | `ts >= target_minus_one_hour AND ts < target_time` — explanatory bounds, not literal column names |
| Local same-source context | `source = %s` plus inclusive start/end timestamp bounds |

The default problem-only filter still applies to `count_problems()`. To count all ordinary INFO or severity-0 lines, also set `problems_only=False`.

### Run offline tests

From the project root, using the existing environment:

```bash
.venv/bin/python -B -m unittest discover -s tests -v
git diff --check
```

These offline tests use mocked database connections. They do not require a data reload. Do not run `load.py` or `prepare_data.py` as part of an Engineer fix evaluation.

### Common debugging mistakes

| Mistake | Better check |
|---|---|
| Search for a level name in message text | Filter `level` or numeric `severity` |
| Accept “critical” because `max_severity` is 3 | Inspect the actual filter before grouping |
| Count displayed rows as the total | Read exact totals and truncation notes |
| Pass six to the tool for a top-five question | Pass five; the tool performs internal lookahead |
| Treat `position="before"` as earlier in time | Compare timestamps and `seconds_from_target` |
| Connect different-source errors through an imagined mechanism | Keep them contextual unless a supported association exists |
| Treat a shared identifier as proof of cause | Explain the association and what would confirm causality |
| Assume updated disk files mean updated browser behavior | Verify the running process and advertised tool inputs |
| Mark evidence verification as answer verification | Review the actual written explanation against its citations |

### How to verify future fixes

1. Write down the exact question, expected tool inputs, and expected result before testing.
2. Add a small offline regression that fails for the reported behavior. Preserve existing tests.
3. Run the offline suite and `git diff --check`.
4. Before an authorized live retest, confirm that the server belongs to this project and has loaded the intended code. Restart only through the approved procedure when needed.
5. Save the tool inputs, exact SQL, parameters, returned data, truncation flags, and notes. Preserve the conversation-local evidence IDs.
6. Review the written answer separately: severity scope, counts, timestamp order, sources, causal claims, and citations must agree with the tool evidence.
7. Record **PASS**, **FAIL**, or **NOT YET REVIEWED** for each layer. Do not fill missing transcripts with assumed success.
