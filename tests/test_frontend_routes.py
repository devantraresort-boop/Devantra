from html.parser import HTMLParser


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        url = values.get("src") if tag in {"img", "script"} else values.get("href") if tag == "link" else None
        if url and url.startswith("/"):
            self.urls.add(url)


def test_frontend_pages_and_linked_assets_survive_reorganization(client):
    urls = set()
    for path in ["/", "/admin", "/reviews"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        parser = PageAssets()
        parser.feed(response.text)
        urls.update(parser.urls)
    assert "/css/styles.css" in urls and "/js/admin.js" in urls
    assert "/assets/developers/tejas-joshi.webp" in urls
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.content, url


def test_verification_and_legacy_urls_remain_available(client):
    response = client.get("/googlef17034cc86bbb01d.html")
    assert response.status_code == 200
    assert response.text.strip() == "google-site-verification: googlef17034cc86bbb01d.html"
    for old, new in [
        ("/styles.css", "/css/styles.css"),
        ("/script.js", "/js/script.js"),
        ("/admin.css", "/css/admin.css"),
        ("/assets/admin-base.css", "/css/admin-base.css"),
        ("/admin.js", "/js/admin.js"),
        ("/devantara-resort.jpg", "/assets/resort/property-preview.jpg"),
        ("/file_00000000efec820b99f93b6e6968371f.png", "/assets/brand/resort-logo.png"),
    ]:
        response = client.get(old)
        assert response.status_code == 200, old
        assert response.content == client.get(new).content
