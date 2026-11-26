import datetime as dt
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent

import httpx

from messy_weather_sports_bot import scheduler_store

DOCUMENTATION = Path(__file__).parents[2] / "README.md"
PASSWORD = "disposable browser test password"


def test_documented_caddy_waitress_boundary(monkeypatch, tmp_path):
    caddy = shutil.which("caddy")
    assert caddy, "Install Caddy before running the deployment test."
    monkeypatch.setenv("MESSY_WEATHER_DB_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("MESSY_WEATHER_DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("MESSY_WEATHER_DASHBOARD_SECRET", "x" * 32)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "caddy-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "caddy-config"))
    scheduler_store.initialize(dt.datetime.now(dt.UTC))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"https://127.0.0.1:{port}"
    # macOS has a short Unix socket path limit. Keep the test socket name short.
    with tempfile.TemporaryDirectory(prefix="mw-") as runtime:
        sock = Path(runtime, "web.sock")
        config = _write_caddyfile(caddy, url, sock, tmp_path)
        command = [
            sys.executable,
            "-c",
            "from messy_weather_sports_bot.dashboard import main; raise SystemExit(main())",
            "--socket",
            str(sock),
        ]
        with (
            _service(command, tmp_path / "waitress.log") as app,
            _service([caddy, "run", "--config", str(config)], tmp_path / "caddy.log") as proxy,
        ):
            trust = tmp_path / "caddy-data/caddy/pki/authorities/local/root.crt"
            tls = _wait_for_login(url, trust, sock, app, proxy)
            assert stat.S_IMODE(sock.stat().st_mode) == 0o660
            _check_login_and_refresh(url, tls)


@contextmanager
def _service(command: list[str], log_path: Path):
    with log_path.open("w+") as log, subprocess.Popen(command, stdout=log, stderr=log) as server:
        try:
            yield server
        except Exception:
            log.seek(0)
            sys.stderr.write(log.read())
            raise
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()


def _write_caddyfile(caddy: str, url: str, sock: Path, directory: Path) -> Path:
    password_hash = subprocess.check_output(
        [caddy, "hash-password"], input=f"{PASSWORD}\n", text=True
    ).strip()
    version = subprocess.check_output([caddy, "version"], text=True)
    _, opening, remainder = DOCUMENTATION.read_text().partition(
        "\n    https://dashboard.home.arpa {\n"
    )
    body, closing, _ = remainder.partition("\n    }\n")
    assert opening and closing, "The README must provide the dashboard Caddy site block."
    site = (
        dedent(opening + body + closing)
        .replace("https://dashboard.home.arpa", url)
        .replace("<pi-lan-ip>", "127.0.0.1")
        .replace("<username>", "operator")
        .replace("<password-hash>", password_hash)
        .replace("/run/messy-weather-dashboard/dashboard.sock", str(sock))
    )
    if re.match(r"^v?2\.[67]\.", version):
        site = site.replace("basic_auth", "basicauth")
    config = directory / "Caddyfile"
    config.write_text(
        "{\n  admin off\n  skip_install_trust\n  auto_https disable_redirects\n}\n" + site
    )
    return config


def _wait_for_login(
    url: str,
    trust: Path,
    sock: Path,
    app: subprocess.Popen,
    proxy: subprocess.Popen,
) -> ssl.SSLContext:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert app.poll() is None, "Waitress exited before startup."
        assert proxy.poll() is None, "Caddy exited before startup."
        if trust.is_file() and sock.is_socket():
            tls = ssl.create_default_context(cafile=str(trust))
            try:
                response = httpx.get(f"{url}/", verify=tls, trust_env=False, timeout=1)
            except httpx.TransportError:
                pass
            else:
                assert response.status_code == 401
                return tls
        time.sleep(0.05)
    raise AssertionError("The authenticated dashboard did not start.")


def _check_login_and_refresh(url: str, tls: ssl.SSLContext) -> None:
    with httpx.Client(base_url=url, verify=tls, trust_env=False) as client:
        for path in ("/games/nfl", "/admin/tasks", "/admin/tasks/status", "/admin/data/games"):
            response = client.get(path)
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"].startswith("Basic ")
        auth = ("operator", PASSWORD)
        tasks = client.get("/admin/tasks", auth=auth)
        assert tasks.status_code == 200
        _check_browser_resources(client, tasks.text)
        assert "Secure" in tasks.headers["Set-Cookie"]
        token = re.search(r'name="csrf_token" value="([^"]+)"', tasks.text)
        assert token is not None
        form = {"csrf_token": token.group(1)}
        refresh = "/admin/tasks/nfl/refresh"
        assert client.post(refresh, data=form).status_code == 401
        assert client.post(refresh, auth=auth, data=form).status_code == 400
        assert (
            client.post(
                refresh, auth=auth, data=form, headers={"Referer": f"{url}/admin/tasks"}
            ).status_code
            == 303
        )
        assert "Queued" in client.get("/admin/tasks/status", auth=auth).text

        oversized = client.post(refresh, auth=auth, content=b"x" * (8 * 1024 + 1))
        assert oversized.status_code == 413
        assert "X-Content-Type-Options" not in oversized.headers


def _check_browser_resources(client: httpx.Client, tasks_html: str) -> None:
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', tasks_html)
    assert assets, "The dashboard must link to its CSS and JavaScript."
    # Browsers can request assets and discover icons without sending page credentials.
    icons = (
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/apple-touch-icon-180x180.png",
    )
    expected = dict.fromkeys(assets, (200, False)) | dict.fromkeys(icons, (404, False))
    actual = {}
    for path in expected:
        response = client.get(path)
        actual[path] = (response.status_code, "WWW-Authenticate" in response.headers)
    assert actual == expected
