from pathlib import Path


def test_default_bootstrap_creates_plans_and_admin(app_module):
    assert app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first() is not None
    assert app_module.Admin.query.filter_by(email="admin@scanstory.com").first() is not None


def test_media_storage_dirs_are_always_absolute(app_module):
    """Regression guard for the broken target-image bug: os.replace() (write,
    resolved against the process cwd for a relative dir) and
    send_from_directory() (read, resolved against app.root_path for a
    relative dir) only agree once the directory itself is absolute - whether
    it came from SCANSTORY_DATA_DIR or the built-in default."""
    import os

    for attr in ("DATA_DIR", "IMAGES_DIR", "VIDEOS_DIR", "ADMIN_DATA_DIR", "ADMIN_IMAGES_DIR", "ADMIN_VIDEOS_DIR"):
        path = getattr(app_module, attr)
        assert os.path.isabs(path), f"{attr} is not absolute: {path!r}"


def test_data_dir_default_is_anchored_to_base_dir_not_cwd():
    """The specific regression this fixes: SCANSTORY_DATA_DIR unset must
    default under BASE_DIR (app.root_path), not a bare "data" that resolves
    against whatever directory the process happened to be launched from."""
    source = Path("app.py").read_text(encoding="utf-8")
    assert (
        'DATA_DIR = os.environ.get("SCANSTORY_DATA_DIR", os.path.join(BASE_DIR, "data"))'
        in source
    )


def test_project_pair_path_helpers_use_isolated_storage(app_module, project_with_pair):
    project, pair = project_with_pair
    assert pair.image_file_path.startswith(app_module.IMAGES_DIR)
    assert pair.video_file_path.startswith(app_module.VIDEOS_DIR)
    assert pair.npz_file_path.startswith(app_module.FEATURES_DIR)
    assert f"{project.id}_{pair.pair_index}.npz" in pair.npz_file_path


def test_missing_feature_artifact_returns_empty_payload(app_module, project_with_pair):
    project, pair = project_with_pair
    features = app_module.load_features(project.id, pair.pair_index)
    assert features["w"] == 0
    assert features["h"] == 0


def test_existing_feature_artifact_loads_dimensions(app_module, project_with_pair, feature_artifact):
    project, pair = project_with_pair
    features = app_module.load_features(project.id, pair.pair_index)
    assert features["w"] == 100
    assert features["h"] == 100
