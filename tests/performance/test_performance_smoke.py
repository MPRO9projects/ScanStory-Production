import time

import pytest


pytestmark = pytest.mark.slow


def test_scanner_page_response_time_smoke(client, project_with_pair):
    project, pair = project_with_pair
    start = time.perf_counter()
    response = client.get(f"/scanner/{project.id}")
    elapsed = time.perf_counter() - start
    assert response.status_code == 200
    assert elapsed < 2.0
