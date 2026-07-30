def test_default_bootstrap_creates_plans_and_admin(app_module):
    assert app_module.SubscriptionPlan.query.filter_by(is_trial_plan=True).first() is not None
    assert app_module.Admin.query.filter_by(email="admin@scanstory.com").first() is not None


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
