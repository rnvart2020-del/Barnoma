from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from io import BytesIO
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

REPOSITORY = "rnvart2020-del/Barnoma"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
ARCHIVE_URL = f"https://api.github.com/repos/{REPOSITORY}/zipball/main"
VERSION_FILE = ROOT / ".barnoma_version"
ALLOWED_FILES = {"index.html", "run.py", "README.md"}
MAX_ARCHIVE_SIZE = 20 * 1024 * 1024
RESTART_AFTER_UPDATE = False


def github_request(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Barnoma-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def install_latest_update():
    """Download the latest main branch, back up current files, then install safely."""
    commit_data = json.loads(github_request(API_URL).decode("utf-8"))
    latest_sha = commit_data["sha"]

    installed_sha = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else ""
    if installed_sha == latest_sha:
        return {"status": "current", "message": "У вас уже установлена последняя версия.", "sha": latest_sha[:7]}

    archive = github_request(ARCHIVE_URL)
    if len(archive) > MAX_ARCHIVE_SIZE:
        raise RuntimeError("Архив обновления слишком большой.")

    with tempfile.TemporaryDirectory(prefix="barnoma_update_", dir=ROOT) as temp_name:
        temp_dir = Path(temp_name)
        staged_dir = temp_dir / "staged"
        staged_dir.mkdir()
        found = set()

        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            members = bundle.infolist()
            total_size = sum(item.file_size for item in members)
            if total_size > MAX_ARCHIVE_SIZE:
                raise RuntimeError("Распакованный архив превышает допустимый размер.")

            for item in members:
                parts = Path(item.filename).parts
                if len(parts) != 2 or parts[1] not in ALLOWED_FILES or item.is_dir():
                    continue
                name = parts[1]
                if Path(name).name != name:
                    raise RuntimeError("В архиве обнаружен недопустимый путь.")
                destination = staged_dir / name
                destination.write_bytes(bundle.read(item))
                found.add(name)

        if not {"index.html", "run.py"}.issubset(found):
            raise RuntimeError("В архиве не найдены обязательные файлы Barnoma.")

        backup_dir = ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)
        existing = [name for name in ALLOWED_FILES if (ROOT / name).is_file()]
        for name in existing:
            shutil.copy2(ROOT / name, backup_dir / name)

        replaced = []
        try:
            for name in found:
                os.replace(staged_dir / name, ROOT / name)
                replaced.append(name)
            VERSION_FILE.write_text(latest_sha, encoding="utf-8")
        except Exception:
            for name in replaced:
                backup = backup_dir / name
                if backup.exists():
                    shutil.copy2(backup, ROOT / name)
            raise

    return {
        "status": "updated",
        "message": "Обновление установлено. Barnoma сейчас перезапустится.",
        "sha": latest_sha[:7],
    }


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global RESTART_AFTER_UPDATE
        if self.path != "/api/update":
            self.send_json(404, {"status": "error", "message": "Неизвестный адрес."})
            return
        if self.headers.get("X-Barnoma-Update") != "1":
            self.send_json(403, {"status": "error", "message": "Запрос обновления отклонён."})
            return
        try:
            result = install_latest_update()
            self.send_json(200, result)
            if result["status"] == "updated":
                RESTART_AFTER_UPDATE = True
                threading.Timer(1.2, self.server.shutdown).start()
        except Exception as error:
            self.send_json(500, {
                "status": "error",
                "message": f"Не удалось установить обновление: {error}",
            })


if __name__ == "__main__":
    address = "127.0.0.1"
    port = 8000
    url = f"http://{address}:{port}"
    server = ThreadingHTTPServer((address, port), Handler)
    print("Barnoma запущена:", url)
    print("Чтобы остановить сервер, нажми Ctrl+C.")
    try:
        threading.Timer(1, lambda: __import__("webbrowser").open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBarnoma остановлена.")
    finally:
        server.server_close()

    if RESTART_AFTER_UPDATE:
        print("Запускаю обновлённую Barnoma...")
        os.execv(sys.executable, [sys.executable, str(ROOT / "run.py")])
