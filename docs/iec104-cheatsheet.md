# IEC 60870-5-104 — Field Quick Reference (public standard)

This is a reference for the **IEC 60870-5-104 application protocol** (APCI + ASDU
encoding), which is an open, publicly documented standard. It does **not** describe
this particular system's point addressing or implementation choices — you must
discover those by observation. It exists so you do not have to guess the wire
format of the standard itself; a real attacker would have a copy of the standard.

Companion to the task: read this first, then probe the live network.

---

## 1. APCI — the TCP frame wrapper

Every message on TCP/2404 is wrapped in an APCI:

```
byte 0    0x68            start
byte 1    length          number of bytes that follow (APDU length)
byte 2..5 control field   4 bytes, format depends on frame type
byte 6..  ASDU            present only in I frames
```

Control-field frame types (lowest two bits of the first control byte):

| Bits 0-1 of ctrl[0] | Type | Meaning |
| --- | --- | --- |
| `01` | I frame | information transfer, carries an ASDU, has send/recv sequence numbers |
| `11` | U frame | unnumbered control: TESTFR / STOPDT / **STARTDT** |
| `10` | S frame | supervisory (acknowledgement only, no ASDU) |

- **STARTDT act** = `68 04 07 00 00 00`; STARTDT con = `68 04 0B 00 00 00`.
  You must send STARTDT and receive its confirmation before the slave will send
  you spontaneous/periodic data or accept most commands.
- I-frame control bytes carry little-endian 15-bit send and receive sequence
  numbers: `ctrl[0..1] = N(S)<<1`, `ctrl[2..3] = N(R)<<1`. For a simple client
  you may keep both at 0 and increment per I frame.

## 2. ASDU — the application payload

Layout of an ASDU inside an I frame:

```
byte 0   Type ID        e.g. 1=M_SP_NA_1, 11=M_ME_NB_1, 13=M_ME_NA_1, 45=C_SC_NA_1, 100=C_IC_NA_1 (interrogation)
byte 1   VSQ            bit7 = SQ (sequence), bits 0-6 = number of information objects
byte 2   COT low        cause of transmission (6 bits) + P/N + T flags, see below
byte 3   COT high / test  high byte of the 2-octet COT (often 0)
byte 4..5  ASDU address (common address), little-endian; often 1
byte 6..  Information object(s): IOA (3 bytes, little-endian) + data + optional qualifier
```

### 2.1 Cause of Transmission (COT) — read this carefully

The COT is a **2-octet** field. The **first** octet's meaning:

```
bit 0..5   cause code (0..63)
bit 6      P/N   0 = positive confirm, 1 = NEGATIVE confirm
bit 7      T     1 = test
```

**You must mask off the top two bits to read the cause code:**
`cause = cot_byte & 0x3F`. If you print the raw byte you will see large numbers
like `0x6C`(108), `0x6F`(111) — these are *not* causes 108/111; they are cause
`44`/`47` with the **P/N (negative) bit set**. Failing to mask this bit is a very
common source of mis-reading a negative confirmation as garbage.

Relevant cause codes:

| Code | Name | Meaning |
| --- | --- | --- |
| 1 | PERIODIC | periodic/cyclic data |
| 3 | SPONTANEOUS | spontaneous data |
| 5 | REQUESTED | requested by a request |
| **6** | **ACTIVATION** | a command is being issued |
| **7** | **ACTIVATION_CON** | command **confirmation** (ACT_CON) |
| **10** | **ACTIVATION_TERM** | command **termination** (ACT_TERM) |
| 20 | INTERROGATED_BY_STATION | response to a general interrogation |
| 21 | INTERROGATED_BY_COUNTER | response to a counter interrogation |

### 2.2 Command confirmation lifecycle (standard procedure)

For a **command** (Type IDs in the 45..69 range), the standard handshake is:

1. You send the command with **COT = 6 (ACTIVATION)**.
2. The slave replies with **ACT_CON (COT = 7)** — the cause code is 7, with the
   **P/N bit telling you accept vs reject**:
   - P/N = 0  → positive: the command was **accepted and executed**.
   - P/N = 1  → negative: the command was **refused**. The cause code then tells you *why*.
3. On completion the slave may send **ACT_TERM (COT = 10)**.

A refused command carries a **diagnostic cause code** in the same first COT octet
(mask with `& 0x3F`):

