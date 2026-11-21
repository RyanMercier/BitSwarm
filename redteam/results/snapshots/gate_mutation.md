# Gate mutation (Phase 2.4)

Each row applies a known-bad patch (every other subtask uses its
reference). 'Grader rejected?' is whether the grader itself gave the
subtask zero. 'Audit caught?' is whether the independent audit suite
failed. A row where the grader gave credit but the audit failed is a
HOLE: the grader paid for code that does not work.

| Gate | Task | Grader score | Stub | Integ | Regr | Holdback | Audit | Grader rejected? | Audit caught? | Verdict |
| --- | --- | ---: | --- | ---: | --- | --- | --- | --- | --- | --- |
| Stub (fake renderer) | wordle | 1.0 | True | 1.0 | None | 1/1=True | 2/9 | no | yes | HOLE: grader paid for broken code |
| Integration (wrong scorer) | wordle | 1.0 | True | 1.0 | None | 1/1=True | 5/10 | no | yes | HOLE: grader paid for broken code |
| Integration (MISSING module) | minidb | 0.0 | False | 1.0 | None | 7/7=True | 0/12 | yes | yes | gate rejected |
| Holdback (diff mode) | click_enum | 0.0 | False | 0.5 | True | 2/3=False | 13/14 | yes | yes | gate rejected |
| Regression (diff mode) | click_enum | 0.5 | True | 0.5 | False | 3/3=True | 14/14 | no | no | ok |
