from datetime import datetime, timedelta


def _project(app_module, db_session, user, name, created_at, ready=True):
    project = app_module.Project(
        name=name,
        owner_user_id=user.id,
        created_at=created_at,
    )
    db_session.add(project)
    db_session.commit()
    pair = app_module.ProjectPair(
        project_id=project.id,
        pair_index=0,
        image_filename=f"{project.id}_0.jpg",
        video_filename=f"{project.id}_0.mp4",
        is_processed=ready,
        processing_status="completed" if ready else "failed",
    )
    db_session.add(pair)
    db_session.commit()
    return project


def test_projects_page_orders_newest_first(client, app_module, db_session, login_user):
    older = _project(app_module, db_session, login_user, "Older Story", datetime.utcnow() - timedelta(days=2))
    newer = _project(app_module, db_session, login_user, "Newer Story", datetime.utcnow())

    html = client.get("/projects").get_data(as_text=True)
    assert html.index("Newer Story") < html.index("Older Story")


def test_projects_page_search_matches_story_name(client, app_module, db_session, login_user):
    _project(app_module, db_session, login_user, "Zzyzx Unicorn Story", datetime.utcnow())
    _project(app_module, db_session, login_user, "Generic Other Project", datetime.utcnow())

    response = client.get("/projects?q=Zzyzx")
    assert response.status_code == 200
    assert b"Zzyzx Unicorn Story" in response.data
    assert b"Generic Other Project" not in response.data


def test_projects_page_non_matching_search_shows_filtered_empty_state(client, app_module, db_session, login_user):
    _project(app_module, db_session, login_user, "Has One Story", datetime.utcnow())

    response = client.get("/projects?q=NoSuchNameXYZ")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Has One Story" not in html
    # Must be the "results filtered to zero" copy, not the "brand new user" copy.
    assert "No stories match" in html
    assert "No Stories yet" not in html
    assert "Clear search" in html


def test_projects_page_zero_projects_shows_brand_new_user_empty_state(client, login_user):
    html = client.get("/projects").get_data(as_text=True)
    assert "No Stories yet" in html
    assert "No stories match" not in html


def test_projects_page_card_actions_have_correct_hrefs(client, app_module, db_session, login_user):
    project = _project(app_module, db_session, login_user, "Action Check Story", datetime.utcnow())

    html = client.get("/projects").get_data(as_text=True)
    assert f"/project/{project.id}" in html
    assert f"/projects/{project.id}/qr" in html
    assert f"/projects/{project.id}/edit" in html
    assert f'action="/projects/{project.id}/reprocess"' in html
    assert f'action="/projects/delete/{project.id}"' in html