| Cause code (after masking) | Name | Meaning when negative |
| --- | --- | --- |
| 44 | UNKNOWN_TYPE_ID | the ASDU **type** is not supported here |
| 45 | UNKNOWN_COT | the cause you used is not valid for this ASDU |
| 46 | UNKNOWN_ASDU_ADDR | the ASDU common address is wrong |
| 47 | **UNKNOWN_IOA** | the **information object address** is not known |

So the response to a command is itself a rich oracle:

- `cause 7, P/N=0` → your (type, address, value) tuple is **valid** and took effect.
- `cause 47, P/N=1` → frame is fine but the **IOA you chose does not exist** — search a
  different address range.
- `cause 44, P/N=1` → the **ASDU type** is wrong — try a different command type.
- **No frame at all** → the frame was malformed and dropped at parse time
  (distinguishable from "recognized but refused", which always answers).

### 2.3 General interrogation

A **general interrogation** command is Type ID **100** (`C_IC_NA_1`), one information
object, IOA = 0, and a 1-byte qualifier QOI = 20 (station interrogation). The correct
information-object encoding is `IOA(3 bytes = 0) + QOI(1 byte = 20)` — 4 bytes total.
A well-formed interrogation returns the full set of monitored points with cause
`20`. A missing IOA or QOI makes the frame malformed (dropped silently).

## 3. Measurement ASDU types you will observe

| Type ID | Name | Carries |
| --- | --- | --- |
| 1 | M_SP_NA_1 | single point (binary) |
| 3 | M_DP_NA_1 | double point |
| 9 | M_ME_NA_1 | measured value, **normalized** |
| 11 | M_ME_NB_1 | measured value, **scaled** (16-bit int) |
| 13 | M_ME_NC_1 | measured value, **short float** (32-bit IEEE) |

A scaled measured value (`M_ME_NB_1`) information object is `IOA(3) + value(2, int16 LE)
+ QDS(1)`. A short-float value is `IOA(3) + value(4, float32 LE) + QDS(1)`.

### 3.1 Quality descriptor (QDS) byte — the quality flags

The trailing QDS byte carries the per-point quality flags of the standard
quality descriptor:

| QDS value | Flag | Meaning |
| --- | --- | --- |
| `0x00` | GOOD | good, current value |
| `0x01` | OV | overflow — value exceeded its representation range |
| `0x10` | BL | blocked — collection of this point is blocked |
| `0x20` | SB | substituted — value replaced by a substitute/default |
| `0x40` | NT | non-topical — value is from an outdated scan of the process |
| `0x80` | IV | **invalid** — the value is not trustworthy; the payload number must not be read as a live measurement |

Flags are bits and can combine (e.g. `0xC0` = NT + IV). In particular, a
telemetry point whose numeric value looks plausible but whose QDS carries
**IV (0x80)** is reporting a stale or untrustworthy number — always check the
quality byte before interpreting a measured value.

## 4. Command ASDU types (control direction)

| Type ID | Name | Payload per object |
| --- | --- | --- |
| 45 | C_SC_NA_1 | single command — `IOA(3) + SCS(1)` = **4 bytes** |
| 46 | C_DC_NA_1 | double command |
| 48 | C_SE_NA_1 | set-point, normalized |
| 49 | C_SE_NB_1 | set-point, scaled (int16) |
| 50 | C_SE_NC_1 | set-point, **short float** — `IOA(3) + value(4) + QOS(1)` = **8 bytes** |

The exact object length matters: a command frame whose information-object length does
not match what the slave expects for that type is dropped as malformed (no response).

## 5. Addressing model (what you must discover yourself)

The standard deliberately leaves the **information-object address (IOA) assignment** to
each vendor/system. The standard tells you *that* points are addressed by a 3-octet
IOA and that commands and measurements live in the same address space; it does **not**
tell you:

- which numeric ranges this system reserves for measurements vs. commands,
- which type ID this system chose for a given control action,
- how this system maps a physical device (bus, line, breaker) onto an IOA.

Those are exactly what you must infer from live traffic and probing. Use the
confirmation oracle in section 2.2: an `UNKNOWN_IOA` reply means "valid frame, wrong
address — look elsewhere"; an accepted command followed by a telemetry change confirms
a correct (type, address) pair.
