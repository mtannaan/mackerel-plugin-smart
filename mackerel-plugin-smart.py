#!/usr/bin/env python3

"""S.M.A.R.T custom metrics plugin for mackerel.io agent."""

import argparse
import configparser
import datetime
import json
import logging
import os
import pathlib
import re
import subprocess as sp
import sys
from logging.handlers import SysLogHandler
from typing import Dict, List


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
        self.add_argument(
            "--log-to-syslog",
            "-l",
            action="store_true",
            help="If specified, log output is written to the syslog and stderr. If not, log output is written to stderr.",
        )
        self.add_argument("--syslog-device", type=str, default="/dev/log")
        self.add_argument("--debug", "-d", action="store_true")


class DataStructureNotCompatibleError(RuntimeError):
    """Compatible SMART Attributes Data Structure revision number not found."""


class DeviceOpenFailedError(RuntimeError):
    """Failed to open the disk device.

    This might be because the device is in a low-power mode (see below),
    but might also be because the program does not have the privilege to open the device.

        Bit 1: Device open failed,
        device did not return an IDENTIFY DEVICE structure,
        or device is in a low-power mode (see '-n' option above).
    """

    def __init__(self, msg: str, returncode: int, *args: object) -> None:
        super().__init__(msg, *args)
        self.returncode = returncode


ATTR_COLUMNS = "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE".split()
"""Columns from SMART Attributes Data Structure revision 16."""

STATUS_BIT_MEANINGS = [
    "Command line did not parse",
    "Device open failed",
    "command to the disk failed, or checksum error",
    "SMART status DISK FAILING",
    "prefail Attributes <= threshold",
    "Attributes <= threshold in the past",
    "error log contains errors",
    "self-test log contains errors",
]
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


def parse_mask(s: str):
    s = s.strip()
    if s.startswith("0x"):
        return int(s[2:], base=16)
    elif s.startswith("0o"):
        return int(s[2:], base=8)
    elif s.startswith("0b"):
        return int(s[2:], base=2)
    return int(s, base=10)


def escape_disk_name(disk_name: str):
    # https://mackerel.io/docs/entry/advanced/custom-metrics#graph-schema
    return re.sub(r"[^-a-zA-Z0-9_]", "_", disk_name)


def new_timestamp():
    now = datetime.datetime.now()
    timestamp_now = int(now.timestamp())
    return timestamp_now


def print_graph_schema(config):
    """See https://mackerel.io/docs/entry/advanced/custom-metrics#graph-schema"""

    logging.debug("print_graph_schema")

    disks = get_disk_sections(config)
    norm_attr_ids = config.getintegers("metrics", "normalized_attributes")
    raw_attr_ids = config.getintegers("metrics", "raw_attributes")

    graphs = {
        # group (#) by disk
        # https://mackerel.io/api-docs/entry/host-metrics#post-graphdef
        "smart.status.#": {
            "label": "SMART - Status",
            "unit": "integer",
            "metrics": [{"name": "all", "label": "all"}]
            + [
                {
                    "name": f"{ibit}",
                    "label": f"{ibit}:{STATUS_BIT_MEANINGS[ibit]}",
                }
                for ibit in range(len(STATUS_BIT_MEANINGS))
            ],
        },
    }

    smart_attrs = {}
    if norm_attr_ids or raw_attr_ids:
        for disk in disks:
            _returncode, output = do_smartctl(
                disk, ignore_nocheck=True
            )  # does not report metrics
            smart_attrs[disk["name"]] = parse_smart_attrs(output)
    attr_labels = {
        int(attr["ID#"]): attr["ATTRIBUTE_NAME"]
        for attrs_per_disk in smart_attrs.values()
        for attr in attrs_per_disk
    }

    if norm_attr_ids:
        graphs["smart.attributes.normalized.#"] = {  # #=disk
            "label": "SMART - Normalized Attributes",
            "unit": "integer",
            "metrics": [
                {
                    "name": f"{attr_id}",
                    "label": f"{attr_id}:{attr_labels[attr_id]}",
                }
                for attr_id in norm_attr_ids
            ],
        }

    for attr_id in raw_attr_ids:
        graphs[f"smart.attributes.raw.#.{attr_id}"] = {  # #=disk
            "label": f"SMART - Raw Attribute {attr_id:03}: {attr_labels[attr_id]}",
            "unit": "integer",
            "metrics": [{"name": "value", "label": attr_labels[attr_id]}],
        }

    print("# mackerel-agent-plugin")
    output = json.dumps({"graphs": graphs}, indent=4)
    print(output)

    logging.debug("output:")
    if logging.getLogger().level <= logging.DEBUG:
        for line in output.splitlines():
            logging.debug(line)

    return 0


