# Abbott LibreLinkUp API v5, EU context, and alternatives

Research notes logged from discussion (2026-07-21).  
Not medical advice. Not affiliated with Abbott. GlucoBar and DIY tools are not medical devices.

---

## 1. What’s changing: LibreLinkUp API v5

### Summary

Abbott’s **LibreLinkUp (LLU) API 5.0.0** encrypts the **glucose payload itself** (application-layer encryption), not just transport TLS. Third-party clients that previously parsed plain JSON from LLU 4.x can no longer read readings through the official v5 path.

### Timeline / community signal

- Discussed publicly around **April 2026** (e.g. [nightscout-librelink-up discussion #228](https://github.com/timoschlueter/nightscout-librelink-up/discussions/228)).
- Android LLU 5 followed; community treats that as a step toward shutting down LLU 4.
- Maintainers plan to **keep v4 alive as long as Abbott does**; when v4 endpoints die, unofficial cloud scrapers break.

### Technical picture (community reverse-engineering)

| Aspect | LLU 4.x | LLU 5.x |
|--------|---------|---------|
| Auth / login | Plain JSON (JWT, etc.) | Login may still look like plain JSON |
| Glucose responses | Readable JSON | Encrypted ciphertext |
| Client binding | Version / product headers, `Account-Id` hash, etc. | App-bound crypto; reports of per-user / per-device keys |
| Difficulty for OSS | Manageable (unofficial but workable) | White-box / heavily obfuscated; distributing keys is legally and practically hard |

Related hardening elsewhere:

- Libre 3 app: white-box crypto (Zimperium / Chow-style secure key bag patterns reported by community).
- “Libre by Abbott” and LLU 5 described as next-generation protected.
- LibreView upload path for Libre 3 already more locked down; download/cloud scrape expected to follow the same direction.

### Why Abbott is doing this (inferred)

Abbott has not published a friendly third-party v5 migration guide. Likely motives:

1. **Stop unauthorized API use** — close the long-lived reverse-engineered LLU surface (Nightscout bridges, menubar apps, etc.).
2. **Medical / privacy hardening** — CGM data is sensitive; payload encryption goes beyond HTTPS.
3. **Ecosystem control** — push users toward official apps and approved partners.
4. **Align with newer apps** — same crypto posture as Libre 3 / Libre by Abbott.

This is **deliberate lock-down of unofficial clients**, not “TLS was missing before.”

### Legal note (EU)

Circumventing technical protection measures and redistributing keys/crypto can conflict with EU law even when “interoperability research” is discussed. Community maintainers generally will **not** ship decryptors for v5.

### Impact on GlucoBar (this repo)

- Daemon uses **`pylibrelinkup`** against LibreView / LLU-style backends and assumes **readable JSON**.
- Same risk class as Nightscout LLU uploaders: **works while v4 lives; not casually adaptable to v5** without Abbott cooperation or a different data path (BLE hub, another CGM, etc.).
- Durable redesign direction discussed: daemon talks to **Nightscout or a local Juggluco/xDrip HTTP feed**, not LibreView scrape.

---

## 2. Portugal / Europe context

- Portugal is in the **EU Libre sensor market** (not US-coded sensors).
- Typical sensors: **Libre 2, Libre 2+, Libre 3** depending on prescription / SNS.
- Glucose units commonly **mmol/L**.
- “EU BLE” third-party tooling is more relevant here than US Libre 2 setups.

---

## 3. What “EU BLE” means

**EU BLE** = **Bluetooth Low Energy direct from phone → sensor**, using **European-market Libre sensors**, without Abbott’s cloud.

Abbott ships region-specific sensor variants. Community reality:

| | EU Libre 2 / 2+ (Portugal, most of Europe) | US / CA / AU Libre 2 |
|--|--------------------------------------------|----------------------|
| Direct BLE to xDrip+ / many DIY apps | Yes (widely supported) | No / much harder |
| Juggluco | Yes | Yes (heavier; needs newer Android) |

**Libre 3** also uses BLE, but with a harder encrypted handshake. Juggluco is the usual Android answer for Libre 3 direct connection; classic “xDrip EU BLE” docs mostly mean **Libre 2 EU**.

---

## 4. What is Juggluco?

**Juggluco** (“juggle glucose”) is a third-party **Android** app by Jaap Kaltes that reads CGM sensors over NFC/Bluetooth instead of (or instead of relying on) Abbott’s apps for the live connection.

- Site: https://www.juggluco.nl/Juggluco/
- Source: https://github.com/j-kaltes/Juggluco

### What it does

1. NFC-scans the sensor to start or take over a session.
2. Receives ~1-minute glucose over Bluetooth (Libre 2/2+/3/3+, plus other brands).
3. Shows graphs, alarms, widgets; Wear OS support.
4. Can export to Nightscout, LibreView, Health Connect, local web server, broadcasts to other apps.

### Caveats

- **Android only** (+ Wear OS). No official iOS Juggluco.
- Usually don’t run official LibreLink / Libre 3 app as the active Bluetooth owner at the same time (interference; EU Libre 2 is slightly more forgiving but still messy).
- Not an Abbott product; DIY / #WeAreNotWaiting style.
- Can still upload to LibreView so clinics keep seeing data.

---

## 5. Can Juggluco be ported to iOS?

### Short answer

**Not as a straightforward port.** For Libre 2 EU on iPhone you usually don’t need Juggluco — use existing iOS apps. For Libre 3, crypto is the blocker.

### Why a port is hard

Juggluco is not a thin UI; it includes:

- Android BLE + background services
- NFC sensor takeover
- Native crypto libraries (especially Libre 3)
- Wear OS, broadcasts, embedded web server, etc.

Rewriting in Swift means **reimplementing protocol + crypto**, not translating screens. Libre 3 key derivation / white-box material has blocked standalone iOS bridges ([example: Juggluco issue #433](https://github.com/j-kaltes/Juggluco/issues/433)).

iOS platform constraints also hurt always-on CGM: background BLE limits, App Store policy, TestFlight/sideload friction.

### What already exists on iPhone

| Sensor | Direct BLE on iOS? | Notes |
|--------|--------------------|-------|
| Libre 2 / 2+ (EU) | Yes | **xDrip4iOS** (and similar) can be master over BLE |
| Libre 3 / 3+ | Not really alone | Often relied on LibreLinkUp follower; pure iOS BLE still research-grade |

### Libre 3 + iPhone workarounds discussed by community

1. Spare Android as collector (Juggluco) → Nightscout / LAN → iPhone/Mac display
2. Hybrid research: iPhone BLE + Android crypto shim
   - https://github.com/poml88/libre3-auth-ios-sample-app
   - https://github.com/poml88/libre3-auth-android-server
3. Wait for a mature offline Swift bridge
4. Stay on Abbott apps / hope LLU v4 lasts
5. Switch CGM (e.g. Dexcom) for stronger official third-party iOS support

---

## 6. Alternatives when LLU cloud scrape dies

Grouped by *how you get data*.

### 6.1 Stay on Libre — talk to the sensor (bypass cloud)

| Option | Platform | Sensors (typical) | Notes |
|--------|----------|-------------------|-------|
| Juggluco | Android | Libre 2/2+/3/3+, others | Main community pick for Libre 3 BLE; Nightscout / LibreView / local HTTP |
| xDrip+ | Android | Libre 2 EU, others | Mature; Nightscout ecosystem |
| Diabox / Tomato | Android / iOS | Libre variants | Support varies by sensor/region |
| xDrip4iOS / Spike / Zukka | iOS | Libre 2 EU BLE; Libre 3 often via LLU historically | LLU follower path dies with v5 |
| DIY BLE clients | Android / custom | Libre 3/3+ emerging | Fragile, research-grade |
| NFC scan apps | Phone | Libre 1 / some Libre 2 | Not a real-time CGM replacement |

**Tradeoff:** Works without Abbott API. Usually need an Android phone near the sensor. Official Abbott app often can’t share the same sensor session cleanly.

### 6.2 Stay on Libre — official / partner cloud

| Option | Audience | Notes |
|--------|----------|-------|
| LibreLink / Libre by Abbott + LibreLinkUp | Patients & caregivers | First-party only |
| LibreView web / clinic portal | You + HCPs | Reports / sharing — not a live menubar feed |
| Epic ↔ LibreView | Clinics (US) | EHR integration, not DIY |
| Enterprise aggregators (e.g. Validic) | Companies / clinics | Contractual Abbott marketplace access |
| Historical partners (mySugr, Diasend, Social Diabetes, etc.) | Patients | Availability varies by country |

**Tradeoff:** Durable and legitimate, but **no public Abbott hobbyist API**.

### 6.3 Stay on Libre — use a hub

| Hub | How data gets in | Then |
|-----|------------------|------|
| Nightscout | Juggluco / xDrip / (until death) LLU bridge | Any Nightscout client / watch / custom UI |
| Health Connect (Android) | Juggluco → HC | Other apps read HC |
| Apple Health | Limited Libre paths | Weaker for live Libre than Dexcom |
| Tidepool | Some pumps/CGMs / uploads | More clinic/research oriented |

**Tradeoff:** Best “display anywhere” if the hub is fed from BLE. Pure LLU→Nightscout bridges die with v4.

### 6.4 Switch CGM brand

| Path | Why |
|------|-----|
| Dexcom G6/G7/ONE+ + Share / Follow | Documented Share ecosystem; Nightscout, Loop, watches |
| Dexcom Clarity / developer programs | More official third-party story than Abbott |
| Eversense | Different implantable ecosystem |
| Other CGMs (Sibionics, Accu-Chek SmartGuide, CareSens, …) | Often friendlier with Juggluco/xDrip; region/insurance limited |

### 6.5 Change the product goal

- Use Abbott UI only (phone / watch / official followers)
- Manual LibreView Excel/PDF export (not live)
- Hardware display from BLE phone (Android collector → local HTTP → Pi/ESP32)
- Advocacy to Abbott about open APIs (low hit rate so far)

### 6.6 Not durable

| Idea | Why not |
|------|---------|
| Decrypt LLU v5 in open source | White-box / per-device keys; legal risk; rotations |
| Pretend v4 forever | Works until Abbott turns it off |
| “Just use LibreView web API instead” | Same company stack; encryption expected to spread |
| Hope Epic/Validic feeds GlucoBar | B2B/clinical, not consumer OAuth for menubar apps |

---

## 7. Decision map (Portugal-oriented)

```text
Need live DIY display (GlucoBar-class)?
├─ Keep Libre sensors
│  ├─ Android OK
│  │  ├─ Libre 2/2+ → Juggluco or xDrip+ (EU BLE) → Nightscout / local HTTP → UI
│  │  └─ Libre 3    → Juggluco (BLE) → Nightscout / local HTTP → UI
│  └─ iPhone only
│     ├─ Libre 2/2+ EU → xDrip4iOS (direct BLE)
│     └─ Libre 3       → spare Android collector, Abbott apps, or switch CGM
├─ Want official APIs & least drama → Dexcom (+ Share / Nightscout)
└─ Only care about clinic view → LibreView / partner portals
```

### GlucoBar-specific durable direction

Rebuild data source as:

1. **Nightscout API**, or
2. **Local Juggluco / xDrip HTTP feed**,

not LibreView / LLU scrape.

---

## 8. Key external references

- [New LLU API 5.0.0 discussion (nightscout-librelink-up #228)](https://github.com/timoschlueter/nightscout-librelink-up/discussions/228)
- [Juggluco](https://www.juggluco.nl/Juggluco/) · [GitHub](https://github.com/j-kaltes/Juggluco)
- [Juggluco Libre 3 notes](https://www.juggluco.nl/Juggluco/libre3/)
- [xDrip4iOS docs](https://xdrip4ios.readthedocs.io/)
- [Libre 3 iOS + Android crypto shim (research)](https://github.com/poml88/libre3-auth-ios-sample-app)
- [LibreView unofficial API docs (historical 4.x era)](https://github.com/FokkeZB/libreview-unofficial)
- Historical LLU HTTP notes: https://gist.github.com/khskekec/6c13ba01b10d3018d816706a32ae8ab2

---

## 9. Open questions (not settled)

- Exact date Abbott will shut down LLU 4 endpoints
- Whether any consumer-facing official Abbott API will appear
- Whether a fully offline Libre 3 iOS BLE stack matures
- Whether GlucoBar should prioritize Nightscout vs local Juggluco HTTP as the first alternate backend
