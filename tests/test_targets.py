import pytest

from terrasync.targets import TargetError, check_email, check_webhook


def resolver(mapping):
    return lambda host: mapping[host]


def test_public_https_is_accepted():
    url = "https://hooks.example.com/terrasync"
    assert check_webhook(url, resolver=resolver(
        {"hooks.example.com": ["93.184.216.34"]})) == url


@pytest.mark.parametrize("url,addrs", [
    ("https://metadata.internal/", ["169.254.169.254"]),
    ("https://intranet.example/", ["10.0.0.5"]),
    ("https://localhost/", ["127.0.0.1"]),
    ("https://v6.example/", ["::1"]),
    ("https://mixed.example/", ["93.184.216.34", "192.168.1.1"]),
])
def test_private_targets_are_refused(url, addrs):
    host = url.split("/")[2]
    with pytest.raises(TargetError):
        check_webhook(url, resolver=resolver({host: addrs}))


def test_literal_private_ip_refused_without_lookup():
    with pytest.raises(TargetError):
        check_webhook("https://127.0.0.1:8787/x", resolver=lambda h: [])


def test_plain_http_refused_unless_private_allowed():
    with pytest.raises(TargetError):
        check_webhook("http://hooks.example.com/", resolver=lambda h: ["93.184.216.34"])
    assert check_webhook("http://10.0.0.5/hook", allow_private=True)


def test_credentials_in_url_refused():
    with pytest.raises(TargetError):
        check_webhook("https://user:pw@hooks.example.com/",
                      resolver=lambda h: ["93.184.216.34"])


def test_email():
    assert check_email(" ops@example.com ") == "ops@example.com"
    with pytest.raises(TargetError):
        check_email("not-an-address")
