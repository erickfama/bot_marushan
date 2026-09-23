from __future__ import annotations

import argparse
import base64
import getpass
import http.server
import secrets
import urllib.parse
import webbrowser

import requests

SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"


def main() -> None:
    parser = argparse.ArgumentParser(description="Obtiene un refresh token de Spotify mediante PKCE.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.redirect_uri)
    client_secret = getpass.getpass("Spotify client secret: ")
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [None])[0] != state:
                self.send_error(400, "Estado OAuth inválido")
                return
            result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Autorización completada. Puedes cerrar esta ventana.".encode())

        def log_message(self, *_: object) -> None:
            return

    params = urllib.parse.urlencode({
        "client_id": args.client_id,
        "response_type": "code",
        "redirect_uri": args.redirect_uri,
        "scope": SCOPES,
        "state": state,
    })
    webbrowser.open(f"https://accounts.spotify.com/authorize?{params}")
    with http.server.HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 8765), Handler) as server:
        server.handle_request()
    credentials = base64.b64encode(f"{args.client_id}:{client_secret}".encode()).decode()
    response = requests.post("https://accounts.spotify.com/api/token", headers={"Authorization": f"Basic {credentials}"}, data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": args.redirect_uri,
    }, timeout=20)
    response.raise_for_status()
    print(response.json()["refresh_token"])


if __name__ == "__main__":
    main()
