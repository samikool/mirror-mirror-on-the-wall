"""Human-readable formatting helpers shared by all frontends."""

STALLED = object()  # sentinel for fmt_eta: no activity for too long


def fmt_size(n: float) -> str:
    """Decimal units (1 TB = 10¹² bytes), as drive labels and `df -H` use —
    dividing by 1024 under a "TB" label read ~9% low beside them."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


def fmt_speed(bps: float) -> str:
    return fmt_size(int(bps)) + "/s"


def fmt_duration(secs: float) -> str:
    if secs <= 0:
        return "…"
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def fmt_eta(secs) -> str:
    if secs is STALLED:
        return "--:--"
    if secs <= 0:
        return "..."
    return fmt_duration(secs)
