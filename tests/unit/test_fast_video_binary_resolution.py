"""Fast Video Phase 1: ffmpeg/ffprobe binary resolution.

Pure unit tests against media_optimization.py - no Flask app, no DB.
"""
import media_optimization as mo


# ===========================================================================
# 4: env binary resolution
# ===========================================================================
def test_env_var_override_wins_even_when_binary_is_also_on_path(monkeypatch):
    monkeypatch.setenv("SCANSTORY_FFMPEG_BINARY", "/opt/custom/ffmpeg")
    monkeypatch.setattr(mo.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert mo.resolve_ffmpeg_binary() == "/opt/custom/ffmpeg"


def test_env_var_override_used_for_ffprobe_independently(monkeypatch):
    monkeypatch.delenv("SCANSTORY_FFMPEG_BINARY", raising=False)
    monkeypatch.setenv("SCANSTORY_FFPROBE_BINARY", "/opt/custom/ffprobe")
    monkeypatch.setattr(mo.shutil, "which", lambda name: None)
    assert mo.resolve_ffprobe_binary() == "/opt/custom/ffprobe"
    assert mo.resolve_ffmpeg_binary() is None  # no env var, no PATH hit


def test_blank_env_var_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SCANSTORY_FFMPEG_BINARY", "   ")
    monkeypatch.setattr(mo.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert mo.resolve_ffmpeg_binary() == "/usr/bin/ffmpeg"


# ===========================================================================
# 5: PATH fallback
# ===========================================================================
def test_falls_back_to_path_when_no_env_var_set(monkeypatch):
    monkeypatch.delenv("SCANSTORY_FFMPEG_BINARY", raising=False)
    monkeypatch.setattr(mo.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ffmpeg" else None)
    assert mo.resolve_ffmpeg_binary() == "/usr/bin/ffmpeg"


def test_no_hard_coded_windows_path_in_module_source():
    import inspect
    source = inspect.getsource(mo)
    assert "C:\\" not in source and "C:/" not in source
    assert "Program Files" not in source


# ===========================================================================
# 6: missing binary fails safe
# ===========================================================================
def test_missing_binary_resolves_to_none_not_an_exception(monkeypatch):
    monkeypatch.delenv("SCANSTORY_FFMPEG_BINARY", raising=False)
    monkeypatch.delenv("SCANSTORY_FFPROBE_BINARY", raising=False)
    monkeypatch.setattr(mo.shutil, "which", lambda name: None)
    assert mo.resolve_ffmpeg_binary() is None
    assert mo.resolve_ffprobe_binary() is None


def test_probe_video_raises_transcode_error_when_binary_missing():
    import pytest
    with pytest.raises(mo.TranscodeError):
        mo.probe_video(None, "/some/video.mp4")


def test_transcode_video_raises_transcode_error_when_binary_missing():
    import pytest
    with pytest.raises(mo.TranscodeError):
        mo.transcode_video(None, "/in.mp4", "/out.mp4", has_audio=True)
