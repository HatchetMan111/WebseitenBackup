# Unit-Tests fuer Helferfunktionen (kein Netzwerk).
import base64

import pytest


# ---------------------------------------------------------------- canon_url
def test_canon_url_trailing_slash(a):
    assert a.canon_url("https://example.com/a/") == "https://example.com/a"
    assert a.canon_url("https://example.com/a") == a.canon_url("https://example.com/a/")


def test_canon_url_host_lowercase(a):
    assert a.canon_url("https://EXAMPLE.com/A") == "https://example.com/A"


def test_canon_url_strips_fragment_keeps_query(a):
    assert a.canon_url("https://example.com/p?a=1#sec") == "https://example.com/p?a=1"


def test_canon_url_default_port_dropped(a):
    assert a.canon_url("https://example.com:443/") == "https://example.com/"


def test_canon_url_root_index_dedup(a):
    """Fix: /index.html ist dieselbe Seite wie / (kein Duplikat-Backup)."""
    assert a.canon_url("https://example.com/index.html") == "https://example.com/"
    assert a.canon_url("https://example.com/index.htm") == "https://example.com/"
    assert a.canon_url("https://example.com/") == a.canon_url("https://example.com/index.html")


def test_canon_url_custom_port_kept(a):
    assert a.canon_url("http://example.com:8080/x") == "http://example.com:8080/x"


# ---------------------------------------------------------------- url_to_filename
def test_url_to_filename_root(a):
    assert a.url_to_filename("https://example.com/") == "index.html"


def test_url_to_filename_path(a):
    assert a.url_to_filename("https://example.com/ueber-uns/team") == "ueber-uns/team.html"


def test_url_to_filename_existing_html(a):
    assert a.url_to_filename("https://example.com/kontakt.html") == "kontakt.html"


def test_url_to_filename_query_hash(a):
    f = a.url_to_filename("https://example.com/suche?q=term")
    assert f.startswith("suche_") and f.endswith(".html")


def test_url_to_filename_traversal_sanitized(a):
    f = a.url_to_filename("https://example.com/../../etc/passwd")
    assert ".." not in f.split("/") and "passwd" in f


def test_url_to_filename_deep_path_truncated(a):
    f = a.url_to_filename("https://example.com/" + "/".join(f"s{i}" for i in range(10)))
    assert len(f.split("/")) <= 6


# ---------------------------------------------------------------- asset_local_path
def test_asset_local_path_basic(a):
    rel = a.asset_local_path("https://cdn.example.com/img/logo.png")
    assert rel.startswith("assets/cdn.example.com/") and rel.endswith("logo.png")


def test_asset_local_path_query_hash_before_extension(a):
    """Fix: Hash vor die Extension (Mimetype-Erkennung bleibt intakt)."""
    rel = a.asset_local_path("https://cdn.example.com/img/pic.jpg?v=123")
    name = rel.rsplit("/", 1)[-1]
    assert name.startswith("pic_") and name.endswith(".jpg"), rel


def test_asset_local_path_rejects_non_http(a):
    assert a.asset_local_path("data:image/png;base64,AAAA") is None
    assert a.asset_local_path("mailto:x@y.z") is None


# ---------------------------------------------------------------- slugify
def test_slugify_umlauts(a):
    assert a.slugify("Meine Firmen-Webseite!") == "meine_firmen_webseite"
    assert a.slugify("Ärger & Überraschung") == "aerger_ueberraschung"


# ---------------------------------------------------------------- same_site
def test_same_site_www(a):
    assert a.same_site("https://example.com/", "https://www.example.com/x")
    assert a.same_site("https://sub.example.com/", "https://example.com/")
    assert not a.same_site("https://example.com/", "https://other.org/")


# ---------------------------------------------------------------- split_srcset / css_refs
def test_split_srcset(a):
    out = a.split_srcset("a.jpg 1x, b.jpg 2x")
    assert out == [("a.jpg", " 1x"), ("b.jpg", " 2x")]


def test_css_refs_relative_and_absolute(a):
    css = "body{background:url(bg.png)} @font-face{src:url('https://cdn.example.com/f.woff')}"
    refs = a.css_refs(css, "https://example.com/style/main.css")
    assert "https://example.com/style/bg.png" in refs
    assert "https://cdn.example.com/f.woff" in refs


