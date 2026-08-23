"""Job lifecycle and state machine — SPEC.md §3 and §4.

Pure Python, no I/O. Every rule in SPEC.md §4 gets a test that names it, so a
future change to the transition table fails against the rule it broke rather
than against an opaque assertion.

The rules, restated:

1. `completed` requires a non-null artifact.
2. `failed` requires a non-null failure.
3. Terminal states never transition again.
4. A job may not stay in `running` without a stage.
5. `degraded=true` is compatible with `completed` — quality flag, not failure.
"""

import pytest

from app.domain.job import ArtifactRef, Failure, Job
from app.domain.state import (
    STAGE_ORDER,
    InvalidTransition,
    advance_stage,
    transition,
)

QUERY = "How does the pH scale work?"


def a_job() -> Job:
    return Job.create(query=QUERY)


def an_artifact() -> ArtifactRef:
    return ArtifactRef(
        url="/videos/abc/artifact", duration_s=68.4, size_bytes=7_930_112, scenes=5
    )


def a_failure(code="render_failed", stage="rendering") -> Failure:
    return Failure(code=code, stage=stage, message="Renderer produced no frames.")


def a_running_job(stage="scripting") -> Job:
    return transition(a_job(), "running", stage=stage)


def a_completed_job() -> Job:
    job = a_running_job("publishing")
    return transition(job, "completed", artifact=an_artifact())


def a_failed_job() -> Job:
    return transition(a_running_job("rendering"), "failed", failure=a_failure())


# ---------------------------------------------------------------------------
# Starting state
# ---------------------------------------------------------------------------

def test_a_new_job_starts_queued_with_no_stage():
    job = a_job()
    assert job.status == "queued"
    assert job.stage is None


def test_a_new_job_keeps_the_learners_query_verbatim():
    """SPEC.md §6.4: the raw query is stored and rendered on the title card.

    It is never normalised here — the resolver normalises a copy.
    """
    assert Job.create(query="  How does the PH scale work??  ").query == \
        "  How does the PH scale work??  "


def test_a_new_job_has_a_unique_id():
    assert a_job().job_id != a_job().job_id


def test_a_new_job_is_not_degraded_and_has_no_attempts():
    job = a_job()
    assert job.degraded is False
    assert job.attempts == 0
    assert job.failure is None
    assert job.artifact is None


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------

def test_queued_may_start_running():
    job = transition(a_job(), "running", stage="resolving")
    assert job.status == "running"
    assert job.stage == "resolving"


def test_running_may_complete_with_an_artifact():
    job = transition(a_running_job("publishing"), "completed", artifact=an_artifact())
    assert job.status == "completed"
    assert job.artifact is not None


def test_running_may_fail_with_a_failure():
    job = transition(a_running_job("muxing"), "failed", failure=a_failure("mux_failed", "muxing"))
    assert job.status == "failed"
    assert job.failure.code == "mux_failed"


def test_queued_may_fail_without_ever_running():
    """Resolution rejects an unsupported concept before any spend."""
    job = transition(
        a_job(), "failed",
        failure=a_failure("unsupported_concept", "resolving"),
    )
    assert job.status == "failed"


def test_a_transition_updates_the_timestamp():
    job = a_job()
    before = job.updated_at
    transition(job, "running", stage="resolving")
    assert job.updated_at >= before


# ---------------------------------------------------------------------------
# Rule 1 — completed requires an artifact
# ---------------------------------------------------------------------------

def test_job_never_reaches_completed_without_an_artifact():
    """The worst outcome this project can produce is reporting success for a
    file that is missing, zero-byte or silent (CLAUDE.md §11). The state
    machine refuses it structurally, before G7 is even consulted.
    """
    with pytest.raises(InvalidTransition):
        transition(a_running_job("publishing"), "completed")


def test_completed_is_compatible_with_degraded():
    """Rule 5. A fallback script still produces a real, watchable video."""
    job = a_running_job("publishing")
    job.degraded = True
    job = transition(job, "completed", artifact=an_artifact())
    assert job.status == "completed"
    assert job.degraded is True


# ---------------------------------------------------------------------------
# Rule 2 — failed requires a failure
# ---------------------------------------------------------------------------

def test_job_never_reaches_failed_without_a_named_failure():
    """R7: failures are named and explicit, never silent."""
    with pytest.raises(InvalidTransition):
        transition(a_running_job(), "failed")


def test_the_failure_records_the_stage_it_died_in():
    job = a_failed_job()
    assert job.failure.stage == "rendering"
    assert job.failure.code == "render_failed"


# ---------------------------------------------------------------------------
# Rule 3 — terminal states are terminal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["queued", "running", "completed", "failed"])
def test_a_completed_job_never_transitions_again(target):
    with pytest.raises(InvalidTransition):
        transition(a_completed_job(), target, artifact=an_artifact(),
                   failure=a_failure(), stage="publishing")


@pytest.mark.parametrize("target", ["queued", "running", "completed", "failed"])
def test_a_failed_job_never_transitions_again(target):
    with pytest.raises(InvalidTransition):
        transition(a_failed_job(), target, artifact=an_artifact(),
                   failure=a_failure(), stage="rendering")


def test_a_job_cannot_go_back_to_queued():
    with pytest.raises(InvalidTransition):
        transition(a_running_job(), "queued")


def test_a_job_cannot_skip_running_and_complete_directly():
    with pytest.raises(InvalidTransition):
        transition(a_job(), "completed", artifact=an_artifact())


# ---------------------------------------------------------------------------
# Rule 4 — running implies a stage
# ---------------------------------------------------------------------------

def test_running_requires_a_stage():
    with pytest.raises(InvalidTransition):
        transition(a_job(), "running")


def test_stages_advance_only_forward():
    job = a_running_job("narrating")
    with pytest.raises(InvalidTransition):
        advance_stage(job, "scripting")


def test_a_stage_cannot_advance_to_itself():
    job = a_running_job("narrating")
    with pytest.raises(InvalidTransition):
        advance_stage(job, "narrating")


def test_stages_may_skip_forward():
    """Resolution failures jump straight past the generation stages."""
    job = a_running_job("resolving")
    assert advance_stage(job, "muxing").stage == "muxing"


def test_the_documented_stage_order_is_the_pipeline_order():
    assert STAGE_ORDER == (
        "resolving", "scripting", "narrating", "rendering", "muxing", "publishing",
    )


def test_a_terminal_job_cannot_advance_its_stage():
    with pytest.raises(InvalidTransition):
        advance_stage(a_completed_job(), "publishing")


# ---------------------------------------------------------------------------
# The guard is loud, never silent
# ---------------------------------------------------------------------------

def test_an_illegal_transition_names_both_ends():
    """SPEC.md §4: illegal transitions are logged, never silently ignored.

    The exception message is what reaches the log, so it has to say what was
    attempted rather than just "invalid".
    """
    with pytest.raises(InvalidTransition) as excinfo:
        transition(a_completed_job(), "running", stage="muxing")

    message = str(excinfo.value)
    assert "completed" in message and "running" in message


def test_a_rejected_transition_leaves_the_job_untouched():
    """A guard that half-applies a transition is worse than no guard."""
    job = a_running_job("rendering")
    with pytest.raises(InvalidTransition):
        transition(job, "completed")  # no artifact

    assert job.status == "running"
    assert job.stage == "rendering"
    assert job.artifact is None
