"""Add independent registration listeners to a Mihomo config.

The deployment keeps the subscription URL and credentials in the host-only
config. This helper only rewrites the generated proxy groups/listeners, so it
can safely run against that private file without copying secrets into Git.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def _top_level_section(lines: list[str], name: str) -> tuple[int, int] | None:
    start = next(
        (index for index, line in enumerate(lines) if line == f"{name}:"),
        None,
    )
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line[0].isspace() and line.endswith(":"):
            end = index
            break
    return start, end


def _use_providers(provider_names: list[str]) -> list[str]:
    return ["    use:"] + [f"      - {name}" for name in provider_names]


def _groups(slot_count: int, provider_names: list[str]) -> list[str]:
    lines = [
        "proxy-groups:",
        "  - name: REGISTER-ALL",
        "    type: select",
        *_use_providers(provider_names),
        "  - name: REGISTER-US",
        "    type: select",
        *_use_providers(provider_names),
        "    filter: '(?i)(🇺🇸|美国|美國|United States|\\bUS\\b|USA)'",
    ]
    for slot in range(1, slot_count + 1):
        lines.extend(
            [
                f"  - name: REGISTER-SLOT-{slot:02d}",
                "    type: select",
                *_use_providers(provider_names),
            ]
        )
    return lines


def _listeners(slot_count: int, port_base: int) -> list[str]:
    lines = ["listeners:"]
    for slot in range(1, slot_count + 1):
        lines.extend(
            [
                f"  - name: REGISTER-IN-{slot:02d}",
                "    type: mixed",
                f"    port: {port_base + slot}",
                "    listen: 0.0.0.0",
                f"    proxy: REGISTER-SLOT-{slot:02d}",
            ]
        )
    return lines


def augment_config(
    text: str,
    *,
    slot_count: int = 50,
    port_base: int = 7900,
    provider_names: list[str] | None = None,
) -> str:
    lines = str(text).replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    groups_section = _top_level_section(lines, "proxy-groups")
    if groups_section is None:
        raise ValueError("Mihomo config is missing proxy-groups")
    start, end = groups_section
    providers = [str(name).strip() for name in (provider_names or []) if str(name).strip()]
    lines[start:end] = _groups(slot_count, providers or ["registration-subscription"])

    listener_section = _top_level_section(lines, "listeners")
    if listener_section is not None:
        listener_start, listener_end = listener_section
        lines[listener_start:listener_end] = _listeners(slot_count, port_base)
    else:
        rules_section = _top_level_section(lines, "rules")
        if rules_section is None:
            lines.extend(["", *_listeners(slot_count, port_base)])
        else:
            lines[rules_section[0]:rules_section[0]] = ["", *_listeners(slot_count, port_base)]

    lines = [
        line.replace("MATCH,REGISTER-US", "MATCH,REGISTER-ALL")
        for line in lines
    ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--slot-count", type=int, default=50)
    parser.add_argument("--port-base", type=int, default=7900)
    # Repeatable: --provider registration-subscription-1 --provider registration-subscription-2
    parser.add_argument("--provider", action="append", dest="provider_names", default=None)
    args = parser.parse_args()
    output = augment_config(
        args.source.read_text(encoding="utf-8"),
        slot_count=max(1, min(args.slot_count, 200)),
        port_base=args.port_base,
        provider_names=args.provider_names,
    )
    temporary = args.destination.with_suffix(args.destination.suffix + ".tmp")
    try:
        temporary.write_text(output, encoding="utf-8")
        temporary.replace(args.destination)
    except OSError:
        args.destination.write_text(output, encoding="utf-8")
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
