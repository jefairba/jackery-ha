> [!WARNING]
> **Personal fork for one specific setup. Unsupported.** This copy is tuned for a single Explorer 5000 Plus + Smart Transfer Switch installation and may change or break without notice. Issues are disabled. **If you want this integration, use the actively maintained upstream: [turmacar/jackery-homeassistant](https://github.com/turmacar/jackery-homeassistant).**
>
> **Credits:** originally written by [theak](https://github.com/theak/jackery-homeassistant) (Akshay Kannan); Transfer Switch, circuit, plan and MQTT support by [turmacar](https://github.com/turmacar/jackery-homeassistant). This fork only adds local changes on top of their work. MIT licensed; see [LICENSE](LICENSE).

> **Note:** This integration targets the **Jackery** app backend (used by portable stations like the Explorer series and Smart Transfer Switch). If your device is managed by the **Jackery Home** app (e.g. HomePower 2000 Ultra, SolarVault), that app uses a different API; see [iLLixM/jackery_home_cloud-ha](https://github.com/iLLixM/jackery_home_cloud-ha) for a community integration targeting that backend.

# Jackery Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![fork](https://img.shields.io/badge/personal%20fork-%40jefairba-lightgrey.svg)](https://github.com/jefairba/jackery-ha)
[![version](https://img.shields.io/badge/version-1.2.0-blue.svg)](https://github.com/jefairba/jackery-ha)

Custom Home Assistant integration for monitoring and controlling Jackery portable power stations and the Smart Transfer Switch. Provides real-time sensors, writable controls, and automation services.

## What this fork changes

Differences from [turmacar/jackery-homeassistant](https://github.com/turmacar/jackery-homeassistant):

- **Credentials stay out of logs.** Tokens, the MQTT password, user ID, Wi-Fi name, LAN IP and MAC are masked; a failed login no longer logs the request URL (which carried the password in recoverable form).
- **Network outages are not "wrong password".** If Jackery can't be reached (e.g. HA boots before the internet is back after a power cut), setup retries instead of waiting for reauthentication. A standard *Reauthenticate* screen handles real password changes.
- **Per-install device identity** instead of one hardcoded ID shared by every install.
- **Commands must be confirmed.** A switch/select/number only changes in HA when the device reports the new value within 10 s; otherwise HA shows *rejected* or *unconfirmed* and re-reads the real state.
- **240V circuits are never left on one leg.** If either leg fails, both are returned to their previous state (with a loud warning if that fails too).
- **Stale data is visible.** A *Data Stale* sensor turns on after ~2 min without fresh cloud data; entities go unavailable after ~5 min (was 15).
- **Power-cutting controls are dropdowns, not switches.** Circuit On/Off, *UPS Mode* (Off/On) and *Grid / Station* (Grid/Battery) are selects, so "turn off" voice commands, area commands ("turn off the garage"), `homeassistant.turn_off` and scenes that sweep up switches can't reach them. Force Charge stays a switch.
- **Sharing the one Jackery login with the phone app.** Jackery allows one sign-in per account. When the app signs in, HA now steps aside for a configurable time (default 15 min; *Configure* on the integration) instead of immediately signing the app out. A *Yielding to Jackery App* sensor and a *Reclaim Jackery Session* button live on a *Jackery Cloud Session* device.
- **Devices shared to the account are discovered** (Jackery app sharing). Whether a shared account can *control* a device is decided by Jackery and has not been verified.

## Features

- Battery, power, and time-remaining sensors for portable power stations
- Writable switches, selects, and number entities for supported device settings
- Charging plan support for Jackery Plus portable models
- Smart Transfer Switch: grid/station toggle, UPS mode, working mode selection, circuit control, fault diagnostics, and scheduled charge/discharge plan management
- Per-circuit power monitoring with automatic split-phase pair combining
- Custom Lovelace cards for plan and circuit management

For the full entity reference, see the [wiki pages](wiki/Home.md) in this repository (upstream: [turmacar wiki](https://github.com/turmacar/jackery-homeassistant/wiki)).

## Installation

### HACS (Recommended)

1. Install [HACS](https://hacs.xyz/) if you have not already.
2. Add this repository as a custom repository in HACS.
3. Search for "Jackery" in the integrations section and click Download.
4. Restart Home Assistant.

HACS installs from published GitHub releases. To get unreleased fixes, HACS can also install the repository's default branch directly.

### Manual

1. Download or clone this repository.
2. Copy the `jackery` folder to your `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings** > **Devices and Services** > **Add Integration**.
2. Search for "Jackery" and select it.
3. Enter your Jackery account email and password.
4. Click **Submit**.

The integration will discover your devices and create all supported entities automatically.

## Requirements

- Home Assistant 2023.8.0 or newer
- Python 3.10 or newer

**Dependencies:**
- `requests>=2.31.0`
- `pycryptodomex>=3.19.0`
- `socketry>=0.2.4`

## Wiki

Full documentation is available in the [Wiki](../../wiki):

| Page | Contents |
|------|----------|
| [Charging Plans](../../wiki/Charging-Plans) | Scheduled charge/discharge plan details and protocol |
| [Controls](../../wiki/Controls) | Switches, selects, numbers, and text entities |
| [Device Availability](../../wiki/Device-Availability) | Which entities are created for which devices |
| [Example Automations](../../wiki/Example-Automations) | Sample automations for common use cases |
| [Lovelace Cards](../../wiki/Lovelace-Cards) | Custom card setup and features |
| [Portable Devices](../../wiki/Portable-Devices) | Portable station features, charging behavior, and quirks |
| [Properties](../../wiki/Properties) | Raw device property key reference |
| [Sensors](../../wiki/Sensors) | All sensor and binary sensor reference tables |
| [Services](../../wiki/Services) | HA services for Transfer Switch plan management |
| [Supported Devices](../../wiki/Supported-Devices) | Known supported hardware and compatibility notes |
| [Transfer Switch](../../wiki/Transfer-Switch) | Smart Transfer Switch working modes, circuits, fault diagnostics |
| [Troubleshooting](../../wiki/Troubleshooting) | Common issues and debug logging |

## Lovelace Cards

Custom Lovelace cards for the Transfer Switch are available in a separate repository:

**[jackery-lovelace-cards](https://github.com/turmacar/jackery-lovelace-cards)**

See [Lovelace Cards](../../wiki/Lovelace-Cards) in the wiki for details on each card.

## Contributing

Pull requests are encouraged and welcome! For major changes, open an issue first to discuss what you would like to change.

When changing `custom_components/jackery/manifest.json` version metadata, push the matching semantic version tag so HACS can install that version directly.

## License

MIT - see [LICENSE](LICENSE).

## Acknowledgments

- Based heavily on code from https://qiita.com/Hsky16/items/c163137265a87186ac39
- Thanks to the Home Assistant community for the excellent framework
- Special thanks to all contributors and users who provide feedback

---

**Note:** This is a community-driven integration and is not officially affiliated with Jackery. Use at your own risk.
