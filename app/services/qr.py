"""QR codes for short links."""
import io

import segno


def _make(text: str) -> segno.QRCode:
    return segno.make(text, error='m', micro=False)


def svg(text: str, *, scale: int = 8) -> str:
    buffer = io.BytesIO()
    _make(text).save(buffer, kind='svg', scale=scale, border=2, xmldecl=False, svgns=True, nl=False)
    return buffer.getvalue().decode()


def svg_data_uri(text: str, *, scale: int = 8) -> str:
    return _make(text).svg_data_uri(scale=scale, border=2)


def png(text: str, *, scale: int = 10) -> bytes:
    buffer = io.BytesIO()
    _make(text).save(buffer, kind='png', scale=scale, border=2)
    return buffer.getvalue()
