# Real SpeedTest - Bandwidth Monitor

A real-time internet bandwidth monitoring tool for macOS that tracks your actual network speed, alerts you when it drops below acceptable levels, and keeps a full history of your usage.

## Why?

ISPs promise high speeds, but what you actually get can vary throughout the day. Apps like Bandwidth+ show a number, but don't tell you when your speed dropped, how often it happens, or keep any record. This tool solves that by:

- **Measuring your real bandwidth** every second via interface counters
- **Running actual speed tests** periodically to verify what you're getting from your ISP
- **Alerting you instantly** (macOS notification with sound) when speed drops below your threshold
- **Logging everything** to a local SQLite database so you have proof and history

## Features

- Live terminal dashboard with colored speed bars (download + upload)
- Periodic internet speed tests via speedtest-cli (configurable interval)
- macOS desktop notifications when bandwidth drops below thresholds
- SQLite-backed usage history with reporting
- Configurable alert thresholds for download and upload
- One-off speed test command
- Usage reports with averages, peaks, and alert history

## Screenshots

```
┌──────────────────────────────────────────────────────────────────────┐
│  Bandwidth Monitor  |  Interface: en0  |  Uptime: 0:05:32          │
├─────────────────────────────┬────────────────────────────────────────┤
│  Real-time Interface Speed  │  Internet Speed Test                   │
│                             │                                        │
│  ▼ Download  ████████░░░░░  │  Download:  45.2 Mbps                  │
│              25.30 Mbps     │  Upload:    12.8 Mbps                  │
│  ▲ Upload    ███░░░░░░░░░░  │  Ping:      18 ms                     │
│              4.10 Mbps      │  Server:    Airtel                     │
├─────────────────────────────┴────────────────────────────────────────┤
│  Alerts: 2  |  Thresholds — Down: 5 Mbps, Up: 1 Mbps  |  Ctrl+C   │
└──────────────────────────────────────────────────────────────────────┘
```

## Requirements

- macOS (uses `osascript` for notifications, `en0` for Wi-Fi)
- Python 3.7+

## Installation

```bash
# Clone the repo
git clone git@github.com:nilemandal/real-speedtest.git
cd real-speedtest

# Create virtual environment and install dependencies
python3 -m venv bandwidth_monitor_env
source bandwidth_monitor_env/bin/activate
pip install psutil rich speedtest-cli
```

## Usage

Always activate the virtual environment first:

```bash
source bandwidth_monitor_env/bin/activate
```

### Start the live dashboard

```bash
python3 bandwidth_monitor.py
```

### Run a one-off speed test

```bash
python3 bandwidth_monitor.py speedtest
```

### View usage report (last 24 hours)

```bash
python3 bandwidth_monitor.py report --hours 24
```

### View or change configuration

```bash
# Show current config
python3 bandwidth_monitor.py config --show

# Set alert thresholds (alert when download < 10 Mbps or upload < 2 Mbps)
python3 bandwidth_monitor.py config --set-alert-down 10 --set-alert-up 2

# Change speed test interval to every 10 minutes
python3 bandwidth_monitor.py config --set-speedtest-interval 10

# Use a different network interface
python3 bandwidth_monitor.py config --set-interface en1
```

### Monitor with custom settings (without saving to config)

```bash
python3 bandwidth_monitor.py monitor --alert-down 10 --alert-up 2 --speedtest-interval 10
```

## Configuration

Default config (`bandwidth_config.json`):

| Setting | Default | Description |
|---------|---------|-------------|
| `alert_down_mbps` | 5.0 | Alert when download speed drops below this (Mbps) |
| `alert_up_mbps` | 1.0 | Alert when upload speed drops below this (Mbps) |
| `speedtest_interval_min` | 15 | Minutes between automatic speed tests |
| `interface` | en0 | Network interface to monitor (en0 = Wi-Fi on macOS) |

## How It Works

1. **Interface sampling** — Reads network counters from your Wi-Fi interface every second using `psutil` to calculate real-time throughput
2. **Speed tests** — Runs a full download/upload/ping test using `speedtest-cli` at configurable intervals to measure actual ISP speed
3. **Alerts** — Compares real-time speed against your thresholds. If speed drops below the limit (and the connection is active), sends a macOS notification with sound. Has a 60-second cooldown to avoid spam
4. **Logging** — Stores interface samples (every 5s), speed test results, and alerts in a local SQLite database (`bandwidth_usage.db`)
5. **Reporting** — Queries the database to show averages, peaks, speed test history, and alert log

## Data Storage

All data is stored locally:

- `bandwidth_usage.db` — SQLite database with three tables:
  - `usage_samples` — interface speed readings (every 5 seconds)
  - `speed_tests` — full speed test results
  - `alerts` — triggered alert history
- `bandwidth_config.json` — your configuration

## License

MIT
