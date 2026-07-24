from __future__ import annotations

import pytest

from autostop_manager.public_camera import CAMERA_TITLE, PublicCameraError, extract_public_player_url


def test_extract_public_player_url_accepts_expected_public_player():
    payload = {
        "overlayTitle": CAMERA_TITLE,
        "content": '<iframe src="https://fl-4.telecoma.tv:443/semd185_1/embed.mp4?"></iframe>',
    }

    assert extract_public_player_url(payload) == "https://fl-4.telecoma.tv:443/semd185_1/embed.mp4?"


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"overlayTitle": "Другая камера", "content": ""}, "unexpected_camera_title"),
        ({"overlayTitle": CAMERA_TITLE, "content": "<div>no player</div>"}, "camera_iframe_missing"),
        (
            {"overlayTitle": CAMERA_TITLE, "content": '<iframe src="https://example.com/embed.mp4"></iframe>'},
            "unexpected_camera_player",
        ),
    ],
)
def test_extract_public_player_url_rejects_unexpected_payload(payload, reason):
    with pytest.raises(PublicCameraError, match=reason):
        extract_public_player_url(payload)
