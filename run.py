from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import webbrowser

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

if __name__ == "__main__":
    address = "127.0.0.1"
    port = 8000
    url = f"http://{address}:{port}"
    server = ThreadingHTTPServer((address, port), Handler)
    print("Barnoma запущена:", url)
    print("Чтобы остановить сервер, нажми Ctrl+C.")
    try:
        webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBarnoma остановлена.")
    finally:
        server.server_close()
