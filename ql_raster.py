"""
Brother QL-1050 label printing -- rendering, raster protocol, Windows spooler.

MIRRORED FILE. An identical copy lives in both:

    cairn/agent/ql_raster.py
    lf-label-printer/ql_raster.py

They are byte-identical on purpose. The two systems run on different machines
and in different repositories, but they must put exactly the same ink in exactly
the same place -- a label printed by one and an item logged under the other have
to be the same physical tag. When this file changes, copy it to the other side.

Nothing here knows about queues, HTTP, or where a job came from. It renders a
label, turns it into Brother raster commands, and hands the bytes to the Windows
spooler in RAW mode. That is the whole of it, which is why it can be shared.

Requires: pillow, pywin32.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

log = logging.getLogger("ql_raster")

# ── the printer ────────────────────────────────────────────────────────────
#
#  QL-1050 specifics. The head is 1296 dots wide at 300 dpi, which is 162 bytes
#  per raster line REGARDLESS of the roll loaded -- narrow media is printed by
#  padding the line, not by shortening it. Get the padding wrong and the label
#  comes out shifted sideways or blank, so these three numbers are the ones to
#  check first if the output looks off, and they are deliberately constants you
#  can edit rather than magic buried in a library.

PRINT_HEAD_BYTES = 162          # 1296 dots across the head
PRINT_HEAD_DOTS = PRINT_HEAD_BYTES * 8

# DK-2205, 62 mm continuous. 62 mm at 300 dpi is 732 dots of tape, of which 696
# are printable; the rest is the unprintable edge.
MEDIA_NAME = "DK-2205 (62 mm continuous)"
MEDIA_TYPE_CONTINUOUS = 0x0A
MEDIA_WIDTH_MM = 62
PRINTABLE_DOTS = 696

# Distance from the left edge of the head to the first printable dot. For the
# QL-1050 this is the label's own right margin (12) plus the model's offset
# (44), subtracted from the head width.
RIGHT_MARGIN_DOTS = 12 + 44
LEFT_OFFSET_DOTS = PRINT_HEAD_DOTS - PRINTABLE_DOTS - RIGHT_MARGIN_DOTS   # 544

# How far along the tape one label runs. Continuous tape has no fixed length,
# so this is ours to choose: 300 dots is about 25 mm, a 62 x 25 mm tag.
# Both spellings, because this file is shared with lf-label-printer, which has
# nothing to do with Cairn and should not need a CAIRN_ variable set on it.
LABEL_LENGTH_DOTS = int(
    os.environ.get("LABEL_LENGTH_DOTS")
    or os.environ.get("CAIRN_LABEL_LENGTH_DOTS")
    or "300"
)

# Tape fed after each label before the cut.
FEED_DOTS = 35



# ── label artwork ──────────────────────────────────────────────────────────

def _font(size: int, bold: bool = False):
    """
    Pick a real typeface, falling back rather than failing.

    A label that prints in the ugly built-in bitmap font is still a usable
    label; one that raises because a font file moved is not.
    """
    from PIL import ImageFont

    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    log.warning("No TrueType font found; falling back to the bitmap font.")
    return ImageFont.load_default()


def _us_date(iso: str) -> str:
    """8/31/2026 -- the format the office has always read off these labels."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso or ""
    return f"{d.month}/{d.day}/{d.year}"


def render_label(code: str, received_on: str, expires_on: str):
    """
    Draw one label, 1-bit, black on white, at print resolution.

    Same three lines the old .lbx template carried: received date, the item
    code large enough to read across a counter, expiry date.
    """
    from PIL import Image, ImageDraw

    img = Image.new("L", (PRINTABLE_DOTS, LABEL_LENGTH_DOTS), 255)
    draw = ImageDraw.Draw(img)

    small = _font(38)
    large = _font(112, bold=True)
    gap = 14

    lines = [
        (f"RCV {_us_date(received_on)}", small),
        (code, large),
        (f"EXP {_us_date(expires_on)}", small),
    ]
    # Measure before drawing, so the block sits in the middle of the tape. Laid
    # out from a fixed top margin instead, a short code and a long one produce
    # visibly different labels off the same roll.
    boxes = [draw.textbbox((0, 0), text, font=font) for text, font in lines]
    heights = [box[3] - box[1] for box in boxes]
    top = (LABEL_LENGTH_DOTS - (sum(heights) + gap * (len(lines) - 1))) / 2

    for (text, font), box, height in zip(lines, boxes, heights):
        x = (PRINTABLE_DOTS - (box[2] - box[0])) / 2 - box[0]
        draw.text((x, top - box[1]), text, font=font, fill=0)
        top += height + gap

    # Threshold rather than dither: this is type on white, and dithering type at
    # 300 dpi makes the edges mushy on thermal tape. Black on white, so --preview
    # writes something a person can look at; the printer's convention (a set bit
    # is a black dot) is applied in _raster_lines.
    return img.point(lambda p: 0 if p < 128 else 255, mode="1")


