LIFECYCLE = {
    "queued": "Waiting to start",
    "planning": "Preparing analysis",
    "running": "Analysis in progress",
    "awaiting_input": "Waiting for your answer",
    "awaiting_confirmation": "Confirmation needed",
    "completed": "Analysis finished",
    "failed": "Could not complete the analysis",
    "cancelled": "Analysis cancelled",
}
STEP_STATUS = {
    "to_do": "Waiting",
    "in_progress": "In progress",
    "completed": "Done",
    "failed": "Could not complete",
    "cancelled": "Cancelled",
    "paused": "Paused",
}
QUALITY = {
    "complete": "Complete result",
    "partial": "Partial result — some requested data or checks are missing.",
    "unavailable": "Requested data unavailable — any available layers may be supporting material only.",
    "unknown": "Completeness unknown — saved evidence is insufficient.",
}
IMPORT_STATUS = {
    "pending": "Results ready to import",
    "downloading": "Downloading result layers",
    "importing": "Importing result layers",
    "complete": "Result import finished",
    "failed": "Import interrupted. Retry saved result import; analysis will not run again.",
}


ISSUE_CODES = {
    "source_unavailable",
    "search_failed",
    "not_ready",
    "sql_timeout",
    "incomplete_check",
    "unknown_evidence",
}
DIAGNOSTIC_CODES = ISSUE_CODES | {
    "polling_unavailable",
    "recovery_unavailable",
    "submission_rejected",
    "start_response_unavailable",
    "action_response_unavailable",
    "results_response_unavailable",
    "recovery_save_failed",
    "import_failed",
    "request_failed",
    "project_link_unsaved",
    "project_link_invalid",
    "insufficient_credits",
    "qgis_action_conflict",
    "qgis_request_validation",
}


def diagnostic_text(value) -> str:
    if not isinstance(value, dict):
        return ""
    code = value.get("code")
    if not isinstance(code, str) or code not in DIAGNOSTIC_CODES:
        return ""
    status = value.get("http_status")
    return code + (
        f" (HTTP {status})" if type(status) is int and 100 <= status <= 599 else ""
    )


def present_step(step: dict) -> dict:
    error = step.get("error")
    safe = error if isinstance(error, dict) else {}
    return {
        "title": step.get("title", ""),
        "status": STEP_STATUS.get(step.get("status"), "Updating status"),
        "error": safe.get("message", "")
        if isinstance(safe.get("message", ""), str)
        else "",
        "details": diagnostic_text(safe),
    }


def present_analysis(state: dict, request_state: dict, import_state: dict) -> dict:
    phase = request_state.get("phase")
    if phase in {"capturing", "synchronizing", "recapture"}:
        state = {}
        import_state = {}
    latest = state.get("latest") or {}
    local = {
        "capturing": "Preparing layers",
        "synchronizing": "Uploading layers",
        "starting": "Waiting to start",
        "recapture": "Preparation interrupted",
        "cancelled": "Analysis cancelled",
        "rejected": "Analysis start rejected",
    }
    if phase in local:
        headline = local[phase]
    elif latest.get("phase") == "resolving_area" and latest.get("status") in {
        "planning",
        "queued",
        "running",
    }:
        headline = "Finding the analysis area"
    elif latest.get("status"):
        headline = LIFECYCLE.get(latest["status"], "Updating status")
    elif (state.get("last_rejection") or {}).get("operation") == "start":
        headline = "Analysis start rejected"
    elif state.get("start_intent") or state.get("task_id"):
        headline = "Waiting to start"
    else:
        headline = "Ready — enter your request and press GO."
    notices = []
    if phase == "recapture":
        notices.append("Retry to capture the selected layers again.")
    import_status = import_state.get("status")
    if import_status:
        notices.append(IMPORT_STATUS.get(import_status, "Updating import status"))
    warnings = import_state.get("warnings") or []
    notices.extend(
        "Style: " + warning for warning in warnings if isinstance(warning, str)
    )
    manifest = state.get("results") or {}
    outcome = (
        manifest.get("outcome")
        if state.get("results") is not None
        else latest.get("outcome")
    ) or {}
    summary = []
    if latest.get("status") == "completed" or state.get("results") is not None:
        summary.append(QUALITY.get(outcome.get("quality"), QUALITY["unknown"]))
        layers = manifest.get("layers") or []
        names = [
            layer["name"] for layer in layers if isinstance(layer.get("name"), str)
        ]
        if state.get("results") is not None:
            summary.append(
                "Received layers: " + (", ".join(names) if names else "None")
            )
        issues = [
            issue["message"]
            for issue in outcome.get("issues", [])
            if isinstance(issue, dict) and isinstance(issue.get("message"), str)
        ]
        if issues:
            summary.append("Known gaps:\n" + "\n".join(dict.fromkeys(issues)))
        imported = import_state.get("imported_names") or []
        if imported:
            summary.append("Imported layers: " + ", ".join(imported))
        elif import_status == "complete":
            summary.append("Imported layers: None")
        elif layers:
            summary.append("Layer import is not complete.")
    return {
        "headline": headline,
        "steps": [
            present_step(step)
            for step in latest.get("steps", [])
            if isinstance(step, dict)
        ],
        "notices": notices,
        "outcome_message": "\n\n".join(summary),
        "details": "\n".join(
            value
            for value in [
                "Lifecycle: " + LIFECYCLE.get(latest.get("status"), "Updating status"),
                "Phase: Finding the analysis area"
                if latest.get("phase") == "resolving_area"
                else "",
                diagnostic_text(
                    {"code": (state.get("last_rejection") or {}).get("error_code")}
                ),
            ]
            + [diagnostic_text(issue) for issue in outcome.get("issues", [])]
            if value
        ),
    }
