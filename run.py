from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path, PurePosixPath
from datetime import datetime
from io import BytesIO
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

REPOSITORY = "rnvart2020-del/Barnoma"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
ARCHIVE_URL = f"https://api.github.com/repos/{REPOSITORY}/zipball/{{sha}}"
VERSION_FILE = ROOT / ".barnoma_version"
ALLOWED_FILES = {"index.html", "run.py", "README.md"}
MAX_ARCHIVE_SIZE = 20 * 1024 * 1024


def github_request(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Barnoma-Updater/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read(500).decode("utf-8", "replace")
        if error.code == 403:
            raise RuntimeError(
                "GitHub временно ограничил запросы. Подожди несколько минут и попробуй снова. "
                + detail[:180]
            ) from error
        raise RuntimeError(f"GitHub вернул ошибку HTTP {error.code}: {detail[:180]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(
            "Не удалось подключиться к GitHub. Проверь интернет-соединение и попробуй ещё раз."
        ) from error


def stage_latest_update():
    """Download and validate the exact GitHub commit without touching installed files."""
    commit_data = json.loads(github_request(API_URL).decode("utf-8"))
    latest_sha = commit_data.get("sha")
    if not latest_sha or len(latest_sha) != 40:
        raise RuntimeError("GitHub вернул некорректный номер версии.")

    installed_sha = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else ""
    if installed_sha == latest_sha:
        return {"status": "current", "message": "У вас уже установлена последняя версия.", "sha": latest_sha[:7]}

    archive = github_request(ARCHIVE_URL.format(sha=latest_sha))
    if len(archive) > MAX_ARCHIVE_SIZE:
        raise RuntimeError("Архив обновления слишком большой.")

    staging_root = ROOT / ".barnoma_update_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
    staged_dir = staging_root / latest_sha
    staged_dir.mkdir(parents=True, exist_ok=True)
    found = set()

    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if sum(item.file_size for item in members) > MAX_ARCHIVE_SIZE:
                raise RuntimeError("Распакованный архив превышает допустимый размер.")

            for item in members:
                parts = PurePosixPath(item.filename).parts
                if len(parts) != 2 or parts[1] not in ALLOWED_FILES or item.is_dir():
                    continue
                name = parts[1]
                if name in found:
                    raise RuntimeError(f"В архиве обнаружен повторяющийся файл: {name}")
                destination = staged_dir / name
                destination.write_bytes(bundle.read(item))
                found.add(name)

        if not {"index.html", "run.py"}.issubset(found):
            raise RuntimeError(
                "В архиве GitHub не найдены index.html и run.py. "
                "Обновление отменено, текущие файлы не изменены."
            )
        return {
            "status": "staged",
            "message": "Обновление загружено и проверено.",
            "sha": latest_sha,
            "staged_dir": str(staged_dir),
        }
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def finish_update(staged_dir_name, latest_sha, parent_pid):
    """Run as a helper process: wait for the app to exit, then replace its files."""
    staged_dir = Path(staged_dir_name).resolve()
    if staged_dir.parent != (ROOT / ".barnoma_update_staging").resolve():
        raise RuntimeError("Недопустимый путь временного обновления.")

    # The old server must fully exit before replacing run.py (especially on Windows).
    while True:
        try:
            os.kill(int(parent_pid), 0)
            time.sleep(0.35)
        except OSError:
            break

    backup_dir = ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in ALLOWED_FILES if (ROOT / name).is_file()]
    for name in existing:
        shutil.copy2(ROOT / name, backup_dir / name)

    replaced = []
    try:
        for name in sorted(p.name for p in staged_dir.iterdir() if p.is_file() and p.name in ALLOWED_FILES):
            source = staged_dir / name
            destination = ROOT / name
            # Windows can briefly retain a file lock after the server exits.
            last_error = None
            for attempt in range(20):
                try:
                    os.replace(source, destination)
                    last_error = None
                    break
                except PermissionError as error:
                    last_error = error
                    time.sleep(0.5)
            if last_error:
                raise last_error
            replaced.append(name)

        temp_version = ROOT / ".barnoma_version.tmp"
        temp_version.write_text(latest_sha, encoding="utf-8")
        os.replace(temp_version, VERSION_FILE)
        shutil.rmtree(staged_dir.parent, ignore_errors=True)

        log = ROOT / ".barnoma_update.log"
        log.write_text(
            f"Обновление {latest_sha[:7]} установлено: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Резервная копия: {backup_dir}\n",
            encoding="utf-8",
        )
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [sys.executable, str(ROOT / "run.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
    except Exception as error:
        for name in reversed(replaced):
            backup = backup_dir / name
            if backup.exists():
                shutil.copy2(backup, ROOT / name)
        (ROOT / ".barnoma_update_error.log").write_text(
            f"Ошибка установки обновления: {error}\nВремя: {datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
        raise


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/update":
            self.send_json(404, {"status": "error", "message": "Неизвестный адрес."})
            return
        if self.headers.get("X-Barnoma-Update") != "1":
            self.send_json(403, {"status": "error", "message": "Запрос обновления отклонён."})
            return
        try:
            result = stage_latest_update()
            if result["status"] == "current":
                self.send_json(200, result)
                return

            helper_args = [
                sys.executable,
                str(ROOT / "run.py"),
                "--finish-update",
                result["staged_dir"],
                result["sha"],
                str(os.getpid()),
            ]
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                helper_args,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
            self.send_json(200, {
                "status": "updated",
                "message": "Обновление загружено. Программа сейчас перезапустится.",
                "sha": result["sha"][:7],
            })
            threading.Timer(0.8, self.server.shutdown).start()
        except Exception as error:
            self.send_json(500, {
                "status": "error",
                "message": f"Не удалось подготовить обновление: {error}",
            })


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--finish-update":
        try:
            finish_update(sys.argv[2], sys.argv[3], sys.argv[4])
        except Exception:
            sys.exit(1)
    else:
        address = "127.0.0.1"
        port = 8000
        url = f"http://{address}:{port}"
        server = ThreadingHTTPServer((address, port), Handler)
        print("Barnoma запущена:", url)
        print("Проверка обновлений доступна в приложении.")
        print("Чтобы остановить сервер, нажми Ctrl+C.")
        try:
            threading.Timer(1, lambda: __import__("webbrowser").open(url)).start()
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nBarnoma остановлена.")
        finally:
            server.server_close()