# ── Brother raster protocol ────────────────────────────────────────────────
#
#  Command reference: "Brother QL Series Command Reference" (raster mode).
#  Byte for byte, no interpretation by any driver, which is what makes the
#  output reproducible.

ESC = 0x1B


def _raster_lines(img) -> bytes:
    """
    One 'g' command per printed line.

    The image is printed along the tape, so image rows become raster lines. Each
    line is the full head width: the label's dots sit at LEFT_OFFSET_DOTS and
    the rest of the head is blank.

    The rows come straight out of Pillow's packed 1-bit buffer and are shifted
    into place as integers rather than pixel by pixel -- a 200-label job is
    forty million pixels, and touching each one in Python turns a two-second
    print into most of a minute.
    """
    width, height = img.size
    if width != PRINTABLE_DOTS:
        raise ValueError(f"Label is {width} dots wide; the roll takes {PRINTABLE_DOTS}.")

    stride = (width + 7) // 8
    packed = img.tobytes()          # MSB first, rows padded to a byte boundary
    header = bytes([0x67, 0x00, PRINT_HEAD_BYTES])
    # Pillow packs mode "1" with a set bit meaning WHITE; the printer's set bit
    # means a black dot, so every row is inverted. Done with an XOR on the whole
    # row rather than with ImageChops.invert, which silently returns all-ones
    # for 1-bit images in current Pillow and would print every label solid black.
    invert = (1 << width) - 1

    out = bytearray()
    for y in range(height):
        row = packed[y * stride:(y + 1) * stride]
        # Left-align the row in the head, invert, then push it right by the margin.
        dots = (int.from_bytes(row, "big") >> (stride * 8 - width)) ^ invert
        line = (dots << RIGHT_MARGIN_DOTS).to_bytes(PRINT_HEAD_BYTES, "big")
        out += header + line
    return bytes(out)


def build_job(labels: list[dict]) -> bytes:
    """
    Turn a job's labels into the exact byte stream the printer consumes.

    Deterministic and side-effect free -- which is why --dump exists. If a label
    ever comes out wrong, the bytes that produced it can be captured and
    compared without a printer in the room.
    """
    out = bytearray()

    # Clear whatever state a previous job left behind. 200 null bytes flushes a
    # half-received command out of the printer's buffer; ESC @ resets it.
    out += b"\x00" * 200
    out += bytes([ESC, 0x40])
    # Raster mode (as opposed to the template/P-touch modes).
    out += bytes([ESC, 0x69, 0x61, 0x01])

    for page, spec in enumerate(labels):
        img = render_label(spec["code"], spec.get("receivedOn", ""), spec.get("expiresOn", ""))
        raster = _raster_lines(img)
        lines = img.size[1]

        # Print information: tell the printer what media to expect and how many
        # raster lines are coming. Declaring it lets the printer refuse a
        # mismatch instead of printing a mangled label on the wrong roll.
        out += bytes([ESC, 0x69, 0x7A])
        out += bytes([0x86])                      # media type + width valid, recover on
        out += bytes([MEDIA_TYPE_CONTINUOUS])
        out += bytes([MEDIA_WIDTH_MM])
        out += bytes([0x00])                      # length: 0, continuous tape
        out += lines.to_bytes(4, "little")
        out += bytes([0x00 if page == 0 else 0x01])
        out += bytes([0x00])

        out += bytes([ESC, 0x69, 0x4D, 0x40])     # auto cut on
        out += bytes([ESC, 0x69, 0x41, 0x01])     # cut after every label
        out += bytes([ESC, 0x69, 0x4B, 0x08])     # cut at end of the job
        out += bytes([ESC, 0x69, 0x64]) + FEED_DOTS.to_bytes(2, "little")
        out += bytes([0x4D, 0x00])                # no compression: simpler, and
                                                  # a 62x25 mm label is tiny anyway

        out += raster
        # 0x0C prints this page and keeps going; 0x1A prints and ends the job.
        out += bytes([0x1A if page == len(labels) - 1 else 0x0C])

    return bytes(out)