# ---------------------------------------------------------------- _decode_html (Charset-Fix)
def test_decode_html_meta_charset(a):
    raw = '<html><head><meta charset="iso-8859-1"></head><body>Käse</body></html>'.encode("iso-8859-1")
    out = a._decode_html(raw, "text/html")
    assert "Käse" in out


def test_decode_html_header_charset(a):
    raw = "Grüße".encode("latin-1")
    assert a._decode_html(raw, "text/html; charset=latin-1") == "Grüße"


def test_decode_html_bom(a):
    raw = b"\xef\xbb\xbf" + "Häuser".encode("utf-8")
    assert a._decode_html(raw, "text/html") == "Häuser"


def test_decode_html_fallback_utf8(a):
    assert a._decode_html("Häuser".encode("utf-8"), "text/html") == "Häuser"


def test_decode_html_broken_charset_name_falls_back(a):
    raw = "Häuser".encode("utf-8")
    assert a._decode_html(raw, "text/html; charset=---") == "Häuser"


# ---------------------------------------------------------------- validate_url_host
def test_validate_url_host_rejects_credentials(a):
    assert a.validate_url_host("https://user:pw@example.com/") is not None


def test_validate_url_host_rejects_ftp(a):
    assert a.validate_url_host("ftp://example.com/") is not None


def test_validate_url_host_allows_public(a):
    assert a.validate_url_host("https://example.com/") is None


# ---------------------------------------------------------------- _safe_pages_rel
def test_safe_pages_rel_simple(a):
    assert a._safe_pages_rel(".", "assets/host/x.png") == "assets/host/x.png"


def test_safe_pages_rel_parent(a):
    assert a._safe_pages_rel("blog/2024", "../../assets/x.png") == "assets/x.png"


def test_safe_pages_rel_rejects_traversal(a):
    assert a._safe_pages_rel(".", "../../../../etc/passwd") is None
    assert a._safe_pages_rel(".", "/etc/passwd") is None


def test_safe_pages_rel_rejects_absolute_urls(a):
    assert a._safe_pages_rel(".", "https://x.com/a.png") is None
    assert a._safe_pages_rel(".", "data:image/png;base64,AA") is None


# ---------------------------------------------------------------- page_visible_text / extract_title
def test_page_visible_text_strips_scripts(a):
    html = "<html><body><h1>Hallo</h1><script>evil()</script><style>x{}</style></body></html>"
    assert "Hallo" in a.page_visible_text(html) and "evil" not in a.page_visible_text(html)


def test_extract_title_fallback_h1_and_url(a):
    assert a.extract_title("<h1>Willkommen</h1>", "https://x.com/") == "Willkommen"
    assert a.extract_title("", "https://x.com/kontakt-impressum.html") == "Kontakt Impressum"


# ---------------------------------------------------------------- Single-File-Bausteine
def test_data_uri_png(a, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    uri = a._data_uri(f)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == f.read_bytes()


def test_data_uri_unknown_extension_none(a, tmp_path):
    f = tmp_path / "x.xyzunknown"
    f.write_bytes(b"zzz")
    assert a._data_uri(f) is None


def test_css_inline_inlines_local_refs(a, tmp_path):
    pages = tmp_path
    (pages / "assets" / "host").mkdir(parents=True)
    (pages / "assets" / "host" / "bg.png").write_bytes(b"\x89PNG")
    out = a._css_inline("body{background:url(assets/host/bg.png)}", ".", pages)
    assert "url(data:image/png;base64," in out


def test_css_inline_leaves_absolute(a, tmp_path):
    out = a._css_inline("body{background:url(https://cdn.example.com/x.png)}", ".", tmp_path)
    assert "https://cdn.example.com/x.png" in out


def test_css_inline_nested_css_recursive(a, tmp_path):
    pages = tmp_path
    (pages / "assets").mkdir()
    (pages / "assets" / "inner.css").write_text("a{color:red}")
    (pages / "assets" / "bg.png").write_bytes(b"\x89PNG")
    outer = '@import url(assets/inner.css); b{background:url(assets/bg.png)}'
    out = a._css_inline(outer, ".", pages)
    assert "data:text/css;base64," in out and "data:image/png;base64," in out
