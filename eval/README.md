# Evaluation harness

Rules and definitions live in [../docs/EVALUATION.md](../docs/EVALUATION.md).
This directory holds the data and the runner.

```
datasets/{seeded,real_bugs,injection,faults}/   cases with ground truth
hidden/                                         upstream maintainer tests, not in git
arms.py                                         baseline (no_gate) and full pipeline
splits.yaml                                     dev and test case IDs, frozen
schema.py                                       case and result schemas
runner.py                                       executes an arm over a split
report.py                                       the only thing that produces numbers
```
