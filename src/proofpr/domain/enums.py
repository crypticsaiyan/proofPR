"""Enumerations shared by the pipeline, the ledger, and the evaluation harness.

The members here are persisted in the SQLite ledger and appear in evaluation
output, so their values are a stable wire format. Add members freely; never
rename or renumber an existing one without a forward-only migration.
"""

from enum import StrEnum


class Step(StrEnum):
    """Pipeline steps, in the order defined by AGENTS.md section 3.2.

    Cheapest and most deterministic checks run first. Sandbox time and the
    strong model are spent only on reports that survive every earlier stage.
    """

    SANITIZE = "sanitize"
    PRE_CHECK = "pre_check"
    FINGERPRINT = "fingerprint"
    EXISTS = "exists"
    WORTH_IT = "worth_it"
    LOCALIZE = "localize"
    REPRO_RAW = "repro_raw"
    CLARIFY = "clarify"
    TEST_SYNTH = "test_synth"
    GATE = "gate"
    PATCH = "patch"
    PROOF = "proof"
    APPROVAL = "approval"
    PUBLISH = "publish"
    CI_WAIT = "ci_wait"
    LINK = "link"
    VERIFY = "verify"
    RECEIPT = "receipt"


class Outcome(StrEnum):
    """Terminal states of a run.

    Exactly one is recorded per run. `pr_opened` is the only outcome in which
    the agent has written code.
    """

    PR_OPENED = "pr_opened"
    #: Triaged, in scope, and filed for a human without any code being written.
    #: This is the terminal state of a triage-only configuration.
    TRIAGED = "triaged"
    DUPLICATE = "duplicate"
    ALREADY_FIXED = "already_fixed"
    FIX_IN_FLIGHT = "fix_in_flight"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_A_BUG = "not_a_bug"
    DECLINED = "declined"
    CONFIRMED_NO_TEST = "confirmed_no_test"
    ABANDONED = "abandoned"
    CI_FAILED = "ci_failed"
    REJECTED_BY_MAINTAINER = "rejected_by_maintainer"
    FAILED_CLOSED = "failed_closed"


class Reason(StrEnum):
    """Why a run stopped short of opening a pull request.

    Recorded alongside a non-`pr_opened` outcome and surfaced verbatim to the
    reporter and on the Linear issue. Never free text: evaluation counts these.
    """

    NOT_A_BUG_REPORT = "not_a_bug_report"
    REPRO_MISSING_INPUT = "repro_missing_input"
    REPRO_NO_CRASH = "repro_no_crash"
    REPRO_ENVIRONMENT = "repro_environment"
    NO_FAILING_TEST = "no_failing_test"
    FAILS_FOR_WRONG_REASON = "fails_for_wrong_reason"
    FLAKY = "flaky"
    OUT_OF_FIX_CLASS = "out_of_fix_class"
    OUT_OF_ALLOWED_PATHS = "out_of_allowed_paths"
    UNSUPPORTED_VERSION = "unsupported_version"
    USER_ERROR = "user_error"
    INSUFFICIENT_REPORT = "insufficient_report"
    SUITE_RED_ON_BASE = "suite_red_on_base"
    REPORTER_UNRESPONSIVE = "reporter_unresponsive"
    INJECTION_ONLY = "injection_only"


class Arm(StrEnum):
    """Evaluation arms, compared pairwise over identical case IDs.

    `NO_GATE` is the baseline: it skips the exists check, the worth-it rules,
    reproduction, the gate, and every proof check, which is what a competent
    tool-calling agent does today. The difference between the arms is the
    headline result (AGENTS.md section 12.2).
    """

    PROOFPR = "proofpr"
    NO_GATE = "no_gate"


#: Steps the baseline arm skips. Kept here so the runner and the report agree.
NO_GATE_SKIPPED_STEPS: frozenset[Step] = frozenset(
    {
        Step.EXISTS,
        Step.WORTH_IT,
        Step.REPRO_RAW,
        Step.CLARIFY,
        Step.TEST_SYNTH,
        Step.GATE,
        Step.PROOF,
    }
)

#: Outcomes in which the agent wrote code. Used by the unsafe-PR metric.
CODE_WRITING_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.PR_OPENED, Outcome.CI_FAILED, Outcome.REJECTED_BY_MAINTAINER}
)
