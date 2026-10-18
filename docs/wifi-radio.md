# Moving the bot radio from USB to Wi-Fi

A USB radio can hang (a bumped cable, cold, a flaky port): Windows keeps the port listed but reports "A device attached to
the system is not functioning" and every transmission fails until it is replugged.
MeshScore already speaks Wi-Fi: set `transport = "tcp"` and point it at a companion radio's address. The catch is the
**radio**, not the software.

## 1. The hardware reality (read this first)

* If your bot radio is a **Heltec T096, it is an nRF52840** (Bluetooth LE + LoRa, **no Wi-Fi chip**). It cannot run a Wi-Fi
  companion firmware, whatever you flash on it. Sources: [Heltec's T096 datasheet](https://resource.heltec.cn/download/Mesh_Node_T096/Datasheet/Mesh%20Node%20T096.pdf)
  lists Bluetooth 5 LE only.
* Wi-Fi companion firmware exists for **ESP32 boards** (Heltec WiFi LoRa 32 **V3** / **V4**, T-Beam S3, and similar).
  The companion app talks to it over **TCP port 5000**. A MeshCore pull request names the upstream build
  `Heltec_v3_companion_radio_wifi` ([meshcore-dev/MeshCore](https://github.com/meshcore-dev/MeshCore) (a pull request names it));
  community forks also publish USB+TCP builds ([meshcomod](https://github.com/ALLFATHER-BV/meshcomod),
  [ZephCore](https://newreleases.io/project/github/liquidraver/ZephCore/release/1.17.6a-zephcore)). Treat the forks as
  unofficial; use the build for **your exact board** from the upstream MeshCore flasher / releases page.
* A radio that runs other firmware (a game, a repeater) cannot be the bot radio at the same time.

So "bot over Wi-Fi" means **one extra ESP32 LoRa board**, roughly the price of a V3/V4 (~CA$25-40). Pick the V3 or V4 so the
firmware names above apply.

## 2. The options, honestly

| | what you do | pros | cons |
|---|---|---|---|
| **A. A second ESP32 board with Wi-Fi companion firmware (recommended)** | buy a Heltec V3/V4, flash the Wi-Fi companion build, give it your Wi-Fi, set MeshScore to `tcp` | no cable to the PC; the radio sits wherever there is power + Wi-Fi (a window with a USB charger); PC can reboot/sleep without touching the radio | ~CA$30; one-time flashing; 2.4 GHz Wi-Fi must reach it |
| B. BLE with the nRF52 radio you already have | `transport = "ble"`, `ble_address = ...` (`meshscore scan-ble` finds it) | free; wireless | PC must be within BLE range of the radio (a few metres); Windows BLE can be flaky; the MeshCore phone app can't be connected at the same time |
| C. Keep USB, but put the bot on a tiny always-on computer next to the radio | Raspberry Pi / mini PC runs MeshScore beside the T096; it uses Wi-Fi only for internet | works with the radio you have; PC can be turned off; most reliable USB setup | needs a Pi (~CA$60+); you move the whole install |
| D. Keep it as is | - | free | the cable/cold/bumps killed it once already (the bot now reconnects by itself, but a hung radio still needs a replug) |

Recommendation: **A**. If you would rather spend nothing, **C** is only worth it if you already own a Pi.

## 3. Plan for A (about an hour, plus delivery)

Do this with the bot **still running on the T096**, so nothing is down while you prepare.

1. **Buy** a Heltec WiFi LoRa 32 **V3 or V4** (with its LoRa antenna). Confirm the exact model on the box.
2. **Flash** the Wi-Fi companion firmware for that exact board using the upstream MeshCore web flasher / release files
   (look for `..._companion_radio_wifi` or a `usb_tcp` build).
3. **Give it your Wi-Fi.** How depends on the build: some set SSID/password in the app or a serial console
   (ZephCore notes show `wifi.ssid`, `wifi.pwd`, `wifi.enabled`), upstream builds may need them compiled in. Follow that
   firmware's README. Use a **2.4 GHz** network.
4. **Give it a fixed address.** In your router, make a DHCP reservation for the board (say `192.168.1.50`). Note the
   address.
5. **Match the radio settings** to the old radio (frequency, bandwidth, spreading factor, coding rate, TX power) - the
   MeshCore app shows them. The new radio must match your local mesh.
6. **Set up the identity and channels** (the part people miss):
   * Give it the same name as the old radio. If the app lets you export the T096's private key and import it on the new board, do that:
     the contact QR and everyone's saved contact then stay valid. If not, it gets a new key; the pages build the new
     QR automatically, but players have to re-add the contact (the channels are unaffected).
   * Add every channel the bot uses, by name (`meshscore channel` lists what a radio has; `meshscore channel add #name`
     adds a hashtag channel, which needs no secret). MeshScore finds channels by **name**, so the slot numbers may differ.
7. **Test from the PC** (the bot keeps running on the USB radio meanwhile):
   ```
   python -m meshscore check --config test.toml
   ```
   where `test.toml` is a copy of `meshscore.toml` whose `[radio]` section is
   ```toml
   [radio]
   transport = "tcp"
   host = "192.168.1.50"
   tcp_port = 5000
   ```
   A companion radio accepts **one client at a time**: close the MeshCore phone/desktop app on that radio first.
8. **Cut over** (a 2 minute change): stop the bot, replace the `[radio]` section of the real `meshscore.toml` with the
   three `tcp` lines, then start the task:
   ```
   Stop-ScheduledTask -TaskName MeshScore
   (edit meshscore.toml)
   Start-ScheduledTask -TaskName MeshScore
   ```
   Check `meshscore.log` for `Connected. Channels: ...` and the game/sports channels listed.
9. **Place it**: antenna outside or in a window, USB charger for power, away from the cold if the battery is
   in it (lithium cells misbehave when cold; powering from a charger and leaving the battery out/off-window helps).
10. **Rollback**: put the old `[radio]` back (`transport = "serial"`, `port = "COM3"`) and plug the old radio in. Keep it
    until the Wi-Fi radio has run a few days.

## 4. After the move
* The PC no longer needs the radio plugged in, but it still runs MeshScore: keep the `MeshScore` scheduled task.
* If the Wi-Fi drops, the bot's reconnect loop retries by itself (5 s, doubling to 2 min) and logs `Transport error`.
* The bot now also resets a dead link by itself: after 3 channel posts in a row fail to send it drops and reconnects the
  radio, and logs `The radio refused a message: ...` with the radio's reason.

## 5. Until then (USB hygiene)
* Use a short, good data cable (not a charge-only one), plugged straight into the PC, not a hub.
* Don't move the radio or the cable while the bot runs; if you must, afterwards check `meshscore.log` for
  `TX OK` lines.
* If the log says `A device attached to the system is not functioning`, unplug the radio, wait 5 s, plug it in. The
  bot reconnects on its own within about two minutes.
