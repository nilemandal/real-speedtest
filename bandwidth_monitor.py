#!/usr/bin/env python3
"""
Bandwidth Monitor — Real-time bandwidth meter with alerts and usage logging.

Features:
  - Live upload/download speed tracking (per-second sampling)
  - Periodic actual internet speed tests (via speedtest-cli)
  - Configurable alerts when speed drops below thresholds
  - SQLite-backed usage history with reporting
  - macOS desktop notifications for alerts
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_INTERFACE = "en0"  # Wi-Fi on macOS
DEFAULT_DB_PATH = Path(__file__).parent / "bandwidth_usage.db"
DEFAULT_CONFIG_PATH = Path(__file__).parent / "bandwidth_config.json"
DEFAULT_ALERT_DOWN_MBPS = 5.0   # alert if download < 5 Mbps
DEFAULT_ALERT_UP_MBPS = 1.0     # alert if upload < 1 Mbps
SPEEDTEST_INTERVAL_MIN = 15     # run a real speed test every N minutes
SAMPLE_INTERVAL_SEC = 1         # poll interface counters every N seconds


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_samples (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            down_mbps  REAL    NOT NULL,
            up_mbps    REAL    NOT NULL,
            sample_type TEXT   NOT NULL DEFAULT 'interface'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speed_tests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            down_mbps  REAL    NOT NULL,
            up_mbps    REAL    NOT NULL,
            ping_ms    REAL,
            server     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            message    TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


def store_sample(conn, down_mbps, up_mbps, sample_type="interface"):
    conn.execute(
        "INSERT INTO usage_samples (ts, down_mbps, up_mbps, sample_type) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), down_mbps, up_mbps, sample_type),
    )
    conn.commit()


def store_speedtest(conn, down_mbps, up_mbps, ping_ms, server):
    conn.execute(
        "INSERT INTO speed_tests (ts, down_mbps, up_mbps, ping_ms, server) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), down_mbps, up_mbps, ping_ms, server),
    )
    conn.commit()


def store_alert(conn, message):
    conn.execute(
        "INSERT INTO alerts (ts, message) VALUES (?, ?)",
        (datetime.now().isoformat(), message),
    )
    conn.commit()


# ── macOS Notification ────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """Send a macOS desktop notification."""
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Basso"',
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # notifications are best-effort


# ── Interface bandwidth sampling ─────────────────────────────────────────────

class BandwidthSampler:
    def __init__(self, interface: str):
        self.interface = interface
        counters = psutil.net_io_counters(pernic=True).get(interface)
        if counters is None:
            raise ValueError(f"Interface '{interface}' not found. Available: "
                             f"{list(psutil.net_io_counters(pernic=True).keys())}")
        self.prev_recv = counters.bytes_recv
        self.prev_sent = counters.bytes_sent
        self.prev_time = time.monotonic()
        self.down_mbps = 0.0
        self.up_mbps = 0.0

    def sample(self):
        counters = psutil.net_io_counters(pernic=True).get(self.interface)
        if counters is None:
            return
        now = time.monotonic()
        dt = now - self.prev_time
        if dt <= 0:
            return

        recv_diff = counters.bytes_recv - self.prev_recv
        sent_diff = counters.bytes_sent - self.prev_sent

        self.down_mbps = (recv_diff * 8) / (dt * 1_000_000)  # bytes→bits→Mbps
        self.up_mbps = (sent_diff * 8) / (dt * 1_000_000)

        self.prev_recv = counters.bytes_recv
        self.prev_sent = counters.bytes_sent
        self.prev_time = now


# ── Per-process bandwidth tracking (macOS nettop) ────────────────────────────

class ProcessBandwidthTracker:
    """Tracks per-process network usage using macOS nettop command."""

    def __init__(self):
        self.processes = []  # list of (name, pid, down_mbps, up_mbps)
        self._lock = threading.Lock()

    def sample(self):
        """Run nettop for 2 samples (1s delta) and parse per-process bandwidth."""
        try:
            result = subprocess.run(
                ["nettop", "-P", "-L", "2", "-n", "-x", "-d", "-s", "1"],
                capture_output=True, text=True, timeout=5,
            )
            lines = result.stdout.strip().splitlines()

            # Find the second sample block (delta values)
            second_header = -1
            header_count = 0
            for i, line in enumerate(lines):
                if line.startswith("time,"):
                    header_count += 1
                    if header_count == 2:
                        second_header = i
                        break

            if second_header == -1:
                return

            procs = []
            for line in lines[second_header + 1:]:
                if line.startswith("time,"):
                    break
                parts = line.split(",")
                if len(parts) < 6:
                    continue

                name_pid = parts[1].strip()
                if not name_pid:
                    continue

                try:
                    bytes_in = int(parts[4]) if parts[4] else 0
                    bytes_out = int(parts[5]) if parts[5] else 0
                except (ValueError, IndexError):
                    continue

                if bytes_in == 0 and bytes_out == 0:
                    continue

                # Parse "ProcessName.PID" format
                dot_idx = name_pid.rfind(".")
                if dot_idx > 0:
                    name = name_pid[:dot_idx]
                    try:
                        pid = int(name_pid[dot_idx + 1:])
                    except ValueError:
                        name = name_pid
                        pid = 0
                else:
                    name = name_pid
                    pid = 0

                down_mbps = (bytes_in * 8) / 1_000_000  # per second (1s sample)
                up_mbps = (bytes_out * 8) / 1_000_000

                procs.append((name, pid, down_mbps, up_mbps))

            # Sort by total bandwidth (download + upload), descending
            procs.sort(key=lambda x: x[2] + x[3], reverse=True)

            with self._lock:
                self.processes = procs[:15]  # top 15

        except Exception:
            pass

    def get_processes(self):
        with self._lock:
            return list(self.processes)


# ── Speed test runner ─────────────────────────────────────────────────────────

class SpeedTestRunner:
    def __init__(self):
        self.last_down = None
        self.last_up = None
        self.last_ping = None
        self.last_server = None
        self.last_time = None
        self.running = False
        self.error = None

    # Test files: multiple servers for reliability
    DOWNLOAD_URLS = [
        ("https://proof.ovh.net/files/10Mb.dat", "OVH (France)"),
        ("https://speed.hetzner.de/10MB.bin", "Hetzner (Germany)"),
    ]
    UPLOAD_URL = "https://httpbin.org/post"

    def _ping(self, host: str = "8.8.8.8") -> float:
        """Measure ping to a host in ms."""
        import subprocess
        try:
            result = subprocess.run(
                ["ping", "-c", "3", "-q", host],
                capture_output=True, text=True, timeout=10,
            )
            # Parse avg from "min/avg/max/stddev = ..."
            for line in result.stdout.splitlines():
                if "avg" in line:
                    parts = line.split("=")[-1].strip().split("/")
                    return float(parts[1])
        except Exception:
            pass
        return 0.0

    def _download_test(self) -> tuple:
        """Test download speed, returns (mbps, server_name)."""
        import urllib.request
        best_mbps = 0.0
        best_server = None
        for url, server in self.DOWNLOAD_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "BandwidthMonitor/1.0"})
                start = time.monotonic()
                data = urllib.request.urlopen(req, timeout=20).read()
                elapsed = time.monotonic() - start
                mbps = (len(data) * 8) / (elapsed * 1_000_000)
                if mbps > best_mbps:
                    best_mbps = mbps
                    best_server = server
            except Exception:
                continue
        return best_mbps, best_server

    def _upload_test(self) -> float:
        """Test upload speed by POSTing data, returns mbps."""
        import urllib.request
        try:
            payload = b"0" * 2_000_000  # 2 MB
            req = urllib.request.Request(
                self.UPLOAD_URL,
                data=payload,
                headers={"User-Agent": "BandwidthMonitor/1.0", "Content-Type": "application/octet-stream"},
            )
            start = time.monotonic()
            urllib.request.urlopen(req, timeout=30)
            elapsed = time.monotonic() - start
            return (len(payload) * 8) / (elapsed * 1_000_000)
        except Exception:
            return 0.0

    def run(self):
        """Run a real internet speed test (blocking, ~30s)."""
        self.running = True
        self.error = None
        try:
            self.last_ping = self._ping()
            self.last_down, self.last_server = self._download_test()
            self.last_up = self._upload_test()
            if self.last_down == 0 and self.last_up == 0:
                self.error = "All test servers unreachable"
            else:
                self.last_time = datetime.now()
        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    defaults = {
        "alert_down_mbps": DEFAULT_ALERT_DOWN_MBPS,
        "alert_up_mbps": DEFAULT_ALERT_UP_MBPS,
        "speedtest_interval_min": SPEEDTEST_INTERVAL_MIN,
        "interface": DEFAULT_INTERFACE,
    }
    if path.exists():
        with open(path) as f:
            user = json.load(f)
        defaults.update(user)
    return defaults


def save_config(path: Path, cfg: dict):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(conn: sqlite3.Connection, hours: int, console: Console):
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    # Usage summary
    row = conn.execute(
        "SELECT AVG(down_mbps), AVG(up_mbps), MAX(down_mbps), MAX(up_mbps), COUNT(*) "
        "FROM usage_samples WHERE ts >= ?",
        (since,),
    ).fetchone()
    avg_d, avg_u, max_d, max_u, count = row

    t = Table(title=f"Usage Summary (last {hours}h)", show_lines=True)
    t.add_column("Metric", style="cyan")
    t.add_column("Download", style="green")
    t.add_column("Upload", style="blue")
    if count > 0:
        t.add_row("Average", f"{avg_d:.2f} Mbps", f"{avg_u:.2f} Mbps")
        t.add_row("Peak", f"{max_d:.2f} Mbps", f"{max_u:.2f} Mbps")
        t.add_row("Samples", str(count), str(count))
    else:
        t.add_row("No data", "-", "-")
    console.print(t)

    # Speed tests
    rows = conn.execute(
        "SELECT ts, down_mbps, up_mbps, ping_ms, server FROM speed_tests WHERE ts >= ? ORDER BY ts DESC LIMIT 10",
        (since,),
    ).fetchall()
    if rows:
        st = Table(title="Speed Tests", show_lines=True)
        st.add_column("Time", style="cyan")
        st.add_column("Down (Mbps)", style="green")
        st.add_column("Up (Mbps)", style="blue")
        st.add_column("Ping (ms)", style="yellow")
        st.add_column("Server")
        for r in rows:
            ts_str = datetime.fromisoformat(r[0]).strftime("%m/%d %H:%M")
            st.add_row(ts_str, f"{r[1]:.1f}", f"{r[2]:.1f}", f"{r[3]:.0f}" if r[3] else "-", r[4] or "-")
        console.print(st)

    # Alerts
    alerts = conn.execute(
        "SELECT ts, message FROM alerts WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
        (since,),
    ).fetchall()
    if alerts:
        at = Table(title="Alerts", show_lines=True)
        at.add_column("Time", style="red")
        at.add_column("Message", style="yellow")
        for a in alerts:
            ts_str = datetime.fromisoformat(a[0]).strftime("%m/%d %H:%M:%S")
            at.add_row(ts_str, a[1])
        console.print(at)
    else:
        console.print("[green]No alerts in this period.[/green]")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def speed_bar(mbps: float, max_mbps: float = 100.0, width: int = 30) -> Text:
    """Render a colored speed bar."""
    filled = int(min(mbps / max_mbps, 1.0) * width)
    if mbps < 2:
        color = "red"
    elif mbps < 10:
        color = "yellow"
    elif mbps < 50:
        color = "green"
    else:
        color = "bright_green"
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f"  {mbps:>7.2f} Mbps", style=f"bold {color}")
    return bar


def build_dashboard(sampler, speedtest_runner, cfg, alert_count, session_start, proc_tracker=None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="top_row", size=10),
        Layout(name="processes"),
        Layout(name="footer", size=5),
    )

    # Header
    elapsed = str(timedelta(seconds=int(time.time() - session_start)))
    layout["header"].update(
        Panel(
            Text(f"  Bandwidth Monitor  |  Interface: {cfg['interface']}  |  Uptime: {elapsed}", style="bold white"),
            style="blue",
        )
    )

    # Top row — live speeds + speedtest
    top_row = Layout()
    top_row.split_row(Layout(name="live", ratio=1), Layout(name="speedtest", ratio=1))

    # Live panel
    live_table = Table.grid(padding=(1, 2))
    live_table.add_row(Text("▼ Download", style="bold green"), speed_bar(sampler.down_mbps))
    live_table.add_row(Text("▲ Upload", style="bold blue"), speed_bar(sampler.up_mbps))
    top_row["live"].update(Panel(live_table, title="[bold]Real-time Interface Speed[/bold]", border_style="green"))

    # Speedtest panel
    if speedtest_runner.running:
        st_content = Text("  Running speed test...", style="yellow italic")
    elif speedtest_runner.error:
        st_content = Text(f"  Error: {speedtest_runner.error}", style="red")
    elif speedtest_runner.last_time:
        st_table = Table.grid(padding=(0, 2))
        st_table.add_row("Download:", Text(f"{speedtest_runner.last_down:.1f} Mbps", style="bold green"))
        st_table.add_row("Upload:", Text(f"{speedtest_runner.last_up:.1f} Mbps", style="bold blue"))
        st_table.add_row("Ping:", Text(f"{speedtest_runner.last_ping:.0f} ms", style="bold yellow"))
        st_table.add_row("Server:", Text(speedtest_runner.last_server or "-", style="dim"))
        st_table.add_row("Tested:", Text(speedtest_runner.last_time.strftime("%H:%M:%S"), style="dim"))
        st_content = st_table
    else:
        st_content = Text("  Waiting for first test...", style="dim")
    top_row["speedtest"].update(Panel(st_content, title="[bold]Internet Speed Test[/bold]", border_style="cyan"))

    layout["top_row"].update(top_row)

    # Per-process bandwidth table
    proc_table = Table(expand=True, pad_edge=True)
    proc_table.add_column("#", style="dim", width=3)
    proc_table.add_column("Application", style="cyan", ratio=3)
    proc_table.add_column("PID", style="dim", width=7)
    proc_table.add_column("Download", style="green", width=14, justify="right")
    proc_table.add_column("Upload", style="blue", width=14, justify="right")
    proc_table.add_column("Total", style="yellow", width=14, justify="right")

    if proc_tracker:
        procs = proc_tracker.get_processes()
        if procs:
            for i, (name, pid, down, up) in enumerate(procs[:10], 1):
                total = down + up
                pid_str = str(pid) if pid else "-"
                proc_table.add_row(
                    str(i), name, pid_str,
                    f"{down:.2f} Mbps", f"{up:.2f} Mbps", f"{total:.2f} Mbps",
                )
        else:
            proc_table.add_row("-", "No active network processes", "-", "-", "-", "-")

    layout["processes"].update(
        Panel(proc_table, title="[bold]Apps Using Bandwidth[/bold]", border_style="magenta")
    )

    # Footer
    footer_text = (
        f"  Alerts triggered: {alert_count}  |  "
        f"Thresholds — Down: {cfg['alert_down_mbps']} Mbps, Up: {cfg['alert_up_mbps']} Mbps  |  "
        f"Speed test every: {cfg['speedtest_interval_min']} min  |  "
        f"[dim]Ctrl+C to stop[/dim]"
    )
    layout["footer"].update(Panel(Text(footer_text), style="dim"))

    return layout


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_monitor(cfg, db_path):
    console = Console()
    conn = init_db(db_path)
    sampler = BandwidthSampler(cfg["interface"])
    speedtest_runner = SpeedTestRunner()
    proc_tracker = ProcessBandwidthTracker()
    alert_count = 0
    session_start = time.time()
    last_speedtest_time = 0
    last_db_write = 0
    last_proc_sample = 0
    alert_cooldown = {}  # type → last_alert_time

    stop_event = threading.Event()

    def on_signal(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    def speedtest_loop():
        nonlocal last_speedtest_time
        while not stop_event.is_set():
            now = time.time()
            interval = cfg["speedtest_interval_min"] * 60
            if now - last_speedtest_time >= interval:
                speedtest_runner.run()
                last_speedtest_time = time.time()
                if speedtest_runner.last_down is not None:
                    store_speedtest(
                        conn,
                        speedtest_runner.last_down,
                        speedtest_runner.last_up,
                        speedtest_runner.last_ping,
                        speedtest_runner.last_server,
                    )
            stop_event.wait(10)

    def proc_tracker_loop():
        """Sample per-process bandwidth every 3 seconds in background."""
        while not stop_event.is_set():
            proc_tracker.sample()
            stop_event.wait(3)

    st_thread = threading.Thread(target=speedtest_loop, daemon=True)
    st_thread.start()

    pt_thread = threading.Thread(target=proc_tracker_loop, daemon=True)
    pt_thread.start()

    console.print("[bold green]Starting bandwidth monitor...[/bold green] Press Ctrl+C to stop.\n")

    try:
        with Live(build_dashboard(sampler, speedtest_runner, cfg, alert_count, session_start, proc_tracker),
                   console=console, refresh_per_second=2, screen=True) as live:
            while not stop_event.is_set():
                sampler.sample()
                now = time.time()

                # Store to DB every 5 seconds (avoid spamming)
                if now - last_db_write >= 5:
                    store_sample(conn, sampler.down_mbps, sampler.up_mbps)
                    last_db_write = now

                # Check alerts (with 60s cooldown per type)
                for label, value, threshold in [
                    ("download", sampler.down_mbps, cfg["alert_down_mbps"]),
                    ("upload", sampler.up_mbps, cfg["alert_up_mbps"]),
                ]:
                    if value > 0.01 and value < threshold:  # ignore idle
                        last = alert_cooldown.get(label, 0)
                        if now - last > 60:
                            msg = f"Low {label} speed: {value:.2f} Mbps (threshold: {threshold} Mbps)"
                            store_alert(conn, msg)
                            notify("Bandwidth Alert", msg)
                            alert_count += 1
                            alert_cooldown[label] = now

                live.update(build_dashboard(sampler, speedtest_runner, cfg, alert_count, session_start, proc_tracker))
                time.sleep(SAMPLE_INTERVAL_SEC)
    except KeyboardInterrupt:
        pass

    console.print("\n[bold yellow]Monitor stopped.[/bold yellow]")
    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Real-time bandwidth monitor with alerts and usage logging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Start live monitor (default)
  %(prog)s --alert-down 10          Alert if download < 10 Mbps
  %(prog)s report --hours 24        Show usage report for last 24 hours
  %(prog)s config --show            Show current config
  %(prog)s config --set-alert-down 20 --set-alert-up 5
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # monitor (default)
    mon = sub.add_parser("monitor", help="Start live monitoring (default)")
    mon.add_argument("-i", "--interface", help="Network interface (default: en0)")
    mon.add_argument("--alert-down", type=float, help="Download alert threshold (Mbps)")
    mon.add_argument("--alert-up", type=float, help="Upload alert threshold (Mbps)")
    mon.add_argument("--speedtest-interval", type=int, help="Minutes between speed tests")
    mon.add_argument("--db", type=str, help="Path to SQLite database")

    # report
    rep = sub.add_parser("report", help="Show usage report")
    rep.add_argument("--hours", type=int, default=24, help="Hours to look back (default: 24)")
    rep.add_argument("--db", type=str, help="Path to SQLite database")

    # config
    conf = sub.add_parser("config", help="View/edit configuration")
    conf.add_argument("--show", action="store_true", help="Show current config")
    conf.add_argument("--set-alert-down", type=float, help="Set download alert threshold")
    conf.add_argument("--set-alert-up", type=float, help="Set upload alert threshold")
    conf.add_argument("--set-interface", type=str, help="Set network interface")
    conf.add_argument("--set-speedtest-interval", type=int, help="Set speed test interval (min)")

    # speedtest
    sub.add_parser("speedtest", help="Run a one-off speed test")

    args = parser.parse_args()
    cfg = load_config(DEFAULT_CONFIG_PATH)

    # Default to 'monitor' if no subcommand
    command = args.command or "monitor"

    if command == "monitor":
        if hasattr(args, "interface") and args.interface:
            cfg["interface"] = args.interface
        if hasattr(args, "alert_down") and args.alert_down is not None:
            cfg["alert_down_mbps"] = args.alert_down
        if hasattr(args, "alert_up") and args.alert_up is not None:
            cfg["alert_up_mbps"] = args.alert_up
        if hasattr(args, "speedtest_interval") and args.speedtest_interval is not None:
            cfg["speedtest_interval_min"] = args.speedtest_interval
        db = Path(args.db) if hasattr(args, "db") and args.db else DEFAULT_DB_PATH
        run_monitor(cfg, db)

    elif command == "report":
        db = Path(args.db) if args.db else DEFAULT_DB_PATH
        conn = init_db(db)
        console = Console()
        print_report(conn, args.hours, console)
        conn.close()

    elif command == "config":
        if args.set_alert_down is not None:
            cfg["alert_down_mbps"] = args.set_alert_down
        if args.set_alert_up is not None:
            cfg["alert_up_mbps"] = args.set_alert_up
        if args.set_interface is not None:
            cfg["interface"] = args.set_interface
        if args.set_speedtest_interval is not None:
            cfg["speedtest_interval_min"] = args.set_speedtest_interval

        if any([
            args.set_alert_down is not None,
            args.set_alert_up is not None,
            args.set_interface is not None,
            args.set_speedtest_interval is not None,
        ]):
            save_config(DEFAULT_CONFIG_PATH, cfg)
            print("Config saved.")

        if args.show or not any([
            args.set_alert_down, args.set_alert_up,
            args.set_interface, args.set_speedtest_interval,
        ]):
            console = Console()
            console.print_json(json.dumps(cfg, indent=2))

    elif command == "speedtest":
        console = Console()
        console.print("[bold]Running speed test...[/bold] (this takes ~30 seconds)")
        runner = SpeedTestRunner()
        runner.run()
        if runner.error:
            console.print(f"[red]Error: {runner.error}[/red]")
        else:
            t = Table(title="Speed Test Results", show_lines=True)
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="bold")
            t.add_row("Download", f"{runner.last_down:.1f} Mbps")
            t.add_row("Upload", f"{runner.last_up:.1f} Mbps")
            t.add_row("Ping", f"{runner.last_ping:.0f} ms")
            t.add_row("Server", runner.last_server or "-")
            console.print(t)
            # Store result
            conn = init_db(DEFAULT_DB_PATH)
            store_speedtest(conn, runner.last_down, runner.last_up, runner.last_ping, runner.last_server)
            conn.close()
            console.print("[green]Result saved to database.[/green]")


if __name__ == "__main__":
    main()