def parse_attr_line(stripped_line: str) -> Dict[str, str]:
    return dict(zip(ATTR_COLUMNS, stripped_line.split()))


def get_cache_path(disk, config):
    return (
        pathlib.Path(
            config.get(
                "metrics",
                "cache_dir_path",
                fallback=f"/var/cache/mackerel-plugin-smart.cache",
            )
        )
        / escape_disk_name(disk["name"])
    )


def check_should_report(disk, config, timestamp_now: int):
    logging.debug("check_cache")

    min_report_periodicity = config.getint(
        "metrics", "min_report_periodicity", fallback=0
    )  # type: int
    logging.debug(f"min_report_periodicity {min_report_periodicity}")
    if min_report_periodicity <= 0:
        logging.debug("should report : min_report_periodicity <= 0")
        return True

    cache_file_path = get_cache_path(disk, config)
    if not cache_file_path.is_file():
        logging.debug(f"should report: cache_file_path not found: {cache_file_path}")
        return True

    with cache_file_path.open("r") as f:
        timestamp_cached = int(f.readline().strip())
        logging.debug(f"timestamp_now {timestamp_now}")
        logging.debug(f"timestamp_cached {timestamp_cached}")
        logging.debug(f"diff  {timestamp_now - timestamp_cached}")
        if timestamp_now - timestamp_cached > min_report_periodicity:
            logging.debug("should report: diff > min_report_periodicity")
            return True

        logging.debug("should not report: diff <= min_report_periodicity")
        return False


def write_cache(disk, config, timestamp):
    """Writes out the timestamp of the last report to a cache file."""

    cache_max_age = config.getint(
        "metrics", "min_report_periodicity", fallback=0
    )  # type: int
    if cache_max_age <= 0:
        return None

    cache_file_path = get_cache_path(disk, config)
    cache_file_path.parent.mkdir(parents=True, exist_ok=True)
    cache_file_path.write_text(str(timestamp))


def do_smartctl(disk, ignore_nocheck=False):
    """Executes smartctl and retrieves SMART status and attributes."""

    logging.debug(
        f"do_smartctl disk {disk.get('name')} ignore_nocheck {ignore_nocheck}"
    )

    smart_args = ["smartctl", "-a"]
    if "device_type" in disk:
        smart_args += ["-d", disk["device_type"]]
    if (not ignore_nocheck) and "nocheck" in disk:
        smart_args += ["-n", disk["nocheck"]]
    smart_args += [
        disk["path"],
    ]
    logging.debug(f"smart_args {smart_args}")

    comp_process = sp.run(
        smart_args,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )
    logging.debug(f"returncode {comp_process.returncode}")

    if comp_process.stderr:
        for line in comp_process.stderr.decode().splitlines():
            logging.warning(line)

    # Bit 0: Command line did not parse.
    if comp_process.returncode & 1:
        raise sp.CalledProcessError(
            returncode=comp_process.returncode,
            cmd=comp_process.args,
            output=comp_process.stdout,
            stderr=comp_process.stderr,
        )

    # Bit 1: Device open failed, device did not return an IDENTIFY DEVICE structure,
    # or device is in a low-power mode (see '-n' option above).
    # --> possibly low-power mode
    if comp_process.returncode & (1 << 1):
        logging.info(
            f"Failed to open device {disk['name']} in path {disk['path']}. "
            "Maybe sleeping, or you don't have the required privilege.",
        )
        raise DeviceOpenFailedError(
            f"device {disk['name']} in path {disk['path']}",
            returncode=comp_process.returncode,
        )

    smart_output = comp_process.stdout.decode()

    return comp_process.returncode, smart_output


