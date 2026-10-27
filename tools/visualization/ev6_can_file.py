"""Plot decoded EV6 vehicle signals from an injected CAN JSONL file."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from codrive.hardware.can.ev6_file import Ev6CanFileDecoder, grouped_can_jsonl
from codrive.contracts.models import Header


def decode_file(path: Path, *, bus: int, max_batches: int | None = None) -> list[dict[str, float | bool]]:
    decoder = Ev6CanFileDecoder(bus=bus)
    rows: list[dict[str, float | bool]] = []
    for sequence, (timestamp_ns, frames) in enumerate(grouped_can_jsonl(path), start=1):
        state = decoder.decode(
            frames,
            Header.now("file.can", sequence, capture_time_ns=timestamp_ns),
        )
        rows.append(
            {
                "time_s": timestamp_ns / 1_000_000_000.0,
                "speed_mps": state.speed_mps,
                "speed_kph": state.speed_mps * 3.6,
                "steering_deg": state.steering_wheel_deg,
                "accelerator_pct": state.accelerator_pct,
                "brake_pressed": state.brake_pressed,
                "yaw_rate_dps": state.yaw_rate_dps or 0.0,
                "can_valid": state.can_valid,
            }
        )
        if max_batches is not None and sequence >= max_batches:
            break
    return rows


def write_csv(rows: list[dict[str, float | bool]], path: Path) -> None:
    fields = ("time_s", "speed_mps", "speed_kph", "steering_deg", "accelerator_pct", "brake_pressed", "yaw_rate_dps", "can_valid")
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_plot(rows: list[dict[str, float | bool]], path: Path, title: str) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for CAN visualization") from exc
    if not rows:
        raise ValueError("the CAN file contains no decodable records")
    times = [float(row["time_s"]) - float(rows[0]["time_s"]) for row in rows]
    speed = [float(row["speed_kph"]) for row in rows]
    steering = [float(row["steering_deg"]) for row in rows]
    accelerator = [float(row["accelerator_pct"]) for row in rows]
    yaw_rate = [float(row["yaw_rate_dps"]) for row in rows]
    brake_times = [time for time, row in zip(times, rows) if bool(row["brake_pressed"])]
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True)
    fig.suptitle(title, fontsize=15)
    axes[0, 0].plot(times, speed, color="#1677c8", linewidth=1.2)
    axes[0, 0].set_title("Vehicle speed")
    axes[0, 0].set_ylabel("km/h")
    axes[0, 1].plot(times, steering, color="#9b4dcc", linewidth=1.2)
    axes[0, 1].set_title("Steering wheel angle")
    axes[0, 1].set_ylabel("degrees")
    axes[1, 0].plot(times, accelerator, color="#e28a17", linewidth=1.2, label="accelerator")
    axes[1, 0].set_title("Pedal and brake")
    axes[1, 0].set_ylabel("accelerator %")
    if brake_times:
        axes[1, 0].vlines(brake_times, 0, 100, color="#d33b3b", alpha=0.18, linewidth=1, label="brake")
    axes[1, 0].legend(loc="upper right")
    axes[1, 1].plot(times, yaw_rate, color="#25834d", linewidth=1.2)
    axes[1, 1].set_title("Estimated yaw rate")
    axes[1, 1].set_ylabel("degrees/s")
    for axis in axes.flat:
        axis.grid(True, alpha=0.25)
        axis.set_xlabel("elapsed time (s)")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plot EV6 signals decoded from CAN JSONL.")
    parser.add_argument("--input", type=Path, required=True, help="JSONL with ts/address/bytes/bus fields")
    parser.add_argument("--output", type=Path, default=Path("ev6_can_plot.png"))
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--bus", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=None)
    args = parser.parse_args(argv)
    rows = decode_file(args.input, bus=args.bus, max_batches=args.max_batches)
    write_plot(rows, args.output, f"EV6 CAN decoded signals · bus {args.bus}")
    if args.csv is not None:
        write_csv(rows, args.csv)
    print(f"wrote {len(rows)} decoded batches to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
