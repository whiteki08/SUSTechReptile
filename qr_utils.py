from __future__ import annotations

import base64
from io import BytesIO


def render_qr_ascii(text: str) -> str:
    """Render QR as monospaced ASCII blocks for terminal display."""
    try:
        import qrcode
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("qrcode package is required for ASCII rendering") from exc

    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    dark = "██"
    light = "  "
    lines = []
    for row in matrix:
        lines.append("".join(dark if cell else light for cell in row))
    return "\n".join(lines)


def build_qr_png_base64(text: str, *, box_size: int = 8, border: int = 2) -> str:
    """Build QR PNG and return raw base64 string."""
    try:
        import qrcode
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("qrcode package is required for base64 rendering") from exc

    qr = qrcode.QRCode(box_size=box_size, border=border)
    qr.add_data(text)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