def parse_smart_attrs(smartctl_output: str):
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
        smartctl_output,
        re.MULTILINE,
    )
    if not m_data_structure:
        raise DataStructureNotCompatibleError(
            "SMART Attributes Data Structure revision number: 16 not found"
        )

    smart_attrs = []
    reading = False
    for orig_line in smartctl_output.splitlines():
        line = orig_line.strip()
        if line.startswith("ID#"):
            reading = True
            continue
        if not reading:
            continue
        if not line:
            break
        smart_attrs.append(parse_attr_line(line))

    return smart_attrs


def print_metrics(smartctl_returncode, smart_attrs, disk, config, timestamp: int):
    status_dict = {
        ibit: int(bool(smartctl_returncode & (1 << ibit)))
        for ibit in range(len(STATUS_BIT_MEANINGS))
    }

    print(
        f"smart.status.{escape_disk_name(disk['name'])}.all",
        smartctl_returncode & config.getmask("metrics", "status_mask", fallback=0xFF),
        timestamp,
        sep="\t",
    )
    for ibit, value in status_dict.items():
        print(
            f"smart.status.{escape_disk_name(disk['name'])}.{ibit}",
            value,
            timestamp,
            sep="\t",
        )

    for attr in smart_attrs:
        attr_id = int(attr["ID#"])
        if attr_id in config.getintegers("metrics", "normalized_attributes"):
            print(
                f"smart.attributes.normalized.{escape_disk_name(disk['name'])}.{attr_id}",
                attr["VALUE"],
                timestamp,
                sep="\t",
            )
        if attr_id in config.getintegers("metrics", "raw_attributes"):
            print(
                f"smart.attributes.raw.{escape_disk_name(disk['name'])}.{attr_id}.value",
                attr["RAW_VALUE"],
                timestamp,
                sep="\t",
            )


def main(args):
    log_handlers = [logging.StreamHandler()]  # type: List[logging.Handler]
    if args.log_to_syslog:
        log_handlers.append(SysLogHandler(address=args.syslog_device))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="mackerel-plugin-smart[%(process)d] (%(levelname)s) %(message)s",
        handlers=log_handlers,
    )

    logging.debug("mackerel-plugin-smart.py started")
    logging.debug(f"args {args}")

    exit_status = 0

    try:
        config = configparser.ConfigParser(
            converters={
                "integers": parse_list_of_int,
                "mask": parse_mask,
            }
        )
        if not args.config.is_file():
            raise FileNotFoundError(f"Config file {args.config} not found.")
        config.read(args.config)

        if os.getenv("MACKEREL_AGENT_PLUGIN_META") or args.print_schema:
            return print_graph_schema(config)

        timestamp_now = new_timestamp()
        for disk in get_disk_sections(config):
            if not check_should_report(disk, config, timestamp_now):
                continue

            try:
                smartctl_retcode, output = do_smartctl(disk)
                smart_attrs = parse_smart_attrs(output)
            except DeviceOpenFailedError as e:
                # maybe in a sleep. do not propagate to the program's exit status
                smartctl_retcode = e.returncode
                smart_attrs = []
            except sp.CalledProcessError as e:
                logging.exception(f"CalledProcessError in disk {disk.get('name')}")
                smartctl_retcode = e.returncode
                exit_status = exit_status | smartctl_retcode
                smart_attrs = []

            if smart_attrs:
                write_cache(disk, config, timestamp_now)

            print_metrics(smartctl_retcode, smart_attrs, disk, config, timestamp_now)
    except Exception:
        logging.exception("")
        raise

    return exit_status


if __name__ == "__main__":
    args = ArgParser().parse_args()
    sys.exit(main(args))
