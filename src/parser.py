import re
from typing import List, Dict

HEADER_PATTERN = re.compile(r'^\s*Index', re.IGNORECASE)
TOTAL_PATTERN = re.compile(r'^\s*Total', re.IGNORECASE)
PROMPT_PATTERN = re.compile(r'=>')
PIPE_ROW_PATTERN = re.compile(
    r'\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([-+]?\d+(?:\.\d+)?)\s*\|\s*'
    r'([-+]?\d+(?:\.\d+)?)\s*\|\s*([-+]?\d+(?:\.\d+)?)\s*\|\s*'
    r'([-+]?\d+(?:\.\d+)?)\s*\|'
)


def parse_measurement(output: str) -> List[Dict]:
    """
    Parse the output of `auto measure power` from the XDS110 automation firmware.
    Expected columns: index, rail name, shunt_uV, voltage_V, current_mA, power_mW.
    Returns list of dicts per rail.
    Lines that cannot be parsed are skipped but captured in raw_payload.
    """
    normalized = output.replace('\r', '\n')
    readings = []

    for match in PIPE_ROW_PATTERN.finditer(normalized):
        readings.append({
            'index': int(match.group(1)),
            'rail': match.group(2).strip(),
            'shunt_uv': float(match.group(3)),
            'voltage_v': float(match.group(4)),
            'current_ma': float(match.group(5)),
            'power_mw': float(match.group(6)),
            'raw': match.group(0).strip(),
        })

    if readings:
        return readings

    for line in normalized.splitlines():
        if not line.strip():
            continue
        if HEADER_PATTERN.match(line) or TOTAL_PATTERN.match(line) or PROMPT_PATTERN.search(line):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            idx = int(parts[0])
            rail_name = parts[1]
            shunt_uv = float(parts[2])
            voltage_v = float(parts[3])
            current_ma = float(parts[4])
            power_mw = float(parts[5])
        except ValueError:
            continue
        readings.append({
            'index': idx,
            'rail': rail_name,
            'shunt_uv': shunt_uv,
            'voltage_v': voltage_v,
            'current_ma': current_ma,
            'power_mw': power_mw,
            'raw': line.strip(),
        })
    return readings
