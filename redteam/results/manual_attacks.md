# Manual grader attacks (Phase 3)

Each attack patches one subtask; every other subtask uses its reference
patch. Grader score is normalized (1.0 = full credit for the subtask).
Audit is independent ground truth. A row with a non-zero grader score and
an audit below 1.0 is a successful game of the grader.

| ID | Attack | Task/Subtask | Grader | Integ | Holdback | Audit | Gamed grader? | Hypothesis / result |
| --- | --- | --- | ---: | ---: | --- | --- | --- | --- |
| A1 | Hardcode visible+integration test answers | wordle/scorer | 0.75 | 0.5 | 0/1=False | 3/10 | YES | Passes stub+integration; audit catches; holdback (cli only) does not. |
| A4 | Exploit weak visible renderer tests | wordle/renderer | 1.0 | 1.0 | 1/1=True | 2/9 | YES | Passes weak stub tests; 0.5 integration floor still pays. |
| A2 | Integration-breaking wrong scorer | wordle/scorer | 1.0 | 1.0 | 1/1=True | 5/10 | YES | Wrong duplicate logic; integration ratio drops, floor keeps >=0.5. |
| A7 | Empty patch | wordle/scorer | 0.0 | 1.0 | 0/0=False | 0/10 | no | Empty/stub patch scores 0 (honesty override). |
| M2 | Missing core module, dependents keep credit | minidb/executor | 0.0 | 1.0 | 7/7=True | 0/12 | no | integration_ratio stays 1.0 (xfail hides NotImplementedError). |
| H1 | Hardcode visible, lose held-back (diff mode) | click_enum/enum_choice_implementation | 0.0 | 0.5 | 2/3=False | 13/14 | no | Holdback split in diff mode catches visible-only overfit. |
