#!/usr/bin/env python3

"""S.M.A.R.T custom metrics plugin for mackerel.io agent."""

import argparse
import configparser
import datetime
import json
import os
import pathlib
import re
import subprocess as sp
import sys
from typing import Dict, Optional


class ArgParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter, *args, **kwargs
        )
        self.add_argument(
            "--config",
            "-c",
            type=pathlib.Path,
            metavar="PATH",
            default="/etc/mackerel-agent/mackerel-plugin-smart.conf",
            help="Path to the config file. See mackerel-plugin-smart.conf.example for details.",
        )
        self.add_argument(
            "--print-schema",
            "-p",
            action="store_true",
            help="Displays the graph schema and exit. This can also be done by passing MACKEREL_AGENT_PLUGIN_META=1 as an environment variable.",
        )


class DataStructureNotCompatibleError(RuntimeError):
    """Compatible SMART Attributes Data Structure revision number not found."""


ATTR_COLUMNS = "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE".split()
"""Columns from SMART Attributes Data Structure revision 16."""

STATUS_BIT_MEANINGS = {
    0: "Command line did not parse",
    1: "Device open failed",
    2: "command to the disk failed, or checksum error",
    3: "SMART status DISK FAILING",
    4: "prefail Attributes <= threshold",
    5: "Attributes <= threshold in the past",
    6: "error log contains errors",
    7: "self-test log contains errors",
}
"""Meaning of each bit in the exit status of smartctl.

Bit 0: Command line did not parse.
Bit 1: Device open failed, device did not return an IDENTIFY DEVICE structure, or device is in a low-power mode (see '-n' option above).
Bit 2: Some SMART or other ATA command to the disk failed, or there was a checksum error in a SMART data structure (see '-b' option above).
Bit 3: SMART status check returned "DISK FAILING".
Bit 4: We found prefail Attributes <= threshold.
Bit 5: SMART status check returned "DISK OK" but we found that some (usage or prefail) Attributes have been <= threshold at some time in the past.
Bit 6: The device error log contains records of errors.
Bit 7: The device self-test log contains records of errors.  [ATA only] Failed self-tests outdated by a newer successful extended self-test are ignored.
"""


def get_disk_sections(config: configparser.ConfigParser):
    return [value for key, value in config.items() if key.startswith("disks.")]


def parse_list_of_int(s: str):
    return [int(s.strip()) for s in s.split(",")]


def escape_disk_name(disk_name: str):
    # https://mackerel.io/docs/entry/advanced/custom-metrics#graph-schema
    return re.sub(r"[^-a-zA-Z0-9_]", "_", disk_name)


def get_status_metric_name(disk_name: str, ibit: int):
    escaped_name = escape_disk_name(disk_name)
    return f"{escaped_name}--{ibit}"


def get_attr_metric_name(disk_name: str, attr_id: int):
    escaped_name = escape_disk_name(disk_name)
    return f"{escaped_name}--{attr_id}"


def print_graph_schema(config):
    """See https://mackerel.io/docs/entry/advanced/custom-metrics#graph-schema"""

    disks = get_disk_sections(config)
    norm_attr_ids = config.getintegers("metrics", "normalized_attributes")
    raw_attr_ids = config.getintegers("metrics", "raw_attributes")

    graphs = {
        "smart.status": {
            "label": "SMART status",
            "unit": "integer",  # 0 or 1
            "metrics": [
                {
                    "name": get_status_metric_name(disk["name"], ibit),
                    "label": f"{disk['name']}/{ibit}:{STATUS_BIT_MEANINGS[ibit]}",
                }
                for disk in disks
                for ibit in config.getintegers("metrics", "status")
            ],
        },
    }

    smart_attrs = {}
    if norm_attr_ids or raw_attr_ids:
        for disk in disks:
            _status, attrs = get_smart_attrs(
                disk, config, timestamp=None
            )  # does not report metrics
            smart_attrs[disk["name"]] = attrs
    attr_labels = {
        int(attr["ID#"]): attr["ATTRIBUTE_NAME"]
        for attrs_per_disk in smart_attrs.values()
        for attr in attrs_per_disk
    }

    if norm_attr_ids:
        graphs["smart.attributes.normalized"] = {
            "label": "Normalized SMART Attributes",
            "unit": "integer",
            "metrics": [
                {
                    "name": get_attr_metric_name(disk["name"], attr_id),
                    "label": f"{disk['name']}/{attr_id}:{attr_labels[attr_id]}",
                }
                for disk in disks
                for attr_id in norm_attr_ids
            ],
        }

    for attr_id in raw_attr_ids:
        graphs[f"smart.attributes.raw.{attr_id}"] = {
            "label": f"SMART Attr {attr_id} {attr_labels[attr_id]} (raw)",
            "unit": "integer",
            "metrics": [
                {
                    "name": escape_disk_name(disk["name"]),
                    "label": disk["name"],
                }
                for disk in disks
            ],
        }

    print("# mackerel-agent-plugin")
    print(json.dumps({"graphs": graphs}, indent=4))

    return 0


def parse_attr_line(stripped_line: str) -> Dict[str, str]:
    return dict(zip(ATTR_COLUMNS, stripped_line.split()))


