import pytest

from callagent.training.ingest import IngestError
from callagent.training.web import check_public_url, crawl_site, read_page, safe_get, site_sections

PUBLIC = ["93.184.216.34"]


def resolver(host: str) -> list[str]:
    return {"acme.test": PUBLIC, "other.test": PUBLIC, "internal.test": ["10.0.0.5"], "rebind.test": PUBLIC + ["127.0.0.1"]}.get(host, [])


def site(pages: dict[str, tuple[int, dict, bytes]]):
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        return pages.get(url, (404, {}, b"not found"))

    return fetch, calls


def html(body: str, title: str = "Acme") -> tuple[int, dict, bytes]:
    return 200, {"content-type": "text/html; charset=utf-8"}, f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://internal.test/",
        "http://rebind.test/",  # one public and one loopback address: refused
        "ftp://acme.test/file",
        "file:///etc/passwd",
        "https://user:secret@acme.test/",
        "https://acme.test:8443/",
        "https://nowhere.invalid/",
    ],
)
def test_addresses_inside_our_network_are_never_fetched(url):
    def resolve(host):
        literal = host.strip("[]")
        return resolver(host) or ([literal] if literal[0].isdigit() or ":" in literal else {"localhost": ["127.0.0.1"]}.get(host, []))

    with pytest.raises(IngestError):
        check_public_url(url, resolve)


def test_public_address_is_accepted_and_fragment_dropped():
    assert check_public_url(" https://acme.test/services#team ", resolver) == "https://acme.test/services"


def test_redirect_to_a_private_address_is_refused():
    fetch, calls = site({"https://acme.test/": (302, {"location": "http://internal.test/secrets"}, b"")})
    with pytest.raises(IngestError, match="not a public website"):
        safe_get("https://acme.test/", fetcher=fetch, resolver=resolver)
    assert calls == ["https://acme.test/"]  # the private address was never requested


def test_redirects_are_followed_within_a_limit_and_errors_reported():
    loop, _ = site({"https://acme.test/a": (301, {"location": "/a"}, b"")})
    with pytest.raises(IngestError, match="too many times"):
        safe_get("https://acme.test/a", fetcher=loop, resolver=resolver)
    with pytest.raises(IngestError, match="error \\(404\\)"):
        safe_get("https://acme.test/missing", fetcher=site({})[0], resolver=resolver)


def test_non_pages_and_oversized_pages_are_refused():
    pdf, _ = site({"https://acme.test/f": (200, {"content-type": "application/pdf"}, b"%PDF-")})
    with pytest.raises(IngestError, match="not a web page"):
        read_page("https://acme.test/f", fetcher=pdf, resolver=resolver)
    big, _ = site({"https://acme.test/big": (200, {"content-type": "text/html"}, b"a" * (2 * 1024 * 1024 + 1))})
    with pytest.raises(IngestError, match="too large"):
        read_page("https://acme.test/big", fetcher=big, resolver=resolver)


def test_crawl_stays_on_the_site_respects_robots_and_the_page_limit():
    fetch, calls = site(
        {
            "https://acme.test/": html(
                "<h1>Acme Billing</h1><p>Medical billing for clinics.</p>"
                "<a href='/services'>Services</a><a href='/pricing#plans'>Pricing</a><a href='/private/notes'>Notes</a>"
                "<a href='https://other.test/partner'>Partner</a><a href='/logo.png'>Logo</a><a href='/services'>Again</a>"
                "<a href='mailto:hi@acme.test'>Mail</a><a href='/gone'>Gone</a>"
            ),
            "https://acme.test/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n"),
            "https://acme.test/services": html("<h2>Payment posting</h2><p>Posted within 24 hours.</p><a href='/team'>Team</a>", "Services"),
            "https://acme.test/pricing": html("<p>Starter is forty-nine dollars.</p>", "Pricing"),
            "https://acme.test/team": html("<p>Our team.</p>", "Team"),
            "https://acme.test/private/notes": html("<p>secret</p>"),
            "https://other.test/partner": html("<p>partner</p>"),
        }
    )
    pages = crawl_site("https://acme.test/", max_pages=3, fetcher=fetch, resolver=resolver)
    assert [p.url for p in pages] == ["https://acme.test/", "https://acme.test/services", "https://acme.test/pricing"]
    assert "https://acme.test/private/notes" not in calls and "https://other.test/partner" not in calls
    assert "https://acme.test/logo.png" not in calls and "https://acme.test/team" not in calls  # limit reached first
    assert calls.count("https://acme.test/services") == 1  # a repeated link is read once

    labels = [s.label for s in site_sections(pages)]
    assert labels[0] == "Acme > Acme Billing (https://acme.test/)"
    assert "Services > Payment posting (https://acme.test/services)" in labels


def test_a_broken_inner_link_is_skipped_but_a_broken_start_page_fails():
    fetch, _ = site({"https://acme.test/": html("<p>Welcome.</p><a href='/gone'>x</a>"), "https://acme.test/robots.txt": (404, {}, b"")})
    assert len(crawl_site("https://acme.test/", fetcher=fetch, resolver=resolver)) == 1
    with pytest.raises(IngestError):
        crawl_site("https://acme.test/nope", fetcher=fetch, resolver=resolver)


def test_a_site_that_forbids_bots_is_not_read():
    fetch, _ = site(
        {"https://acme.test/": html("<p>Welcome.</p>"), "https://acme.test/robots.txt": (200, {}, b"User-agent: *\nDisallow: /\n")}
    )
    with pytest.raises(IngestError, match="robots.txt"):
        crawl_site("https://acme.test/", fetcher=fetch, resolver=resolver)
