import os
import re
import sys
import time
import platform
from pathlib import Path

def enable_vt():
    if platform.system().lower() != "windows":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)) == 0:
            return
        k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass

_prev_lines = 0

def draw(block: str):
    global _prev_lines
    block = block.rstrip("\n")
    lines = block.count("\n") + 1
    if _prev_lines:
        sys.stdout.write(f"\x1b[{_prev_lines}F")
    sys.stdout.write("\x1b[0J")
    sys.stdout.write(block + "\n")
    sys.stdout.flush()
    _prev_lines = lines

def steam_root():
    if platform.system().lower() != "windows":
        return None
    import winreg
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for root, sub, val in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                p = Path(str(winreg.QueryValueEx(k, val)[0]))
                if (p / "steamapps").exists():
                    return p
        except OSError:
            pass
    return None


def library_roots(steam_dir: Path):
    roots = {steam_dir}
    vdf = steam_dir / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        txt = vdf.read_text("utf-8", errors="ignore")
        for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
            p = Path(m.group(1))
            if (p / "steamapps").exists():
                roots.add(p)
    return list(roots)


def find_library_for_appid(libs, appid: str):
    for lib in libs:
        if (lib / "steamapps" / f"appmanifest_{appid}.acf").exists():
            return lib
    return None


def game_name(lib: Path, appid: str):
    mf = lib / "steamapps" / f"appmanifest_{appid}.acf"
    if not mf.exists():
        return f"appid {appid}"
    t = mf.read_text("utf-8", errors="ignore")
    m = re.search(r'"name"\s*"([^"]+)"', t)
    return m.group(1).strip() if m else f"appid {appid}"

def tail_lines(p: Path, max_kb=1024):
    if not p.exists():
        return []
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        n = min(f.tell(), max_kb * 1024)
        f.seek(-n, os.SEEK_END)
        return f.read().decode("utf-8", errors="ignore").splitlines()


_RX_CUR_RATE = re.compile(r"Current download rate:\s*([\d.]+)\s*(Mbps|MB/s|KB/s)", re.IGNORECASE)
_RX_APPID = re.compile(r"AppID\s+(\d+)", re.IGNORECASE)


def parse_rate_to_bps(val: float, unit: str) -> float:
    u = unit.lower()
    if u == "kb/s":
        return val * 1024
    if u == "mb/s":
        return val * 1024**2
    if u == "mbps":
        return (val * 1_000_000) / 8
    return 0.0


def fmt_bps(bps: float):
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    v, i = float(bps), 0
    while v >= 1024 and i < 3:
        v /= 1024
        i += 1
    return f"{v:.2f} {units[i]}"


def parse_status_from_line(line_lower: str) -> str:
    if "suspended" in line_lower:
        return "Приостановка"
    if "none" in line_lower:
        return "Нет активности"
    if "stopping" in line_lower:
        return "Остановлено"
    if "verif" in line_lower:
        return "Проверка"
    if "pause" in line_lower:
        return "Пауза"
    if "preallocating" in line_lower or "reconfiguring" in line_lower:
        return "Подготовка файлов"
    if "staging" in line_lower and "downloading" not in line_lower:
        return "Подготовка файлов"
    if "downloading" in line_lower:
        return "Загрузка"
    if "running update" in line_lower or "update running" in line_lower:
        return "Обновление статуса"
    return "Неизвестно"


def current_activity_from_log(steam_dir: Path, tail_kb=2048, lookback_lines=260):
    log = steam_dir / "logs" / "content_log.txt"
    lines = tail_lines(log, max_kb=tail_kb)
    if not lines:
        return None, "Неизвестно", None, None

    rate_idx = None
    speed_bps = None
    for i in range(len(lines) - 1, -1, -1):
        m = _RX_CUR_RATE.search(lines[i])
        if m:
            rate_idx = i
            speed_bps = parse_rate_to_bps(float(m.group(1)), m.group(2))
            break

    anchor = rate_idx if rate_idx is not None else (len(lines) - 1)
    start = max(0, anchor - lookback_lines)

    active_appid = None
    active_score = -1
    active_line = None

    for j in range(anchor, start - 1, -1):
        l = lines[j].lower()
        if "appid" not in l:
            continue
        m = _RX_APPID.search(lines[j])
        if not m:
            continue
        appid = m.group(1)

        is_state_line = ("app update changed" in l) or ("update canceled" in l) or ("state changed" in l) or ("update started" in l)
        if not is_state_line:
            continue

        score = 0
        if "app update changed" in l:
            score += 5
        if "downloading" in l:
            score += 4
        if "staging" in l or "preallocating" in l or "reconfiguring" in l:
            score += 2
        if "update started" in l:
            score += 1

        if "suspended" in l:
            score -= 10
        if " none" in l or l.endswith(" none") or "app update changed : none" in l:
            score -= 6
        if "stopping" in l:
            score -= 4

        if score >= active_score:
            active_score = score
            active_appid = appid
            active_line = lines[j]

    if not active_appid:
        for j in range(anchor, start - 1, -1):
            m = _RX_APPID.search(lines[j])
            if m:
                active_appid = m.group(1)
                active_line = lines[j]
                break

    if not active_appid:
        return None, "Неизвестно", speed_bps, None

    status = "Неизвестно"
    status_src = None
    for j in range(len(lines) - 1, start - 1, -1):
        if active_appid not in lines[j]:
            continue
        l = lines[j].lower()
        if ("update canceled" in l) or ("state changed" in l) or ("app update changed" in l) or ("pause" in l) or ("verif" in l):
            status = parse_status_from_line(l)
            status_src = lines[j]
            break

    if status == "Неизвестно" and active_line:
        status = parse_status_from_line(active_line.lower())
        status_src = active_line

    return active_appid, status, speed_bps, status_src

def parse_args():
    watch = "--watch" in sys.argv
    ticks = int(sys.argv[sys.argv.index("--ticks") + 1]) if "--ticks" in sys.argv else 5
    interval = int(sys.argv[sys.argv.index("--interval") + 1]) if "--interval" in sys.argv else 60
    return watch, ticks, interval

def main():
    enable_vt()
    watch, ticks, interval = parse_args()

    steam_dir = steam_root()
    if not steam_dir:
        print("Steam не обнаружен")
        return 2

    libs = library_roots(steam_dir)

    t0 = time.monotonic()
    i = 0

    try:
        while watch or i < ticks:
            appid, st, bps, st_line = current_activity_from_log(steam_dir)

            if appid:
                lib = find_library_for_appid(libs, appid) or steam_dir
                name = game_name(lib, appid)
            else:
                name = "-"

            speed_text = "-" if bps is None else fmt_bps(bps)
            block = (
                f"Каталог : {steam_dir}\n"
                f"ID      : {appid or '-'}\n"
                f"Игра    : {name}\n"
                f"Статус  : {st}\n"
                f"Скорость: {speed_text}\n"
                f"Итерации: {i + 1} | Аптайм: {int(time.monotonic() - t0)}с"
            )
            draw(block)

            i += 1
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())