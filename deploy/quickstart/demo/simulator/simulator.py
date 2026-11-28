#!/usr/bin/env python3
"""A pretend house for the argus-scout demo: power loads, a GPU and an energy meter as Prometheus metrics.

Prometheus scrapes it like any exporter; scout never talks to it (scout reads Prometheus only). It switches
between scenarios on request, and writes a time-of-use tariff whose peak is happening *now*, so the
tariff-shift scenario works whatever time of day the demo runs.

    python simulator.py --port 9101 --tariff /config/tariff.yaml
    curl -X POST localhost:9101/scenario/peak-batch     # normal | peak-batch | gpu-hot | gpu-critical
    curl localhost:9101/metrics

Metric names match what real exporters publish and what the demo's signal rules query:
``load_power_watts{load,shiftable}`` (a smart plug or power meter), ``home_energy_kwh_total`` (the house
meter), ``DCGM_FI_DEV_GPU_TEMP`` / ``DCGM_FI_DEV_GPU_UTIL`` (NVIDIA's dcgm-exporter). Standard library only.
"""

from __future__ import annotations

import argparse
import math
import os
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Each scenario: watts per load (name -> (watts, shiftable)), and the GPU's temperature and utilisation.
SCENARIOS: dict[str, dict] = {
    "normal": {
        "loads": {"batch-render": (60.0, True), "ev-charger": (0.0, True), "fridge": (140.0, False)},
        "gpu": {"temp": 52.0, "util": 18.0},
    },
    # A heavy shiftable job during the tariff's peak: scout's tariff_shift rule proposes moving it.
    "peak-batch": {
        "loads": {"batch-render": (1850.0, True), "ev-charger": (0.0, True), "fridge": (140.0, False)},
        "gpu": {"temp": 74.0, "util": 96.0},
    },
    # The GPU runs hot: a warning, which the demo's standing approval may approve.
    "gpu-hot": {
        "loads": {"batch-render": (1850.0, True), "ev-charger": (0.0, True), "fridge": (140.0, False)},
        "gpu": {"temp": 84.0, "util": 99.0},
    },
    # Hotter still: critical. The guardrail withdraws the standing approval and leaves it to a person.
    "gpu-critical": {
        "loads": {"batch-render": (1850.0, True), "ev-charger": (0.0, True), "fridge": (140.0, False)},
        "gpu": {"temp": 93.0, "util": 100.0},
    },
}
DEFAULT_SCENARIO = "normal"
METRICS = ("load_power_watts", "home_energy_kwh_total", "DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_GPU_UTIL", "demo_scenario")


def tariff_yaml(
    now: datetime, *, before: timedelta = timedelta(minutes=30), after: timedelta = timedelta(minutes=40)
) -> str:
    """A tariff (UTC wall clock, as the demo's scout.yaml) whose ``peak`` started ``before`` ago and ends ``after``
    from now; ``off_peak`` covers the rest of the day."""
    start = (now - before).strftime("%H:%M")
    end = (now + after).strftime("%H:%M")
    return (
        "# Written by the demo simulator: peak is centred on the time the scenario started. Do not edit.\n"
        "plan: demo_time_of_use\n"
        "currency: USD\n"
        "unit: kWh\n"
        "daily_charge: 110.0\n"
        "periods:\n"
        f'  peak:\n    start: "{start}"\n    end: "{end}"\n    rate: 52.0\n'
        f'  off_peak:\n    start: "{end}"\n    end: "{start}"\n    rate: 18.0\n'
    )


def write_tariff(path: Path, now: datetime | None = None) -> None:
    """Atomically, so scout never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tariff-", suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(tariff_yaml(now or datetime.now(UTC)))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class House:
    """The simulated state. Thread-safe: the HTTP server is threaded."""

    def __init__(self, tariff_path: Path | None = None, clock=time.monotonic) -> None:
        self.lock = threading.Lock()
        self.scenario = DEFAULT_SCENARIO
        self.tariff_path = tariff_path
        self.clock = clock
        self.energy_kwh = 0.0
        self.last = clock()
        if tariff_path is not None:
            write_tariff(tariff_path)

    def set(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise KeyError(scenario)
        with self.lock:
            self._integrate()
            self.scenario = scenario
        if scenario == "peak-batch" and self.tariff_path is not None:
            write_tariff(self.tariff_path)  # the peak is now, wherever the clock is

    def _integrate(self) -> None:
        now = self.clock()
        watts = sum(w for w, _ in SCENARIOS[self.scenario]["loads"].values())
        self.energy_kwh += watts * (now - self.last) / 3600 / 1000
        self.last = now

    def render(self) -> str:
        with self.lock:
            self._integrate()
            spec, energy, scenario = SCENARIOS[self.scenario], self.energy_kwh, self.scenario
        wobble = math.sin(time.time() / 20)  # a little life in the lines, never across a threshold
        out = [
            "# HELP load_power_watts Power drawn by each load.",
            "# TYPE load_power_watts gauge",
        ]
        for load, (watts, shiftable) in sorted(spec["loads"].items()):
            value = max(0.0, watts * (1 + 0.02 * wobble)) if watts else 0.0
            out.append(f'load_power_watts{{load="{load}",shiftable="{str(shiftable).lower()}"}} {value:.1f}')
        out += [
            "# HELP home_energy_kwh_total Energy through the house meter.",
            "# TYPE home_energy_kwh_total counter",
            f"home_energy_kwh_total {energy:.6f}",
            "# HELP DCGM_FI_DEV_GPU_TEMP GPU temperature (in C).",
            "# TYPE DCGM_FI_DEV_GPU_TEMP gauge",
            f'DCGM_FI_DEV_GPU_TEMP{{gpu="0"}} {spec["gpu"]["temp"] + wobble:.1f}',
            "# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization (in %).",
            "# TYPE DCGM_FI_DEV_GPU_UTIL gauge",
            f'DCGM_FI_DEV_GPU_UTIL{{gpu="0"}} {min(100.0, spec["gpu"]["util"] + 2 * wobble):.1f}',
            "# HELP demo_scenario 1 for the scenario the simulator is playing.",
            "# TYPE demo_scenario gauge",
        ]
        out += [f'demo_scenario{{name="{name}"}} {int(name == scenario)}' for name in SCENARIOS]
        return "\n".join(out) + "\n"


def handler_for(house: House) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            if self.path == "/metrics":
                self._send(200, house.render())
            elif self.path in ("/", "/scenario"):
                self._send(200, f"{house.scenario}\n")
            else:
                self._send(404, "not found\n")

        def do_POST(self) -> None:  # noqa: N802
            prefix = "/scenario/"
            if not self.path.startswith(prefix):
                self._send(404, "not found\n")
                return
            try:
                house.set(self.path[len(prefix) :])
            except KeyError:
                self._send(400, f"unknown scenario; one of: {', '.join(SCENARIOS)}\n")
                return
            self._send(200, f"{house.scenario}\n")

        def log_message(self, fmt: str, *args) -> None:
            if not self.path.startswith("/metrics"):
                super().log_message(fmt, *args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--tariff", type=Path, help="write the demo tariff here (scout's files.tariff)")
    args = parser.parse_args()
    house = House(args.tariff)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(house))
    print(f"demo simulator on {args.host}:{args.port}, scenario {house.scenario}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
