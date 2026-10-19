from saymore import updater


def _rel(tag="v1.3.0", body="", assets=None):
    return {"tag_name": tag, "body": body, "html_url": "https://example/rel",
            "assets": assets if assets is not None else [
                {"name": "notes.txt", "browser_download_url": "x", "size": 1},
                {"name": f"Saymore-Setup-{tag[1:]}.exe",
                 "browser_download_url": "https://example/setup.exe", "size": 123}]}


def test_version_compare():
    assert updater.is_newer("1.2.10", "1.2.9")
    assert updater.is_newer("v1.3", "1.2.9")
    assert not updater.is_newer("1.2.2", "1.2.2")
    assert not updater.is_newer("1.2", "1.2.0")
    assert not updater.is_newer("1.2.1", "1.2.2")
    assert not updater.is_newer("garbage", "1.2.2")


def test_parse_release_picks_installer_and_sha():
    sha = "ab" * 32
    info = updater.parse_release(_rel(body=f"## 更新\n- 修复\n\nSHA-256：{sha}"))
    assert info["version"] == "1.3.0"
    assert info["url"] == "https://example/setup.exe"
    assert info["size"] == 123
    assert info["sha256"] == sha.upper()
    assert info["force"] is False


def test_force_marker_is_detected_and_hidden():
    info = updater.parse_release(_rel(body=f"{updater.FORCE_MARKER}\n修复严重问题"))
    assert info["force"] is True
    assert updater.FORCE_MARKER not in info["notes"]
    assert info["sha256"] == ""


def test_release_without_installer_is_ignored():
    assert updater.parse_release(_rel(assets=[])) is None
