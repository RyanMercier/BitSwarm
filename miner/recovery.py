from dataclasses import dataclass, field
from enum import Enum

from config import MAX_MINER_ITERATIONS as MAX_ITERATIONS
MAX_CONSECUTIVE_SAME_ERROR = 3
MAX_DIFFERENT_ERRORS_SAME_TEST = 3
TEST_OUTPUT_MAX_CHARS = 4000


class StopReason(Enum):
    TESTS_PASSED = "tests_passed"
    MAX_ITERATIONS = "max_iterations_exhausted"
    REPEATED_ERROR = "same_error_repeated_3x"
    TIMEOUT = "execution_timeout"
    SCOPE_VIOLATION = "scope_violation"


class RecoveryAction(Enum):
    INCREMENTAL_FIX = "incremental_fix"
    HARD_RESET = "hard_reset"


@dataclass
class IterationRecord:
    """Record of a single implement-test cycle."""
    iteration: int
    files_written: list
    test_command: str
    tests_passed: bool
    test_output: str
    error_summary: str
    fix_description: str


@dataclass
class RetryState:
    """Persistent state across retry iterations."""
    iteration_count: int = 0
    history: list = field(default_factory=list)
    consecutive_same_error: int = 0
    last_error_signature: str = ""
    stop_reason: StopReason | None = None
    per_test_error_variants: dict = field(default_factory=dict)
    hard_reset_triggered: bool = False


def extract_error_signature(test_output):
    """
    Extract a normalized error signature for dedup.
    Two errors are "the same" if they fail on the same test
    with the same exception type.
    """
    failed_lines = []
    for line in test_output.split("\n"):
        if "FAILED" in line or "ERROR" in line:
            normalized = line.strip().split(" - ")[0]
            failed_lines.append(normalized)
    return "|".join(sorted(failed_lines))


def extract_per_test_errors(test_output):
    """
    Extract a mapping of test_name -> error_type for thrashing detection.
    """
    per_test = {}
    current_test = None
    for line in test_output.split("\n"):
        if "FAILED" in line and "::" in line:
            current_test = line.split("::")[1].split(" ")[0].strip()
        if current_test and ("Error:" in line or "Exception:" in line):
            error_type = line.strip().split(":")[0].strip()
            per_test[current_test] = error_type
            current_test = None
    return per_test


def detect_thrashing(state, new_per_test):
    """
    Detect if the miner is thrashing: same test failing with different errors
    each iteration. Triggers hard reset after 3 distinct error types on one test.
    """
    for test_name, error_type in new_per_test.items():
        if test_name not in state.per_test_error_variants:
            state.per_test_error_variants[test_name] = set()
        state.per_test_error_variants[test_name].add(error_type)

        if len(state.per_test_error_variants[test_name]) >= MAX_DIFFERENT_ERRORS_SAME_TEST:
            return RecoveryAction.HARD_RESET

    return RecoveryAction.INCREMENTAL_FIX


def format_test_feedback(test_output, iteration):
    """Format test output for injection into the agent's next turn."""
    if len(test_output) > TEST_OUTPUT_MAX_CHARS:
        test_output = "[...truncated...]\n" + test_output[-TEST_OUTPUT_MAX_CHARS:]

    hint = ""
    if "TypeError" in test_output:
        hint = (
            "HINT: TypeError usually means wrong return type or wrong argument type. "
            "Check the function signature and return type hint."
        )
    elif "AssertionError" in test_output:
        hint = (
            "HINT: AssertionError means your output doesn't match expected. "
            "Re-read the docstring and test assertion carefully."
        )
    elif "ImportError" in test_output or "ModuleNotFoundError" in test_output:
        hint = (
            "HINT: Import error. Only import from shared files, existing repo files, "
            "and packages in requirements. Do not install new packages."
        )
    elif "AttributeError" in test_output:
        hint = (
            "HINT: AttributeError usually means you're accessing a field that "
            "doesn't exist on a type. Re-read the schema in shared files."
        )
    elif "NotImplementedError" in test_output:
        hint = (
            "HINT: NotImplementedError means you haven't replaced a stub yet. "
            "Check that you wrote ALL functions in the file, not just some."
        )
    elif "mock" in test_output.lower() or "patch" in test_output.lower():
        hint = (
            "HINT: Mock/patch related error. Your implementation must use the exact "
            "module path that the test's @patch decorator targets. Read the test's "
            "patch path and make sure your code imports from that same path."
        )

    return (
        f"--- TEST RESULTS (Iteration {iteration}) ---\n"
        f"{test_output}\n"
        f"--- END TEST RESULTS ---\n"
        f"{hint}\n"
        f"Fix the failing tests. Do not rewrite from scratch  -  identify the specific "
        f"error, fix the specific issue, and re-run."
    )


def build_retry_context(state):
    """Build context string to inject on retry."""
    if not state.history:
        return ""

    lines = [
        f"You are on iteration {state.iteration_count} of {MAX_ITERATIONS}.",
        "Previous attempts:",
    ]

    for record in state.history:
        status = "PASSED" if record.tests_passed else "FAILED"
        lines.append(
            f"  Iteration {record.iteration}: {status} -- {record.error_summary or 'N/A'}"
        )

    if state.hard_reset_triggered:
        lines.append(
            f"\nHARD RESET: The same test has failed {MAX_DIFFERENT_ERRORS_SAME_TEST} times "
            f"with a DIFFERENT error each time. Your incremental fixes are making things "
            f"worse, not better. STOP patching. Re-read the original stub file and test "
            f"file from scratch. Write a completely new implementation. Do not reuse any "
            f"of your previous code."
        )
    elif state.consecutive_same_error >= 2:
        lines.append(
            f"\nWARNING: The same error has occurred {state.consecutive_same_error} times. "
            f"Your previous fix approach is not working. Try a fundamentally different "
            f"approach. Re-read the docstring and test file from scratch."
        )

    return "\n".join(lines)


def should_stop(state):
    """Determine if the retry loop should stop."""
    if state.history and state.history[-1].tests_passed:
        return True, StopReason.TESTS_PASSED

    if state.iteration_count >= MAX_ITERATIONS:
        return True, StopReason.MAX_ITERATIONS

    if state.consecutive_same_error >= MAX_CONSECUTIVE_SAME_ERROR:
        return True, StopReason.REPEATED_ERROR

    return False, None


def update_state(state, record):
    """Update retry state after an iteration."""
    state.iteration_count += 1
    state.history.append(record)

    # Same-error detection
    error_sig = extract_error_signature(record.test_output)
    if error_sig == state.last_error_signature and error_sig != "":
        state.consecutive_same_error += 1
    else:
        state.consecutive_same_error = 1
        state.last_error_signature = error_sig

    # Thrashing detection
    if not record.tests_passed:
        per_test = extract_per_test_errors(record.test_output)
        action = detect_thrashing(state, per_test)
        if action == RecoveryAction.HARD_RESET and not state.hard_reset_triggered:
            state.hard_reset_triggered = True

    stop, reason = should_stop(state)
    if stop:
        state.stop_reason = reason

    return state