# ── Windows spooler ────────────────────────────────────────────────────────

# Flags from winspool. Checked by name rather than by number at the call site,
# because "the printer said 0x00000090" is not something anyone should have to
# decode at a counter on a Monday.
PRINTER_PROBLEMS = [
    (0x00000001, "the printer is paused"),
    (0x00000002, "the printer reported an error"),
    (0x00000080, "the printer is offline"),
    (0x00000008, "the paper is jammed"),
    (0x00000010, "the printer is out of labels"),
    (0x00000400, "the printer cover is open"),
    (0x00000800, "the printer needs attention (paper or ribbon)"),
    (0x00200000, "the printer door is open"),
    (0x00000020, "the printer needs paper loaded by hand"),
]

JOB_ERROR_FLAGS = [
    (0x00000002, "the spooler reported an error on the job"),
    (0x00000004, "the job was deleted"),
    (0x00000100, "the job was cancelled"),
    (0x00000800, "the job was blocked by the device"),
    (0x00000200, "the job was aborted"),
]
JOB_COMPLETE = 0x00001000     # JOB_STATUS_PRINTED


class PrinterError(RuntimeError):
    """A failure the person at the printer can act on."""


def _win32print():
    try:
        import win32print
    except ImportError as exc:  # pragma: no cover - only on a non-Windows box
        raise PrinterError(
            "pywin32 is not installed. Run: pip install pywin32"
        ) from exc
    return win32print


def list_printers() -> list[str]:
    win32print = _win32print()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def check_printer(name: str) -> None:
    """
    Refuse to print into a known-bad state.

    Cheaper and far clearer than discovering afterwards that 40 labels went into
    a jammed printer: the job comes back to the queue with the actual reason,
    which is what Cairn shows on the Labels screen.
    """
    win32print = _win32print()
    available = list_printers()
    if name not in available:
        raise PrinterError(
            f"Windows has no printer called {name!r}. It can see: "
            + (", ".join(available) or "nothing at all")
        )
    handle = win32print.OpenPrinter(name)
    try:
        status = win32print.GetPrinter(handle, 2).get("Status", 0)
    finally:
        win32print.ClosePrinter(handle)

    problems = [text for flag, text in PRINTER_PROBLEMS if status & flag]
    if problems:
        raise PrinterError(f"{name}: " + "; ".join(problems) + ".")


def send_raw(name: str, data: bytes, doc_name: str, timeout: float = 120.0) -> str:
    """
    Hand the bytes to the spooler and wait for the spooler to be finished
    with them.

    RAW datatype means the driver does not touch the stream -- what is built in
    build_job() is what reaches the printer.

    Returning early would recreate the old bug in a new place, so this waits for
    the job to leave the queue and treats a job that leaves in an error state as
    a failure. A job that is still queued when the timeout expires is also a
    failure: it is genuinely unknown whether it printed, and reporting unknown
    as success is how the old system lost labels.
    """
    win32print = _win32print()
    check_printer(name)

    handle = win32print.OpenPrinter(name)
    try:
        job_id = win32print.StartDocPrinter(handle, 1, (doc_name, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            written = win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
        if written != len(data):
            raise PrinterError(
                f"The spooler accepted {written} of {len(data)} bytes for {doc_name}."
            )
        return _await_job(win32print, handle, name, job_id, timeout)
    finally:
        win32print.ClosePrinter(handle)


def _await_job(win32print, handle, name: str, job_id: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last_seen = "queued"
    while time.monotonic() < deadline:
        try:
            info = win32print.GetJob(handle, job_id, 1)
        except Exception:
            # The job is gone from the queue, which for a RAW job means the
            # spooler handed every byte to the port. That is as far as the
            # spooler's knowledge goes and as far as ours can go too.
            return f"spooled to {name}"

        status = info.get("Status", 0)
        errors = [text for flag, text in JOB_ERROR_FLAGS if status & flag]
        if errors:
            raise PrinterError(f"{name}: " + "; ".join(errors) + ".")
        if status & JOB_COMPLETE:
            return f"printed on {name}"

        # A printer that goes offline mid-job stalls rather than erroring.
        try:
            check_printer(name)
        except PrinterError as exc:
            raise PrinterError(f"{exc} (the job was already in the queue)") from exc

        last_seen = f"status 0x{status:08x}"
        time.sleep(0.4)

    raise PrinterError(
        f"{name} did not finish the job within {timeout:.0f}s ({last_seen}). "
        "Whether it printed is unknown, so it has not been marked done."
    )
