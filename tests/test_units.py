import pytest

from stash import meta, model, ulid


# -- ulid --------------------------------------------------------------------------


def test_ulids_sort_by_time():
    ids = [ulid.new(now_ms=t) for t in (1_000, 2_000, 3_000, 1_700_000_000_000)]
    assert ids == sorted(ids)
    assert all(ulid.is_valid(i) for i in ids)
    assert ulid.timestamp_ms(ids[-1]) == 1_700_000_000_000


def test_ulid_bounds_bracket_every_id_in_that_millisecond():
    t = 1_700_000_000_123
    for _ in range(50):
        assert ulid.lower_bound(t) <= ulid.new(now_ms=t) <= ulid.upper_bound(t)
    assert ulid.upper_bound(t - 1) < ulid.lower_bound(t)


@pytest.mark.parametrize("value", ["", "x" * 26, "0" * 25, "8" + "0" * 25, "0" * 25 + "U"])
def test_invalid_ulids(value):
    assert not ulid.is_valid(value)


# -- model ---------------------------------------------------------------------------


def test_tag_normalization():
    assert model.normalize_tags(["#Machine Learning", "machine-learning", "  ", "C++", "naïve"]) == [
        "machine-learning",
        "c++",
        "naïve",
    ]
    assert model.normalize_tags("a, b  c") == ["a", "b", "c"]
    assert model.normalize_tags(None) == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com"),
        ("HTTP://Example.COM/CaseKept?Q=1#Frag", "http://example.com/CaseKept?Q=1#Frag"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_url_normalization(raw, expected):
    assert model.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://x.com", "javascript:alert(1)", "https://", "https://x.com:99999", 42])
def test_url_rejections(raw):
    with pytest.raises(model.ValidationError):
        model.normalize_url(raw)


def test_looks_like_url():
    assert model.looks_like_url("https://a.b/c")
    assert model.looks_like_url("news.ycombinator.com/item?id=1")
    assert not model.looks_like_url("buy milk")
    assert not model.looks_like_url("e.g. this")


def test_time_bounds():
    assert model.parse_time_bound(None, end_of_day=False) is None
    start = model.parse_time_bound("2026-01-02", end_of_day=False)
    end = model.parse_time_bound("2026-01-02", end_of_day=True)
    assert end - start == 86_400_000 - 1
    assert model.parse_time_bound("2026-01-02T00:00:00Z", end_of_day=False) == start
    with pytest.raises(model.ValidationError):
        model.parse_time_bound("yesterday", end_of_day=False)


# -- link previews ---------------------------------------------------------------------


def test_parse_head_prefers_open_graph():
    html = """<html><head>
      <title>  Plain   title </title>
      <meta property="og:title" content="OG &amp; title">
      <meta name="description" content="Fallback description">
      <meta property="og:description" content="The real
        description">
    </head><body><meta property="og:title" content="ignored body tag"></body></html>"""
    assert meta.parse_head(html) == {"title": "OG & title", "excerpt": "The real description"}


def test_parse_head_falls_back_to_title_tag_and_truncates():
    html = f"<title>Just a title</title><meta name=description content='{'word ' * 200}'>"
    result = meta.parse_head(html)
    assert result["title"] == "Just a title"
    assert len(result["excerpt"]) == 400
    assert result["excerpt"].endswith("…")


@pytest.mark.parametrize(
    "title, sites, expected",
    [
        ("Amazon DynamoDB - Wikipedia", ["", "wikipedia", "en.wikipedia.org"], "Amazon DynamoDB"),
        ("GitHub - google/adk-python: A toolkit", ["GitHub"], "google/adk-python: A toolkit"),
        ("Pricing | Stripe", ["stripe"], "Pricing"),
        ("Hacker News", ["", "ycombinator"], "Hacker News"),
        ("Rust - a language", ["", "example"], "Rust - a language"),
    ],
)
def test_strip_site_name(title, sites, expected):
    assert meta.strip_site_name(title, sites) == expected


def test_parse_head_drops_excerpt_that_repeats_title():
    html = """<meta property="og:title" content="GitHub - google/adk-python: An open-source, code-first Python toolkit">
      <meta property="og:description" content="An open-source, code-first Python toolkit - google/adk-python">"""
    assert meta.parse_head(html, "https://github.com/google/adk-python") == {
        "title": "google/adk-python: An open-source, code-first Python toolkit",
        "excerpt": "",
    }


def test_parse_head_survives_garbage():
    assert meta.parse_head("<<<>>><meta <title") == {"title": "", "excerpt": ""}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
    ],
)
def test_ssrf_guard_blocks_private_targets(url):
    with pytest.raises(meta.BlockedURL):
        meta.assert_public(url)
    assert meta.fetch(url) == {"title": "", "excerpt": ""}


def test_decompress_handles_common_encodings_and_caps_output():
    import gzip
    import zlib

    page = b"<title>hi</title>"
    assert meta.decompress(page, None) == page
    assert meta.decompress(gzip.compress(page), "gzip") == page
    assert meta.decompress(zlib.compress(page), "deflate") == page
    raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    assert meta.decompress(raw_deflate.compress(page) + raw_deflate.flush(), "deflate") == page
    assert meta.decompress(b"whatever", "br") is None
    assert meta.decompress(b"not gzip", "gzip") is None
    bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
    assert len(meta.decompress(bomb, "gzip")) == meta.MAX_BYTES


def test_redirects_to_private_targets_are_blocked():
    handler = meta._GuardedRedirects()
    with pytest.raises(meta.BlockedURL):
        handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")
