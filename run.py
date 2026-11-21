from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path, PurePosixPath
from datetime import datetime
from io import BytesIO
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
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
LOG_FILE = ROOT / ".barnoma_update.log"
ERROR_LOG_FILE = ROOT / ".barnoma_update_error.log"
STAGING_ROOT = ROOT / ".barnoma_update_staging"
HELPER_FILE = ROOT / ".barnoma_update_helper.py"
ALLOWED_FILES = {"index.html", "run.py", "README.md"}
MAX_ARCHIVE_SIZE = 20 * 1024 * 1024


def log(message, error=False):
    path = ERROR_LOG_FILE if error else LOG_FILE
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n"
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except Exception:
        pass


def github_request(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Barnoma-Updater/2.0",
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
    log("Проверка новой версии на GitHub.")
    commit_data = json.loads(github_request(API_URL).decode("utf-8"))
    latest_sha = commit_data.get("sha")
    if not latest_sha or len(latest_sha) != 40:
        raise RuntimeError("GitHub вернул некорректный номер версии.")

    installed_sha = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else ""
    if installed_sha == latest_sha:
        log(f"Установлена последняя версия {latest_sha[:7]}.")
        return {"status": "current", "message": "У вас уже установлена последняя версия.", "sha": latest_sha[:7]}

    archive = github_request(ARCHIVE_URL.format(sha=latest_sha))
    if len(archive) > MAX_ARCHIVE_SIZE:
        raise RuntimeError("Архив обновления слишком большой.")

    if STAGING_ROOT.exists():
        shutil.rmtree(STAGING_ROOT, ignore_errors=True)
    staged_dir = STAGING_ROOT / latest_sha
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
        log(f"Архив версии {latest_sha[:7]} загружен и проверен.")
        return {
            "status": "staged",
            "message": "Обновление загружено и проверено.",
            "sha": latest_sha,
            "staged_dir": str(staged_dir),
        }
    except Exception:
        shutil.rmtree(STAGING_ROOT, ignore_errors=True)
        raise


def wait_for_parent_exit(parent_pid):
    parent_pid = int(parent_pid)
    log(f"Помощник запущен. Ожидаю завершения старого сервера PID {parent_pid}.")
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0
        WAIT_FAILED = 0xFFFFFFFF
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            code = ctypes.get_last_error()
            if code in (87, 1168):
                log("Старый сервер уже завершился.")
                return
            raise RuntimeError(
                f"Windows не разрешила дождаться старого процесса (код {code}). "
                "Запусти Barnoma и обновление от обычного пользователя, не от разных учётных записей."
            )
        try:
            result = kernel32.WaitForSingleObject(handle, 120000)
            if result == WAIT_OBJECT_0:
                log("Старый сервер завершён.")
                return
            if result == WAIT_FAILED:
                raise RuntimeError(f"Ошибка ожидания процесса Windows: {ctypes.get_last_error()}.")
            raise RuntimeError("Старый сервер не завершился за 2 минуты. Обновление остановлено.")
        finally:
            kernel32.CloseHandle(handle)
    else:
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                log("Старый сервер завершён.")
                return
            except PermissionError:
                pass
            time.sleep(0.25)
        raise RuntimeError("Старый сервер не завершился за 2 минуты. Обновление остановлено.")


def finish_update(staged_dir_name, latest_sha, parent_pid):
    staged_dir = Path(staged_dir_name).resolve()
    if staged_dir.parent != STAGING_ROOT.resolve():
        raise RuntimeError("Недопустимый путь временного обновления.")
    wait_for_parent_exit(parent_pid)

    backup_dir = ROOT / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir.mkdir(parents=True, exist_ok=True)
    existing = [name for name in ALLOWED_FILES if (ROOT / name).is_file()]
    for name in existing:
        shutil.copy2(ROOT / name, backup_dir / name)

    old_version = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else None
    replaced = []
    try:
        log(f"Начинаю установку версии {latest_sha[:7]}.")
        staged_names = sorted(p.name for p in staged_dir.iterdir() if p.is_file() and p.name in ALLOWED_FILES)
        for name in staged_names:
            source = staged_dir / name
            destination = ROOT / name
            last_error = None
            for attempt in range(30):
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
            log(f"Файл {name} заменён.")

        temp_version = ROOT / ".barnoma_version.tmp"
        temp_version.write_text(latest_sha, encoding="utf-8")
        os.replace(temp_version, VERSION_FILE)

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # Run the new app only after all files and the version marker are in place.
        subprocess.Popen(
            [sys.executable, str(ROOT / "run.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
        log(f"Обновление {latest_sha[:7]} успешно установлено. Резервная копия: {backup_dir}")
        shutil.rmtree(STAGING_ROOT, ignore_errors=True)
    except Exception as error:
        log(f"Ошибка установки: {error}", error=True)
        for name in reversed(replaced):
            backup = backup_dir / name
            if backup.exists():
                shutil.copy2(backup, ROOT / name)
        if old_version is None:
            VERSION_FILE.unlink(missing_ok=True)
        else:
            VERSION_FILE.write_text(old_version, encoding="utf-8")
        log("Выполнен откат к предыдущим файлам.", error=True)
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

            # Important on Windows: the helper is a separate copy, so it does not
            # execute the run.py file that it needs to replace.
            shutil.copy2(Path(__file__).resolve(), HELPER_FILE)
            helper_args = [
                sys.executable,
                str(HELPER_FILE),
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
                "message": "Обновление загружено. Barnoma перезапустится автоматически.",
                "sha": result["sha"][:7],
            })
            threading.Timer(1.0, self.server.shutdown).start()
        except Exception as error:
            log(f"Не удалось подготовить обновление: {error}", error=True)
            self.send_json(500, {
                "status": "error",
                "message": f"Не удалось подготовить обновление: {error}",
            })


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--finish-update":
        try:
            finish_update(sys.argv[2], sys.argv[3], sys.argv[4])
        except Exception as error:
            log(f"Помощник обновления завершился с ошибкой: {error}", error=True)
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
