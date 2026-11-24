import json

from scheduler import cli


class FakeResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"id":"job-a"}'


def test_cli_api_request_sends_json_to_running_service(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    result = cli.api_request("http://scheduler.local/", "/api/jobs", "POST", {"id": "job-a"})

    assert result == {"id": "job-a"}
    assert captured["request"].full_url == "http://scheduler.local/api/jobs"
    assert captured["request"].method == "POST"
    assert json.loads(captured["request"].data) == {"id": "job-a"}
    assert captured["timeout"] == 10
