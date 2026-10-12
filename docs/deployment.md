# Running it for real

The bot needs to run all the time, next to (or network-reachable from) a companion radio.

## Windows

```bat
windows\install.bat           REM creates a virtual environment and installs MeshScore
meshscore init                REM (inside the venv - install.bat opens it for you)
windows\run-meshscore.bat     REM runs, and restarts the bot if it exits
```

Put `windows\run-meshscore.bat` in `shell:startup` to start it at login. For a true service use
[NSSM](https://nssm.cc/) pointing at that script.

## Linux / Raspberry Pi (systemd)

```bash
python3 -m venv ~/meshscore-venv && ~/meshscore-venv/bin/pip install meshscore
~/meshscore-venv/bin/meshscore init
sudo cp contrib/meshscore.service /etc/systemd/system/     # edit User= and paths first
sudo systemctl enable --now meshscore
journalctl -u meshscore -f
```

The service user needs access to the serial device: `sudo usermod -aG dialout $USER` on Debian/Raspberry Pi OS.

## Docker

```bash
cp meshscore.example.toml data/meshscore.toml     # edit it
docker compose up -d
docker compose logs -f
```

The container reads `/data/meshscore.toml` and keeps state and logs in `/data`. For a **WiFi radio** (`transport =
"tcp"`) nothing else is needed. For a **USB radio** pass the device through in `docker-compose.yml` (`devices:` -
Linux hosts; Docker Desktop on Windows/macOS can't pass USB serial devices into containers, run natively there).
Bluetooth from a container is possible but fiddly; run natively.

## Several bots / several radios

* One radio per bot *process*: a companion radio serves one client at a time.
* One process can run many bots (many `[bots.x]` tables) on one radio, sharing its channels.
* For redundancy or several teams on separate radios, run several processes with separate config files and
  `--data-dir`s, and give each a different `instance_port` (or `0`).

## Updating

```bash
pip install -U meshscore
```

Restart the bot to pick it up; the config file format is stable within a major version (unknown settings are errors, so
a typo is caught immediately by `meshscore check`).

## Logs

`meshscore.log` (rotating, 1 MB x 3) next to your config. Look for:

```
TX OK [#leafs]: 🚨 TOR GOAL ...            a message went out
SAME-AIR: already on the air, skipping:    another bot posted it first (working as intended)
AIRTIME budget reached (20/h), not sending a goal was dropped by [airtime] max_per_hour
WARNING Radio disconnected                  the transport drops and reconnects with backoff
```
