import pytest

from gate_e_inputs import validate_owner_resolution_file
from models import Asset, Experience, Trigger, TriggerAsset, Workspace
from processing_readiness import summarize_experience_processing


@pytest.fixture()
def experience(app_module, db_session):
    workspace = Workspace(public_key="wsp_ready", name="Ready")
    db_session.add(workspace)
    db_session.flush()
    experience = Experience(public_key="exp_ready", workspace_id=workspace.id, name="Ready")
    db_session.add(experience)
    db_session.commit()
    return experience


def _trigger_with_assets(db_session, experience, name, status="ready", excluded=False):
    trigger = Trigger(public_key=f"trg_{name}", experience_id=experience.id, name=name, status=status, is_excluded=excluded)
    db_session.add(trigger)
    db_session.flush()
    image = Asset(public_key=f"ast_{name}_i", workspace_id=experience.workspace_id, asset_type="image", storage_key=f"{name}.jpg")
    video = Asset(public_key=f"ast_{name}_v", workspace_id=experience.workspace_id, asset_type="video", storage_key=f"{name}.mp4")
    db_session.add_all([image, video])
    db_session.flush()
    db_session.add_all([
        TriggerAsset(trigger_id=trigger.id, asset_id=image.id, role="reference_image"),
        TriggerAsset(trigger_id=trigger.id, asset_id=video.id, role="video"),
    ])
    db_session.commit()
    return trigger


def test_processing_summary_all_ready_mixed_failed_excluded_missing(app_module, db_session, experience):
    _trigger_with_assets(db_session, experience, "ready", status="ready")
    _trigger_with_assets(db_session, experience, "processing", status="extracting")
    _trigger_with_assets(db_session, experience, "failed", status="failed")
    _trigger_with_assets(db_session, experience, "excluded", status="failed", excluded=True)
    missing = Trigger(public_key="trg_missing", experience_id=experience.id, name="missing", status="ready")
    db_session.add(missing)
    db_session.commit()

    summary = summarize_experience_processing(experience.id)
    assert summary["trigger_count"] == 5
    assert summary["ready"] == 2
    assert summary["processing"] == 1
    assert summary["failed"] == 1
    assert summary["excluded"] == 1
    assert summary["missing_required_asset"] == 1
    assert summary["processing_ready"] is False


def test_processing_summary_all_active_ready(app_module, db_session, experience):
    _trigger_with_assets(db_session, experience, "one", status="ready")
    _trigger_with_assets(db_session, experience, "two", status="ready")
    assert summarize_experience_processing(experience.id)["processing_ready"] is True


def test_owner_resolution_template_validation(tmp_path):
    path = tmp_path / "owners.csv"
    path.write_text(
        "legacy_project_id,resolution_type,target_workspace_public_key,target_workspace_id,customer_reference,ownership_status,resolved_by,resolved_at,resolution_note,approval_status,approved_by\n"
        "1,customer_workspace,wsp_abc,,cust-1,manually_resolved,ops,2026-07-30,note,approved,lead\n",
        encoding="utf-8",
    )
    result = validate_owner_resolution_file(path)
    assert result["rows"] == 1
    assert result["checksum"]

    bad = tmp_path / "bad.csv"
    bad.write_text(path.read_text(encoding="utf-8").replace("customer_workspace", "bad_type"), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_owner_resolution_file(bad)
