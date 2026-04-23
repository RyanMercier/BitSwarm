def compute_scores(subtasks, miner_results, stub_results, integration_passed,
                    integration_pass_ratio=None):
    """
    Compute per-subtask scores.

    Scoring:
    - Stub tests passed = complexity_weight * integration_multiplier
    - Integration multiplier = ratio of integration tests that passed (0.0 to 1.0)
      If all pass: multiplier = 1.0, if none pass: multiplier = 0.5
      Scales linearly: 0% passed → 0.5, 100% passed → 1.0
    - Stub tests failed = 0.0
    - Patch conflict or scope violation = 0.0
    """
    scores = {}

    # Compute integration multiplier: linearly scale from 0.5 (0% pass) to 1.0 (100% pass)
    if integration_pass_ratio is not None:
        integration_mult = 0.5 + 0.5 * integration_pass_ratio
    elif integration_passed:
        integration_mult = 1.0
    else:
        integration_mult = 0.5

    for subtask in subtasks:
        sid = subtask["subtask_id"]
        result = miner_results.get(sid)

        # No submission or empty patch
        if result is None or not result.patch:
            scores[sid] = 0.0
            continue

        # Patch didn't apply cleanly
        if getattr(result, "merge_conflict", False):
            scores[sid] = 0.0
            continue

        # Stub tests failed
        if not stub_results.get(sid, False):
            scores[sid] = 0.0
            continue

        # Stub tests passed — apply integration multiplier
        weight = subtask.get("complexity_weight", 0.25)
        scores[sid] = weight * integration_mult

    return scores
