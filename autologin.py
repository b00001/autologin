"""
Campus Wi-Fi Auto-Login
Usage:
  python autologin.py           # interactive terminal menu (default)
  python autologin.py --daemon  # daemon loop, no menu
  python autologin.py --once    # login once and exit
  python autologin.py --status  # print Wi-Fi/login state and exit
  python autologin.py --setup   # interactive config wizard
  python autologin.py --log     # show the last log lines and exit
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Windows console: enable ANSI escape sequences and UTF-8 output so the
# colours, box drawing and Thai menu labels render correctly.
if os.name == "nt":
    os.system("")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

CONFIG_PATH = Path(__file__).parent / "config.json"


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"{RED}config.json not found. Run: python autologin.py --setup{RESET}")
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print(f"{GREEN}config.json saved to {CONFIG_PATH}{RESET}")


# ── Logging ───────────────────────────────────────────────────────────────────

_log_path: Path | None = None
_last_log_msg: str | None = None


def set_log_path(cfg: dict) -> None:
    """Point the logger at the file named in the config (relative to this script)."""
    global _log_path
    log_file = cfg.get("log_file")
    _log_path = (Path(__file__).parent / log_file) if log_file else None


def _log(msg: str) -> None:
    global _last_log_msg
    if _log_path and msg != _last_log_msg:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        _last_log_msg = msg


def status(colour: str, symbol: str, msg: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"{colour}[{timestamp}] {symbol} {msg}{RESET}")
    _log(msg)


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(title: str, message: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        from plyer import notification
        notification.notify(title=title, message=message,
                            app_name="Campus AutoLogin", timeout=5)
    except Exception:
        pass  # non-fatal if plyer or notification daemon is unavailable


# ── Wi-Fi helpers ─────────────────────────────────────────────────────────────

def get_current_ssid() -> str | None:
    """Return the SSID of the currently connected Wi-Fi, or None."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            # Match "    SSID                   : MyNetwork"
            # Skip "    BSSID                  : aa:bb:..."
            m = re.match(r"\s+SSID\s*:\s*(.+)", line)
            if m and "BSSID" not in line:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def connect_to_ssid(ssid: str) -> bool:
    """Ask Windows to connect to a saved Wi-Fi profile. Returns True if the
    command succeeded (does not guarantee a DHCP lease yet)."""
    status(YELLOW, "⟳", f"Connecting to '{ssid}'...")
    try:
        result = subprocess.run(
            ["netsh", "wlan", "connect", f"name={ssid}"],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout.strip()
        if "successfully" in output.lower():
            status(GREEN, "✓", f"Join request accepted for '{ssid}'")
            return True
        elif "profile" in output.lower() and "not found" in output.lower():
            status(RED, "✗",
                   f"No saved Windows profile for '{ssid}'. "
                   "Connect once manually via Windows Wi-Fi settings, then retry.")
        else:
            status(RED, "✗", f"netsh connect output: {output}")
    except Exception as exc:
        status(RED, "✗", f"Failed to call netsh: {exc}")
    return False


# ── Login helpers ─────────────────────────────────────────────────────────────

def build_session(cfg: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
            "Gecko/20100101 Firefox/152.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Origin": cfg["portal_origin"],
        "Referer": cfg["portal_url"],
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def is_internet_up(session: requests.Session) -> bool:
    """Return True if we have real internet (HTTP 204 probe)."""
    try:
        resp = session.get(
            "http://clients1.google.com/generate_204",
            timeout=5, allow_redirects=False
        )
        return resp.status_code == 204
    except Exception:
        return False


def login_succeeded(response: requests.Response, cfg: dict) -> bool:
    check = cfg.get("success_check", {})
    check_type = check.get("type", "url_not_contains")
    value = check.get("value", "webAuth")

    if check_type == "url_not_contains":
        return value not in response.url
    if check_type == "url_contains":
        return value in response.url
    if check_type == "status_code":
        return response.status_code == int(value)
    return False


def attempt_login(session: requests.Session, cfg: dict) -> bool:
    username = cfg["credentials"]["username"]
    password = cfg["credentials"]["password"]
    portal_url = cfg["portal_url"]
    max_attempts = cfg["retry"]["attempts"]
    backoff = cfg["retry"]["backoff_seconds"]

    data = {
        "une":       username,
        "username":  username,
        "pass_wd":   password,
        "pass_word": password,
    }

    for attempt in range(1, max_attempts + 1):
        status(CYAN, "⟳", f"Login attempt {attempt}/{max_attempts} → {portal_url}")
        try:
            resp = session.post(portal_url, data=data, timeout=10)
            if login_succeeded(resp, cfg):
                status(GREEN, "✓", "Login successful")
                notify("Connected", "Campus Wi-Fi login successful",
                       cfg.get("notifications", True))
                return True
            status(YELLOW, "!", f"Login failed (HTTP {resp.status_code}, url={resp.url})")
        except requests.RequestException as exc:
            status(RED, "✗", f"Request error: {exc}")

        if attempt < max_attempts:
            wait = backoff * attempt
            status(YELLOW, "…", f"Retrying in {wait}s")
            time.sleep(wait)

    status(RED, "✗", "All login attempts failed")
    notify("Login failed", "Check credentials in config.json",
           cfg.get("notifications", True))
    return False


# ── CLI modes ─────────────────────────────────────────────────────────────────

def cmd_status(cfg: dict) -> None:
    ssid = get_current_ssid()
    target = cfg["ssid"]
    if ssid is None:
        print(f"{RED}Not connected to any Wi-Fi{RESET}")
        return
    if ssid != target:
        print(f"{YELLOW}Connected to '{ssid}' (target: '{target}'){RESET}")
        return
    session = build_session(cfg)
    up = is_internet_up(session)
    if up:
        print(f"{GREEN}Connected to '{ssid}' — internet is up (authenticated){RESET}")
    else:
        print(f"{YELLOW}Connected to '{ssid}' — internet blocked (need login){RESET}")


def do_once(cfg: dict) -> bool:
    """Join the target SSID if needed and log in. Returns True on success."""
    session = build_session(cfg)
    target = cfg["ssid"]
    ssid = get_current_ssid()

    if ssid != target:
        if not connect_to_ssid(target):
            return False
        time.sleep(5)

    if is_internet_up(session):
        status(GREEN, "✓", f"Already authenticated on '{target}' — nothing to do")
        return True

    return attempt_login(session, cfg)


def cmd_once(cfg: dict) -> None:
    sys.exit(0 if do_once(cfg) else 1)


def cmd_daemon(cfg: dict) -> None:
    session = build_session(cfg)
    target = cfg["ssid"]
    poll = cfg.get("poll_interval", 10)

    print(f"{CYAN}Auto-login daemon started. Target SSID: '{target}'. "
          f"Poll every {poll}s. Press Ctrl+C to stop.{RESET}")

    try:
        while True:
            ssid = get_current_ssid()

            if ssid == target:
                if is_internet_up(session):
                    status(GREEN, "✓", f"Connected to '{target}' — authenticated")
                else:
                    status(YELLOW, "!", f"On '{target}' but not authenticated — logging in")
                    attempt_login(session, cfg)
            else:
                if ssid is None:
                    status(RED, "✗", "Not connected to any Wi-Fi — attempting to join target")
                else:
                    status(YELLOW, "!", f"On '{ssid}', not target '{target}' — switching")
                connected = connect_to_ssid(target)
                if connected:
                    time.sleep(5)  # wait for DHCP
                    attempt_login(session, cfg)

            time.sleep(poll)

    except KeyboardInterrupt:
        print(f"\n{CYAN}Stopped.{RESET}")


def cmd_setup() -> None:
    print(f"{CYAN}=== Campus Wi-Fi Auto-Login Setup ==={RESET}")
    print("Press Enter to keep the default shown in [brackets].\n")

    defaults = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            defaults = json.load(f)

    def ask(prompt: str, default: str) -> str:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default

    ssid         = ask("Campus Wi-Fi SSID", defaults.get("ssid", ""))
    portal_url   = ask("Portal login URL", defaults.get("portal_url", "http://10.99.92.1/webAuth/"))
    portal_origin = ask("Portal origin (base URL)", defaults.get("portal_origin", "http://10.99.92.1"))
    username     = ask("Student ID / username", defaults.get("credentials", {}).get("username", ""))
    password     = getpass.getpass("Password (input hidden): ").strip()
    if not password:
        password = defaults.get("credentials", {}).get("password", "")

    poll = ask("Poll interval in seconds", str(defaults.get("poll_interval", 10)))
    log_file = ask("Log file path", defaults.get("log_file", "autologin.log"))
    notifs = ask("Enable desktop notifications? (true/false)",
                 str(defaults.get("notifications", True)).lower())

    cfg = {
        "ssid": ssid,
        "poll_interval": int(poll),
        "portal_url": portal_url,
        "portal_origin": portal_origin,
        "credentials": {"username": username, "password": password},
        "success_check": {"type": "url_not_contains", "value": "webAuth"},
        "retry": {"attempts": 3, "backoff_seconds": 5},
        "log_file": log_file,
        "notifications": notifs.lower() == "true",
    }
    save_config(cfg)
    print(f"\n{GREEN}Setup complete! Run: python autologin.py{RESET}")


# ── Interactive menu ──────────────────────────────────────────────────────────

def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _pause() -> None:
    input(f"\n{CYAN}Enter เพื่อกลับ...{RESET}")


def _read_key() -> str:
    """Block for one keypress. Returns 'up' | 'down' | 'enter' | 'esc' | a char."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):                  # arrow / function key
            return {b"H": "up", b"P": "down"}.get(msvcrt.getch(), "")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x1b":
            return "esc"
        if ch == b"\x03":
            raise KeyboardInterrupt
        return ch.decode("utf-8", "ignore").lower()

    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down"}.get(seq, "esc")
        if ch in ("\r", "\n"):
            return "enter"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def select(title: str, subtitle: str, options: list[str]) -> int:
    """Arrow-key menu. Returns the chosen index, or -1 if cancelled."""
    idx = 0
    while True:
        _clear_screen()
        print(f"\n  {CYAN}{title}{RESET}")
        if subtitle:
            print(f"  {subtitle}")
        print()
        for i, opt in enumerate(options):
            print(f"  {GREEN}▸ {opt}{RESET}" if i == idx else f"    {opt}")
        print(f"\n  {CYAN}↑↓ เลื่อน · Enter เลือก · Esc ออก{RESET}")

        key = _read_key()
        if key == "up":
            idx = (idx - 1) % len(options)
        elif key == "down":
            idx = (idx + 1) % len(options)
        elif key == "enter":
            return idx
        elif key == "esc":
            return -1
        elif key.isdigit() and 1 <= int(key) <= len(options):
            return int(key) - 1


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _confirm(prompt: str) -> bool:
    return input(f"{YELLOW}{prompt} (y/N): {RESET}").strip().lower() in ("y", "yes")


def _mask(secret: str) -> str:
    return "*" * len(secret) if secret else "(ว่าง)"


# ── Log viewer ────────────────────────────────────────────────────────────────

_OK_WORDS  = ("successful", "authenticated", "accepted")
_BAD_WORDS = ("failed", "error", "not found", "✗")


def _log_lines() -> list[str]:
    if not _log_path or not _log_path.exists():
        return []
    with _log_path.open(encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _colour_log_line(line: str) -> str:
    low = line.lower()
    if any(w in low for w in _OK_WORDS):
        return f"{GREEN}{line}{RESET}"
    if any(w in low for w in _BAD_WORDS):
        return f"{RED}{line}{RESET}"
    return line


def _last_check_line() -> str:
    """One-line summary of the most recent log entry, for the menu header."""
    if not _log_path or not _log_path.exists():
        return f"{YELLOW}ยังไม่มีบันทึกการตรวจ{RESET}"
    try:
        with _log_path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - 4096, 0))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return f"{YELLOW}อ่านไฟล์ log ไม่ได้{RESET}"
    if not tail:
        return f"{YELLOW}ยังไม่มีบันทึกการตรวจ{RESET}"

    m = re.match(r"\[(.+?)\]\s*(.*)", tail[-1])
    when, msg = (m.group(1), m.group(2)) if m else ("", tail[-1])
    try:
        when = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M:%S")
    except ValueError:
        pass
    low = msg.lower()
    colour = (GREEN if any(w in low for w in _OK_WORDS)
              else RED if any(w in low for w in _BAD_WORDS)
              else YELLOW)
    return f"ตรวจล่าสุด {when} · {colour}{msg}{RESET}"


def _print_log(lines: list[str]) -> None:
    if not lines:
        print(f"{YELLOW}ไม่พบข้อมูลที่ตรงเงื่อนไข{RESET}")
        return
    for line in lines:
        print(_colour_log_line(line))
    print(f"\n{CYAN}— แสดง {len(lines)} บรรทัด —{RESET}")


def _log_stats() -> None:
    lines = _log_lines()
    if not lines:
        print(f"{YELLOW}log ยังว่างอยู่{RESET}")
        return

    ok      = sum(1 for l in lines if "Login successful" in l)
    failed  = sum(1 for l in lines if "All login attempts failed" in l)
    retried = sum(1 for l in lines if "Login failed (HTTP" in l)
    joins   = sum(1 for l in lines if "Join request accepted" in l)
    errors  = sum(1 for l in lines if "Request error" in l)

    size_kb = _log_path.stat().st_size / 1024 if _log_path else 0

    print(f"  ไฟล์            : {_log_path}")
    print(f"  ขนาด            : {size_kb:,.1f} KB")
    print(f"  จำนวนบรรทัด      : {len(lines):,}")
    print(f"  บรรทัดแรก        : {lines[0]}")
    print(f"  บรรทัดล่าสุด     : {lines[-1]}")
    print()
    print(f"  {GREEN}ล็อกอินสำเร็จ     : {ok:,} ครั้ง{RESET}")
    print(f"  {RED}ล็อกอินล้มเหลว    : {failed:,} ครั้ง (ครบทุก attempt){RESET}")
    print(f"  {YELLOW}attempt ที่พลาด   : {retried:,} ครั้ง{RESET}")
    print(f"  {YELLOW}request error    : {errors:,} ครั้ง{RESET}")
    print(f"  เข้าร่วม Wi-Fi     : {joins:,} ครั้ง")


def menu_log() -> None:
    global _last_log_msg
    options = [
        "20 บรรทัดล่าสุด",
        "50 บรรทัดล่าสุด",
        "ค้นหาคำใน log",
        "เฉพาะบรรทัดที่ผิดพลาด",
        "สรุปสถิติ",
        "ตามดูแบบเรียลไทม์",
        "ล้างไฟล์ log",
        "กลับ",
    ]
    while True:
        choice = select("ตรวจสอบ Log", _last_check_line(), options)
        print()

        if choice in (-1, 7):
            return
        if not _log_path:
            print(f"{RED}config ไม่ได้ตั้งค่า log_file ไว้{RESET}")
            _pause()
            return

        if choice == 0:
            _print_log(_log_lines()[-20:])
        elif choice == 1:
            _print_log(_log_lines()[-50:])
        elif choice == 2:
            keyword = _ask("ค้นหา")
            if not keyword:
                continue
            hits = [l for l in _log_lines() if keyword.lower() in l.lower()]
            print(f"\n{CYAN}พบ {len(hits)} บรรทัด (แสดง 100 ล่าสุด){RESET}\n")
            _print_log(hits[-100:])
        elif choice == 3:
            bad = [l for l in _log_lines() if any(w in l.lower() for w in _BAD_WORDS)]
            print(f"{CYAN}พบ {len(bad)} บรรทัด (แสดง 100 ล่าสุด){RESET}\n")
            _print_log(bad[-100:])
        elif choice == 4:
            _log_stats()
        elif choice == 5:
            _tail_follow()
        elif choice == 6:
            if not _log_path.exists():
                print(f"{YELLOW}ยังไม่มีไฟล์ log{RESET}")
            elif _confirm(f"ลบข้อมูลทั้งหมดใน {_log_path.name} ?"):
                _log_path.write_text("", encoding="utf-8")
                _last_log_msg = None
                print(f"{GREEN}ล้าง log เรียบร้อย{RESET}")
            else:
                print("ยกเลิก")

        _pause()


def _tail_follow() -> None:
    """Print new log lines as they are appended, until Ctrl+C."""
    if not _log_path or not _log_path.exists():
        print(f"{YELLOW}ยังไม่มีไฟล์ log{RESET}")
        return
    print(f"{CYAN}กำลังตามดู {_log_path.name} — Ctrl+C เพื่อหยุด{RESET}\n")
    try:
        with _log_path.open(encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines()[-10:]:
                print(_colour_log_line(line))
            f.seek(0, 2)  # jump to EOF
            while True:
                line = f.readline()
                if line:
                    print(_colour_log_line(line.rstrip("\n")))
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{CYAN}หยุดตามดู log{RESET}")


# ── Config editor ─────────────────────────────────────────────────────────────

def _config_options(cfg: dict) -> list[str]:
    creds = cfg.get("credentials", {})
    retry = cfg.get("retry", {})
    check = cfg.get("success_check", {})
    return [
        f"SSID            {cfg.get('ssid', '')}",
        f"Portal URL      {cfg.get('portal_url', '')}",
        f"Portal origin   {cfg.get('portal_origin', '')}",
        f"Username        {creds.get('username', '')}",
        f"Password        {_mask(creds.get('password', ''))}",
        f"Poll interval   {cfg.get('poll_interval', 10)} วินาที",
        f"Retry attempts  {retry.get('attempts', 3)}",
        f"Retry backoff   {retry.get('backoff_seconds', 5)} วินาที",
        f"Log file        {cfg.get('log_file', '')}",
        f"Notifications   {cfg.get('notifications', False)}",
        f"Success check   {check.get('type', '')} = {check.get('value', '')}",
        "ตั้งค่าใหม่ทั้งหมด (wizard)",
        "กลับ",
    ]


def _edit_int(cfg_section: dict, key: str, label: str, default: int) -> None:
    raw = _ask(label, str(cfg_section.get(key, default)))
    try:
        cfg_section[key] = int(raw)
    except ValueError:
        print(f"{RED}ต้องเป็นตัวเลข — ไม่มีการเปลี่ยนแปลง{RESET}")


def menu_config(cfg: dict) -> dict:
    while True:
        cfg.setdefault("credentials", {})
        cfg.setdefault("retry", {})
        cfg.setdefault("success_check", {})

        choice = select("ตั้งค่า Config", "แก้แล้วบันทึกทันที", _config_options(cfg))
        print()

        if choice in (-1, 12):
            return cfg
        elif choice == 0:
            cfg["ssid"] = _ask("SSID", cfg.get("ssid", ""))
        elif choice == 1:
            cfg["portal_url"] = _ask("Portal URL", cfg.get("portal_url", ""))
        elif choice == 2:
            cfg["portal_origin"] = _ask("Portal origin", cfg.get("portal_origin", ""))
        elif choice == 3:
            cfg["credentials"]["username"] = _ask(
                "Username", cfg["credentials"].get("username", ""))
        elif choice == 4:
            pw = getpass.getpass("Password ใหม่ (Enter = ไม่เปลี่ยน): ").strip()
            if pw:
                cfg["credentials"]["password"] = pw
            else:
                print("ไม่เปลี่ยนรหัสผ่าน")
        elif choice == 5:
            _edit_int(cfg, "poll_interval", "Poll interval (วินาที)", 10)
        elif choice == 6:
            _edit_int(cfg["retry"], "attempts", "Retry attempts", 3)
        elif choice == 7:
            _edit_int(cfg["retry"], "backoff_seconds", "Retry backoff (วินาที)", 5)
        elif choice == 8:
            cfg["log_file"] = _ask("Log file", cfg.get("log_file", "autologin.log"))
            set_log_path(cfg)
        elif choice == 9:
            cfg["notifications"] = not cfg.get("notifications", False)
            print(f"Notifications = {cfg['notifications']}")
        elif choice == 10:
            print("ชนิด: url_not_contains | url_contains | status_code")
            cfg["success_check"]["type"] = _ask(
                "ชนิด", cfg["success_check"].get("type", "url_not_contains"))
            cfg["success_check"]["value"] = _ask(
                "ค่า", str(cfg["success_check"].get("value", "webAuth")))
        elif choice == 11:
            cmd_setup()
            cfg = load_config()
            set_log_path(cfg)
            _pause()
            continue

        save_config(cfg)
        _pause()


# ── Main menu ─────────────────────────────────────────────────────────────────

def menu_main(cfg: dict) -> None:
    options = [
        "ตรวจสอบสถานะ",
        "ล็อกอินทันที",
        "เฝ้าอัตโนมัติ (daemon)",
        "ดู log",
        "ตั้งค่า",
        "ออก",
    ]
    while True:
        choice = select("Campus Wi-Fi Auto-Login", _last_check_line(), options)
        print()

        if choice in (-1, 5):
            return
        elif choice == 0:
            cmd_status(cfg)
            _pause()
        elif choice == 1:
            ok = do_once(cfg)
            print(f"\n{GREEN}ล็อกอินสำเร็จ{RESET}" if ok
                  else f"\n{RED}ล็อกอินไม่สำเร็จ{RESET}")
            _pause()
        elif choice == 2:
            cmd_daemon(cfg)
            _pause()
        elif choice == 3:
            menu_log()
        elif choice == 4:
            cfg = menu_config(cfg)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Campus Wi-Fi captive-portal auto-login")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--menu",   action="store_true", help="Interactive terminal menu (default)")
    group.add_argument("--daemon", action="store_true", help="Run the watch loop without the menu")
    group.add_argument("--once",   action="store_true", help="Login once and exit")
    group.add_argument("--status", action="store_true", help="Print Wi-Fi/login state and exit")
    group.add_argument("--setup",  action="store_true", help="Interactive config wizard")
    group.add_argument("--log",    action="store_true", help="Print the last log lines and exit")
    parser.add_argument("-n", type=int, default=20, metavar="LINES",
                        help="Number of lines for --log (default: 20)")
    args = parser.parse_args()

    if args.setup:
        cmd_setup()
        return

    if not CONFIG_PATH.exists():
        print(f"{YELLOW}ยังไม่มี config.json — เริ่มตั้งค่าครั้งแรก{RESET}")
        cmd_setup()

    cfg = load_config()
    set_log_path(cfg)

    if args.status:
        cmd_status(cfg)
    elif args.once:
        cmd_once(cfg)
    elif args.daemon:
        cmd_daemon(cfg)
    elif args.log:
        _print_log(_log_lines()[-max(args.n, 1):])
    else:
        menu_main(cfg)


if __name__ == "__main__":
    main()
