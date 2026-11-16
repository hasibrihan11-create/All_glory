import os
import sys
import time
import shlex
import threading
import subprocess
from pathlib import Path
from functools import wraps
from flask import Flask, jsonify, request, session, render_template

ROOT = Path(__file__).resolve().parent
RUN_FILE = ROOT / "run.py"
LOG_FILE = ROOT / "guildglory.log"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "123456")

process_lock = threading.Lock()
process = None

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"ok": False, "error": "Please log in."}), 401
        return fn(*args, **kwargs)
    return wrapper

def running():
    global process
    with process_lock:
        if process is not None and process.poll() is None:
            return True
        process = None
        return False

@app.get("/")
def home():
    return render_template("index.html", authenticated=bool(session.get("authenticated")))

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    if str(data.get("password", "")) != DASHBOARD_PASSWORD:
        return jsonify({"ok": False, "error": "Incorrect password."}), 403
    session["authenticated"] = True
    return jsonify({"ok": True})

@app.post("/api/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/status")
@login_required
def status():
    active = running()
    return jsonify({
        "running": active,
        "pid": process.pid if active else None,
        "launcher_exists": RUN_FILE.is_file()
    })

@app.post("/api/start")
@login_required
def start():
    global process
    if not RUN_FILE.is_file():
        return jsonify({"ok": False, "error": "run.py is missing from the project folder."}), 400
    with process_lock:
        if process is not None and process.poll() is None:
            return jsonify({"ok": False, "error": "Guildglory is already running."}), 409
        with open(LOG_FILE, "a", encoding="utf-8", buffering=1) as log:
            log.write(f"\n--- Started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            process = subprocess.Popen(
                [sys.executable, "-u", str(RUN_FILE)],
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
                text=True,
                bufsize=1
            )
    return jsonify({"ok": True, "message": "Guildglory launcher started.", "pid": process.pid})

@app.post("/api/stop")
@login_required
def stop():
    global process
    with process_lock:
        if process is None or process.poll() is not None:
            process = None
            return jsonify({"ok": False, "error": "Guildglory is not running."}), 409
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process = None
    return jsonify({"ok": True, "message": "Guildglory stopped."})

@app.post("/api/restart")
@login_required
def restart():
    global process
    if not RUN_FILE.is_file():
        return jsonify({"ok": False, "error": "run.py is missing from the project folder."}), 400

    with process_lock:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process = None

        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        log.write(f"\\n--- Restarted {time.strftime('%Y-%m-%d %H:%M:%S')} ---\\n")
        process = subprocess.Popen(
            [sys.executable, "-u", str(RUN_FILE)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
            text=True,
            bufsize=1
        )

    return jsonify({"ok": True, "message": "Guildglory restarted.", "pid": process.pid})

@app.post("/api/send-command")
@login_required
def send_command():
    data = request.get_json(silent=True) or {}
    command_text = str(data.get("command", "")).strip()
    if not command_text:
        return jsonify({"ok": False, "error": "Enter a command to send."}), 400
    if len(command_text) > 1000 or "\n" in command_text or "\r" in command_text:
        return jsonify({"ok": False, "error": "Command must be a single line of at most 1000 characters."}), 400

    with process_lock:
        if process is None or process.poll() is not None:
            return jsonify({"ok": False, "error": "Guildglory is not running."}), 409
        if process.stdin is None or process.stdin.closed:
            return jsonify({"ok": False, "error": "The running process does not accept input."}), 400
        try:
            process.stdin.write(command_text + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return jsonify({"ok": False, "error": "Could not send command; the process input is closed."}), 409

    return jsonify({"ok": True, "message": "Command sent to Guildglory. Check console output for its response."})

@app.get("/api/logs")
@login_required
def logs():
    if not LOG_FILE.exists():
        return jsonify({"logs": "No logs yet."})
    return jsonify({"logs": LOG_FILE.read_text(encoding="utf-8", errors="replace")[-30000:]})

# Safe, restricted web terminal. Only exact allowlisted commands are accepted.
# No shell=True, pipes, redirects, chaining, or arbitrary command execution.
ALLOWED_COMMANDS = {
    ("pwd",): ["pwd"],
    ("ls",): ["ls"],
    ("ls", "-la"): ["ls", "-la"],
    ("python", "--version"): [sys.executable, "--version"],
    ("pip", "--version"): [sys.executable, "-m", "pip", "--version"],
    ("pip", "list"): [sys.executable, "-m", "pip", "list"],
    ("git", "status", "--short"): ["git", "status", "--short"],
    ("uname", "-a"): ["uname", "-a"],
    ("whoami",): ["whoami"],
    ("date",): ["date"],
    ("df", "-h"): ["df", "-h"],
}

@app.post("/api/command")
@login_required
def command():
    data = request.get_json(silent=True) or {}
    raw = str(data.get("command", "")).strip()
    if not raw:
        return jsonify({"ok": False, "error": "Enter a command."}), 400
    if len(raw) > 200:
        return jsonify({"ok": False, "error": "Maximum command length is 200 characters."}), 400
    try:
        parts = tuple(shlex.split(raw))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"Invalid command syntax: {exc}"}), 400

    argv = ALLOWED_COMMANDS.get(parts)
    if argv is None:
        allowed = ", ".join(" ".join(item) for item in ALLOWED_COMMANDS)
        return jsonify({"ok": False, "error": "Command not allowed. Available: " + allowed}), 400

    try:
        result = subprocess.run(
            argv,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=10,
            env=os.environ.copy()
        )
        return jsonify({
            "ok": result.returncode == 0,
            "output": (result.stdout or "(no output)")[-12000:],
            "returncode": result.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Command timed out after 10 seconds."}), 408

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))