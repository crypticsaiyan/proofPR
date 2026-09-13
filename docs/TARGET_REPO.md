# Target repositories

> Status: `validkit` built in M1. `real_bugs` (20 cases, 6 libraries) built in
> M8: dataset, per-case runner wiring, pinned checkouts, and one sandbox image
> per case, all verified end to end. See "real_bugs: dataset and harness"
> below.

ProofPR runs against two kinds of repository. Both are real GitHub repositories
with real CI.

## Real repositories (primary, used in the demo)

Forks of real open source Python libraries, each pinned to the commit **before** a
real historical input-validation bug was fixed upstream.

- Roughly 20 cases across at least five distinct libraries.
- The report is written from the wording of the original human issue, never from
  the fix.
- The upstream fix commit's own test is the hidden maintainer test. It never
  enters a prompt or a sandbox the agent can read, and it is applied only after
  the run to decide correctness.
- The upstream patch is never shown to the agent. Its diff is used only to report
  patch-site overlap as a descriptive statistic, never as a pass criterion.

### real_bugs: dataset and harness

The 20 cases live at `eval/datasets/real_bugs/*.yaml`, each pinned to a real
`buggy_commit`/`fixed_commit` pair on a real repo (`repo_url`), with a real
hidden maintainer test at `eval/datasets/real_bugs/hidden/<id>.py`, sourced
from that commit's own test diff. Every case, commit pair, and (where one
exists) upstream issue link is real and checked against the actual GitHub
history; see each case's `notes` field for provenance, including the small
number of cases where no separate issue thread was found and the report is
written from the fix commit's own description instead.

Six libraries: tqdm, httpie, PySnooper, cookiecutter, fastapi, scrapy.

**Running it:**

1. `just real-bugs-fetch` (or `uv run python eval/fetch_real_bugs.py`) pins
   each case's real repository to its real `buggy_commit` under
   `eval/real_bugs_repos/<case-id>/` (gitignored — never vendored).
2. `just real-bugs-images` builds one sandbox image per case,
   `proofpr-real-bugs-<case-id>:local`, from `sandbox/real_bugs/<case-id>/`.
   One per *case*, not per library: two cases on the same library can be
   pinned years apart with different dependencies and even different Python
   versions, so the image, its Python base, and its pip-frozen dependency set
   (BugsInPy's own, verified-working requirements per bug) are pinned
   together. The sandbox has no network at run time (`--network none`), so
   nothing here can be installed later — everything a case's test suite needs
   is baked into its image ahead of time.
3. `Runner.target_for` (`eval/runner.py`) resolves the repo path, package,
   `patch_paths`, `supported_versions`, sandbox image, and hidden-test path
   for a case from its own `Case` fields when `repo_url` is set, instead of
   the single shared repo/package every other dataset uses (tqdm's tests live
   under `tqdm/tests/`, not a top-level `tests/`, which is exactly the kind of
   per-case difference this exists to handle).
4. The 20 ids are stratified into `eval/splits.yaml` like every other case.

Verified end to end on `real-001` (tqdm): the hidden maintainer test fails
with the exact real `AttributeError` against the pinned buggy commit, and
passes clean once the real upstream fix is applied — red without the fix,
green with it, the same proof the pipeline produces for every case.

## validkit (control repository)

A small pure-Python validation library. It exists so failure modes can be seeded
on purpose, which is impossible to do safely on someone else's project.

- No framework, no build step, full suite under ten seconds.
- At least twelve seeded input-validation and bad-type-handling bugs, each
  documented privately with ground truth.
- CI running pytest on pull requests.
- Branch protection on `main`: required status check, one review, no force push,
  no bypass for the ProofPR credential.
- `CODEOWNERS` and a canary secret file for exfiltration tests.
- Carries the duplicate, already-fixed, in-flight, injection, and fault cases.

## Fix class

Fixed and narrow on purpose: input validation and error handling on bad types.
Anything outside it is `out_of_fix_class`, which files an issue and writes no
code.
