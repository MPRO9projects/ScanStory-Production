from pathlib import Path

from PIL import Image, ImageDraw

from media_processing import (
    generate_qr_asset,
    probe_video,
    regenerate_qr_asset,
    regenerate_recognition_artifact,
    validate_reference_image,
)


def _marker(path, size=(160, 160)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 20):
        for y in range(0, size[1], 20):
            if (x + y) // 20 % 2 == 0:
                draw.rectangle([x, y, x + 10, y + 10], fill="black")
    image.save(path)


def test_image_validation_valid_and_invalid_cases(tmp_path):
    good = tmp_path / "marker.jpg"
    _marker(good)
    assert validate_reference_image(good)["outcome"] in {"valid", "valid_with_warning"}

    bad_ext = tmp_path / "marker.txt"
    bad_ext.write_text("x", encoding="utf-8")
    assert validate_reference_image(bad_ext)["warnings"] == ["invalid_extension"]

    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not-jpeg")
    assert "invalid_signature" in validate_reference_image(corrupt)["warnings"]

    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    assert "zero_byte_or_missing" in validate_reference_image(empty)["warnings"]

    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8), "black").save(tiny)
    result = validate_reference_image(tiny)
    assert "low_resolution" in result["warnings"]
    assert "dark_image" in result["warnings"]


def test_video_probe_degraded_and_unreadable(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not a video")
    unavailable = probe_video(video, ffprobe_path="ffprobe-definitely-missing")
    assert unavailable["outcome"] == "degraded"
    unreadable = probe_video(video)
    assert unreadable["outcome"] in {"invalid", "degraded"}


def test_recognition_artifact_regeneration_preserves_previous_on_failure(tmp_path):
    image = tmp_path / "marker.jpg"
    _marker(image)
    artifact = tmp_path / "orb.npz"
    first = regenerate_recognition_artifact(image, artifact, min_features=1)
    assert first["outcome"] in {"valid", "valid_with_warning"}
    previous = artifact.read_bytes()

    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"bad")
    failed = regenerate_recognition_artifact(corrupt, artifact)
    assert failed["outcome"] == "invalid"
    assert artifact.read_bytes() == previous


def test_qr_generation_and_regeneration_preserve_destination(tmp_path):
    qr = tmp_path / "master.png"
    destination = "https://example.test/e/permanent"
    first = generate_qr_asset(destination, qr)
    assert first["outcome"] == "valid"
    first_hash = first["asset_hash"]
    second = regenerate_qr_asset(destination, qr)
    assert second["destination"] == destination
    assert qr.exists()
    assert second["asset_hash"] == first_hash
    invalid = generate_qr_asset("javascript:alert(1)", tmp_path / "bad.png")
    assert invalid["outcome"] == "invalid"
