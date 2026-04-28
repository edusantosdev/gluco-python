# Bedside / stand glucose display (research notes)

This document captures design notes for a **dedicated physical display** that shows
blood glucose (value, trend arrow, optional range colors)—for example on a
**nightstand**—and how that relates to **GlucoBar** in this repository.

**These notes are not a build guide, product endorsement, or medical advice.**
GlucoBar and any DIY display are **not medical devices**. Always use your
official LibreView / LibreLink app and professional guidance for treatment
decisions.

---

## Requirements we discussed

- **Always on** — glanceable without unlocking a phone.
- **Content** — numeric value + **trend arrow**; optionally **green / orange /
  red** (or equivalent) for range bands.
- **Use case** — **bedroom nightstand**; prefer **no harsh “phone LCD” backlight**
  *or*, alternatively, embrace a **soft always-on glow** like a **1980s digital
  clock** (segment LEDs).
- **Interaction** — ideally **none** (no tap-to-wake required).
- **Solderless** — breadboard + pre-pinned modules + jumper wires for a first
  build.

---

## Software: what to port from GlucoBar

GlucoBar is split (see `ENGINEERING.md`):

| Part | Portable? | Notes |
|------|-------------|--------|
| **`daemon/`** | **Yes** (with work) | LibreView auth, polling, backoff, local HTTP API (`/health`, `/glucose/current`, `/glucose/graph`, etc.). Logical core to run on a small always-on computer (e.g. Raspberry Pi). |
| **`glucose_common/`** | **Yes** | Regions, trends, units, shared types. |
| **`macos_frontend/`** | **No** (replace) | AppKit / `rumps` menubar UI; wrong stack for ESP32/Pi segment displays. |
| **Credentials** | **Redo off macOS** | Today: LibreView credentials in **macOS Keychain**, forwarded to the daemon. On Linux/Pi/MCU: use a secure alternative (restricted file, OS keyring, NVS on a dedicated device, etc.). |
| **Packaging / lifecycle** | **Mac-specific** | `.app`, py2app, login items, parent-watch pipe—replace with your target platform. |

**Architectures:**

1. **Single device (e.g. Pi)** — Run the daemon; add a minimal UI (web, GPIO
   display library, etc.) that polls `http://127.0.0.1:8765`.
2. **MCU (e.g. ESP32)** — Either reimplement a minimal HTTPS LibreView client on
   device, or proxy via a home server (more moving parts).
3. **Two-device** — Mac runs GlucoBar; a remote display would need **LAN
   exposure** of the API (not how the project is oriented today; would be a
   deliberate change).

---

## Display technologies

### Reflective / “no glow” (e-paper)

- **Pros:** **Bistable**—image holds with very low power between updates; ideal
  for “always on” **without** light pollution. Good when the room is **not**
  pitch black (ambient light, night light, hallway spill).
- **Cons:** In **total darkness**, unlit e-paper is hard to see (like paper).
  **Optional:** very dim **warm front light** on a timer or rarely used—still
  sleep-friendlier than a bright LCD for many people.
- **Color:** Budget panels are often **black / white / red** (or yellow).
  **True green and orange** may need **7-color e-paper** (slower, pricier) or
  you encode range with **icons / layout / red accent** only.

### Emissive / “1980s digital clock” (7-segment LED, VFD, etc.)

- **Pros:** **Always readable in the dark** with **no interaction**—like a
  classic red or green **seven-segment** alarm clock. Trend via **extra LEDs**
  or an extra digit / small matrix.
- **Cons:** There **is** a **soft glow**—mitigate with **PWM dimming**, **red**
  (easier on dark adaptation than blue-white), **diffuser / enclosure**.
- **Range colors:** Whole modules are usually **one LED color**. Practical
  pattern: **digits in one color** + a **separate RGB or R/A/G “status” LED**
  (or one NeoPixel on a **breakout with pins** if staying solderless).

### Vacuum fluorescent (VFD)

- Very **80s appliance** aesthetic; needs a **proper high-voltage driver**—more
  advanced than a first MAX7219 build.

---

## Bedroom nightstand summary

- **Sleep-friendly + zero glow:** **e-paper**; accept low light or add **brief,
  dim front light** only when needed—*unless* you explicitly want a **clock-like
  glow**.
- **“Digital clock from the 80s but for glucose”:** **7-segment LED** (e.g.
  **MAX7219** module) + **PWM** + diffuser; optional discrete LEDs for **arrow**
  and **range**.

---

## Hardware shopping list (DIY, conceptual)

**Core**

| Qty | Item | Notes |
|-----|------|--------|
| 1 | ESP32 dev board (or Pi if running Python daemon) | Wi-Fi + HTTPS; pick a board **with headers soldered** for solderless builds. |
| 1 | USB **5 V** supply (≥ 1 A) + cable | Always-on wall power. |
| 1 | **MAX7219 8-digit** 7-segment module *or* **TM1637** 4-digit | Classic clock look; verify **pins pre-installed**. |
| 1 | Breadboard + Dupont jumpers (M–M, M–F) | Prototyping without soldering. |
| 1 | Enclosure + frosted front | Softens glow. |

**Trend arrow (pick one)**

- A few **through-hole LEDs + resistors** on breadboard (↑ / → / ↓), *or*
- Extra **digit** on MAX7219 for segment-hacked arrow, *or*
- Small **LED matrix** with driver (more wiring).

**Range colors**

- **RGB LED** or **three LEDs** (red / amber / green) + resistors on breadboard,
  *or*
- **Single NeoPixel breakout with pins** (mind 5 V / logic levels).

**Optional**

- Potentiometer for **hardware brightness**, better **USB supply**, small
  **caps** for cleaner LED power, **level shifter** only if a module demands 5 V
  logic.

---

## Solderless-specific notes

- Buy **ESP32** and **display modules** with **headers already soldered** (or
  vendor “assembled” option).
- Prefer **breakouts with pins**, not bare panels or strips with only solder pads.
- Avoid bare **ESP32 modules** (metal can) without a **carrier board**.

**Minimal solderless combo:** pre-headered ESP32 + pre-pinned MAX7219 + breadboard
LEDs for trend + one RGB (or R/A/G) for range + USB power.

---

## More “off the shelf” options

There is **no** widely sold **“LibreView 7-segment bedside clock”** as a single
retail product. Practical tiers:

| Approach | Tradeoff |
|----------|----------|
| **Small tablet / old phone** in a stand, **official LibreView / Libre app** | Most retail, least integration; not segment aesthetic; manage charging and screen glow. |
| **Gadget displays** (e.g. Tidbyt-class, LaMetric-class, some LED frames) | Compact and finished-looking; usually need **middleware** (home server, Nightscout, custom API)—**not** Libre-native out of the box. |
| **Integrated kits** (e.g. M5Stack, Matrix Portal–style) | One boxed look; you still **flash firmware** and handle **auth + HTTPS**. |

**Summary:** The **least DIY** path that still uses **vendor apps directly** is
often a **dedicated small screen** running the official app. A **custom segment
clock** look almost always means **MCU + display + your code** (or community
integrations on a smart display).

---

## References in this repo

- `README.md` — GlucoBar overview and disclaimer.
- `ENGINEERING.md` — daemon vs frontend, HTTP API, process model.

---

## Disclaimer (repeat)

This file is for **engineering exploration only**. It is **not** medical
advice. Abbott and Libre trademarks belong to their owners; this project is
**independent** and **not affiliated** with Abbott.
