# Security

MeshScore connects to your own radio and to public sports data endpoints. It never asks for credentials, and the only
secret it can hold is a Bluetooth pairing PIN in your local config file.

* Anyone on a channel can send commands. Restrict `!mode` / `!phase` with `admin_keys` (DM, authenticated) in
  `meshscore.toml`; `admin_names` can be spoofed in a channel.
* Don't commit your `meshscore.toml` if it contains a BLE PIN or an address you consider private.

Found a security problem? Please open a GitHub security advisory on the repository (Security tab -> "Report a
vulnerability") rather than a public issue.
