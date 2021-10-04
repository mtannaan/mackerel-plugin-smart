# mackerel-plugin-smart

S.M.A.R.T custom metrics plugin for mackerel.io agent.

## Requirements

- Python >=3.6
- smartmontools
    - This pluging uses `smartctl` to get SMART status and attributes.

## Configuration file
List your disks to monitor in the config file, and place it in `/etc/mackerel-agent/mackerel-plugin-smart.conf`.

See mackerel-plugin-smart.conf.example in this folder for details.

## Example of mackerel-agent.conf
```
[plugin.metrics.smart]
command = "python3 /path/to/mackerel-plugin-smart.py"
# user = "root"
# This plugin uses smartctl, which requires root privilege.
# If you run mackerel-agent as a non-root user, then you would need the above line.
```