def get_smart_attrs(
    disk,
    config,
    timestamp: Optional[int] = None,
):
    """Executes smartctl and retrieves SMART status and attributes.

    Args:
        timestamp: Epoch seconds to report. If None, metrics are returned but not printed out.
    """
    smart_args = ["smartctl", "-A"]
    if disk["device_type"]:
        smart_args += ["-d", disk["device_type"]]
    smart_args += [
        disk["path"],
    ]
    comp_process = sp.run(
        smart_args,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )

    if comp_process.stderr:
        sys.stderr.buffer.write(comp_process.stderr)

    smart_status = {
        ibit: int(bool(comp_process.returncode & (1 << ibit)))
        for ibit in config.getintegers("metrics", "status")
    }

    if timestamp is not None:
        for ibit, value in smart_status.items():
            print(
                f"smart.status.{get_status_metric_name(disk['name'], ibit)}",
                value,
                timestamp,
                sep="\t",
            )

    # Bit 0: Command line did not parse.
    if comp_process.returncode & 1:
        raise sp.CalledProcessError(
            returncode=comp_process.returncode,
            cmd=comp_process.args,
            output=comp_process.stdout,
            stderr=comp_process.stderr,
        )

    # Bit 1: Device open failed, device did not return an IDENTIFY DEVICE structure, or device is in a low-power mode (see '-n' option above).
    # --> possibly low-power mode
    if comp_process.returncode & (1 << 1):
        print("device open failed. Maybe sleeping?", file=sys.stderr)
        sys.exit(comp_process.returncode)

    smart_output = comp_process.stdout.decode()

    # SMART Attributes Data Structure revision number: 16
    # Vendor Specific SMART Attributes with Thresholds:
    # ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
    #   1 Raw_Read_Error_Rate     0x002f   175   111   051    Pre-fail  Always       -       40230
    #   3 Spin_Up_Time            0x0027   222   177   021    Pre-fail  Always       -       3858
    #   4 Start_Stop_Count        0x0032   100   100   000    Old_age   Always       -       121
    #   5 Reallocated_Sector_Ct   0x0033   165   165   140    Pre-fail  Always       -       1043
    #   7 Seek_Error_Rate         0x002e   200   200   000    Old_age   Always       -       0
    #   9 Power_On_Hours          0x0032   053   052   000    Old_age   Always       -       34961
    #  10 Spin_Retry_Count        0x0032   100   100   000    Old_age   Always       -       0
    #  11 Calibration_Retry_Count 0x0032   100   253   000    Old_age   Always       -       0
    #  12 Power_Cycle_Count       0x0032   100   100   000    Old_age   Always       -       21
    # 192 Power-Off_Retract_Count 0x0032   200   200   000    Old_age   Always       -       11
    # 193 Load_Cycle_Count        0x0032   198   198   000    Old_age   Always       -       8412
    # 194 Temperature_Celsius     0x0022   109   095   000    Old_age   Always       -       41
    # 196 Reallocated_Event_Count 0x0032   182   182   000    Old_age   Always       -       18
    # 197 Current_Pending_Sector  0x0032   200   200   000    Old_age   Always       -       10
    # 198 Offline_Uncorrectable   0x0030   200   200   000    Old_age   Offline      -       8
    # 199 UDMA_CRC_Error_Count    0x0032   200   200   000    Old_age   Always       -       0
    # 200 Multi_Zone_Error_Rate   0x0008   200   199   000    Old_age   Offline      -       2
    m_data_structure = re.search(
        r"^SMART Attributes Data Structure revision number: 16$",
        smart_output,
        re.MULTILINE,
    )
    if not m_data_structure:
        raise DataStructureNotCompatibleError(
            "SMART Attributes Data Structure revision number: 16 not found"
        )

    smart_attrs = []
    reading = False
    for orig_line in smart_output.splitlines():
        line = orig_line.strip()
        if line.startswith("ID#"):
            reading = True
            continue
        if not reading:
            continue
        if not line:
            break
        smart_attrs.append(parse_attr_line(line))

    if timestamp is not None:
        for attr in smart_attrs:
            attr_id = int(attr["ID#"])
            if attr_id in config.getintegers("metrics", "normalized_attributes"):
                print(
                    f"smart.attributes.normalized.{get_attr_metric_name(disk['name'], attr_id)}",
                    attr["VALUE"],
                    timestamp,
                    sep="\t",
                )
            if attr_id in config.getintegers("metrics", "raw_attributes"):
                print(
                    f"smart.attributes.raw.{attr_id}.{escape_disk_name(disk['name'])}",
                    attr["RAW_VALUE"],
                    timestamp,
                    sep="\t",
                )

    return smart_status, smart_attrs


def main(args):
    config = configparser.ConfigParser(converters={"integers": parse_list_of_int})
    if not args.config.is_file():
        raise FileNotFoundError(f"Config file {args.config} not found.")
    config.read(args.config)

    if os.getenv("MACKEREL_AGENT_PLUGIN_META") or args.print_schema:
        return print_graph_schema(config)

    now = datetime.datetime.now()
    timestamp = int(now.timestamp())
    for disk in get_disk_sections(config):
        get_smart_attrs(disk, config, timestamp)


if __name__ == "__main__":
    args = ArgParser().parse_args()
    main(args)
