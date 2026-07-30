from models import Experience, Trigger


PROCESSING_STATUSES = {"draft", "uploading", "validating", "optimizing", "extracting", "robustness_testing", "retry_scheduled", "retrying"}
FAILED_STATUSES = {"failed"}
READY_STATUSES = {"ready"}


def summarize_experience_processing(experience_id):
    experience = Experience.query.get(experience_id)
    if not experience:
        return {"exists": False}
    triggers = Trigger.query.filter_by(experience_id=experience.id).all()
    summary = {
        "exists": True,
        "trigger_count": len(triggers),
        "ready": 0,
        "processing": 0,
        "failed": 0,
        "excluded": 0,
        "missing_required_asset": 0,
        "warning": 0,
        "blocked_dependency": 0,
        "processing_ready": False,
    }
    for trigger in triggers:
        if trigger.is_excluded:
            summary["excluded"] += 1
            continue
        if trigger.status in READY_STATUSES:
            summary["ready"] += 1
        elif trigger.status in FAILED_STATUSES:
            summary["failed"] += 1
        elif trigger.status in PROCESSING_STATUSES:
            summary["processing"] += 1
        else:
            summary["warning"] += 1
        roles = {link.role for link in trigger.trigger_assets}
        if "reference_image" not in roles or "video" not in roles:
            summary["missing_required_asset"] += 1
    active_count = summary["trigger_count"] - summary["excluded"]
    summary["processing_ready"] = active_count > 0 and summary["ready"] == active_count and summary["missing_required_asset"] == 0
    return summary
