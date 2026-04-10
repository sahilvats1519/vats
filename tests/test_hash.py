from app import image_fingerprint, social_links_only, MatchResult


def test_fingerprint_is_stable():
    data = b"abc123"
    assert image_fingerprint(data) == image_fingerprint(data)


def test_fingerprint_changes():
    assert image_fingerprint(b"a") != image_fingerprint(b"b")


def test_social_filtering():
    rows = [
        MatchResult(source="x", title="a", url="https://instagram.com/p/1"),
        MatchResult(source="x", title="b", url="https://example.com/post"),
    ]
    result = social_links_only(rows)
    assert len(result) == 1
    assert "instagram" in result[0].url
