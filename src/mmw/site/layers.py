"""Far-side layers: custom debs a site adds to a repository it serves.

A site layer never touches a stored version (deltas keep applying against
it, digest and all). Its rebuilt files are an *overlay* per (repository,
version, inputs digest); a repository serves `version ⊕ overlay`, so every
switch — apply, hold, roll back, forward — carries the layer. The rebuild is
the connected side's (`mmw.core.layers._rebuild`) over the site store.
"""
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from mmw.core import disk_io

LAYERS_DIR = "layers"


def site_layer_dir(home: Path, repo: str) -> Path:
    """`{suite}/{component}/*.deb` below it; the repository key made one safe segment."""
    return Path(home) / LAYERS_DIR / quote(repo, safe="")


LAYER_FORMAT = 1


@dataclass
class OverlayResult:
    digest: str | None = None          # None: serve the version as it is (or hold, below)
    hold_reason: str | None = None     # set: do not switch — keep what is served, held
    warnings: list[str] = field(default_factory=list)


class StoreBlobs:
    """The site store as a layer's blob store (see `mmw.core.layers.PoolBlobs`)."""

    def __init__(self, store):
        self.store = store

    def have(self, checksum: str, filename: str) -> bool:
        return self.store.has(checksum)

    def add_bytes(self, body: bytes, checksum: str, filename: str) -> None:
        self.store.add_stream(io.BytesIO(body), expect=checksum)

    def add_deb(self, deb) -> None:
        with disk_io.ops.open_rb(deb.path) as fh:          # copied, never moved: the folder is the user's
            self.store.add_stream(fh, expect=deb.sha256)


def _work(home: Path, repo: str) -> Path:
    return Path(home) / "local" / LAYERS_DIR / quote(repo, safe="")


def site_debs(home: Path, repo: str) -> dict[tuple[str, str], list]:
    """(suite, component) → the scanned debs a site added to `repo`."""
    from mmw.providers.apt_local import scan
    folder = site_layer_dir(home, repo)
    out: dict[tuple[str, str], list] = {}
    if not disk_io.ops.is_dir(folder):
        return out
    for suite_dir in sorted(p for p in disk_io.ops.iterdir(folder) if disk_io.ops.is_dir(p)):
        for comp_dir in sorted(p for p in disk_io.ops.iterdir(suite_dir) if disk_io.ops.is_dir(p)):
            debs = scan(comp_dir, _work(home, repo) / f"scan-{suite_dir.name}-{comp_dir.name}.json")
            if debs:
                out[(suite_dir.name, comp_dir.name)] = debs
    return out


def build_overlay(home: Path, db, store, repo: str, version_id: int) -> OverlayResult:
    """The overlay that serving `version_id` of `repo` needs, built (or
    found) and stored. No layer: no overlay. A layer that cannot be built
    — debs with upstream signing, no key — is a hold, with the reason."""
    from mmw.core import layers as core
    result = OverlayResult()
    layer = db.layer(repo)
    debs = site_debs(Path(home), repo)
    if not debs and (layer is None or layer.signing == "upstream"):
        return result
    state = db.repo_state(repo)
    label = state.label if state else repo
    if debs and (layer is None or layer.signing == "upstream"):
        result.hold_reason = (f"{label} has local packages, so its signature cannot stay — choose "
                              "resign or unsigned for it (mmw site packages signing)")
        return result
    fingerprint, public = "", None
    if layer.signing == "resign":
        from mmw.core.signing import Keyring, SigningError
        keyring = Keyring(home)
        try:
            found = next((k for k in keyring.list() if k.name == layer.key), None)
            if found is None:
                result.hold_reason = (f"{label} needs a signing key named {layer.key!r} here — import "
                                      "or generate it (mmw site signing-key)")
                return result
            fingerprint, public = found.fingerprint, keyring.public_armor(layer.key)
        except SigningError as e:
            result.hold_reason = f"{label}: {e}"
            return result
        finally:
            keyring.stop_agent()
    version = db.version_by_id(version_id)
    files = db.version_files(version_id)
    bases = (state.bases if state and state.bases else [repo])
    base = max((b for b in bases if any(p.startswith(b + "/dists/") for p in files)), key=len, default=None)
    if base is None:
        result.hold_reason = f"{label} v{version.number} has no dists/ tree to add packages to"
        return result
    rows = {p[len(base) + 1:]: (p[len(base) + 1:], str(store.path(cs)), 0, "", cs)
            for p, cs in files.items() if p.startswith(base + "/")}
    published = core._published(rows)
    missing = [f"{s}/{c}" for s, c in debs if c not in published.get(s, {})]
    if missing:
        result.hold_reason = (f"{label} has local packages for {', '.join(missing)}, which "
                              f"v{version.number} does not have")
        return result
    folder = site_layer_dir(Path(home), repo)
    chosen = core.read_decisions(folder)
    digest = hashlib.sha256(json.dumps({
        "format": LAYER_FORMAT, "version": version.digest,
        "debs": sorted((s, c, d.path.name, d.sha256) for (s, c), ds in debs.items() for d in ds),
        "layer": [layer.signing, layer.key, layer.contents, layer.drop_valid_until],
        "key": fingerprint, "decisions": chosen,
    }, sort_keys=True).encode()).hexdigest()
    clashes = core._clashes(label, rows, published, debs, chosen)
    for c in clashes:
        if c.filename not in chosen:
            what = ("the same version as upstream's, with different contents" if c.kind == "same-version"
                    else f"older than upstream's {c.upstream_version}")
            result.warnings.append(f"{label}: {c.filename} ({c.package} {c.version}) is {what} — kept")
    if layer.signing == "unsigned":
        result.warnings.append(f"{label} is served unsigned — unsafe; clients need [trusted=yes] for it")
    if db.has_overlay(repo, version_id, digest) and all(
            not cs or store.has(cs) for cs in db.overlay_files(repo, version_id, digest).values()):
        result.digest = digest
        return result
    dropped = {c.filename for c in clashes if chosen.get(c.filename) == "drop"}
    out = core._rebuild("site", quote(repo, safe=""), layer, rows, published, debs, dropped,
                        Path(home), _work(Path(home), repo), StoreBlobs(store))
    overlay = [(f"{base}/{rel}", cs, size) for rel, cs, size, _kind, _fn in out]
    if public is not None:
        from mmw.core.keys import dearmor
        key = dearmor(public if public.endswith("\n") else public + "\n")
        cs = hashlib.sha256(key).hexdigest()
        StoreBlobs(store).add_bytes(key, cs, f"{layer.key}.gpg")
        overlay.append((f"{base}/mmw-keys/{layer.key}.gpg", cs, len(key)))
    db.put_overlay(repo, version_id, digest, overlay)
    result.digest = digest
    return result


# ── what `mmw site packages` and the far-side TUI do ─────────────────────────

def _published_of(db, store, repo: str) -> tuple[str, dict, dict]:
    """(base, upstream rows relative to it, published dims) of the version served."""
    from mmw.core import layers as core
    state = db.repo_state(repo)
    if state is None or state.serving is None:
        raise core.PackagesError(f"{repo} serves nothing yet — apply a bundle first")
    files = db.version_files(state.serving.id)
    base = max((b for b in (state.bases or [repo]) if any(p.startswith(b + "/dists/") for p in files)),
               key=len, default=None)
    if base is None:
        raise core.PackagesError(f"{state.label} has no dists/ tree to add packages to")
    rows = {p[len(base) + 1:]: (p[len(base) + 1:], str(store.path(cs)), 0, "", cs)
            for p, cs in files.items() if p.startswith(base + "/")}
    return base, rows, core._published(rows)


def check_site_clashes(home: Path, db, store, repo: str, paths, suite: str | None,
                       component: str | None) -> tuple[str, str, list]:
    """(suite, component, clashes) adding `paths` would meet, nothing copied."""
    from mmw.core import layers as core
    _base, rows, published = _published_of(db, store, repo)
    suite = suite or (sorted(published)[0] if published else None)
    component = component or "main"
    if suite is None or component not in published.get(suite, {}):
        raise core.PackagesError(f"{repo} does not have {suite}/{component} — pick a suite and component it has")
    debs = [core._scanned(p) for p in core.collect_debs(paths)]
    return suite, component, core._clashes(repo, rows, {suite: {component: published[suite][component]}},
                                           {(suite, component): debs}, {})


def add_site_packages(home: Path, db, store, repo: str, paths, *, suite=None, component=None,
                      signing=None, key=None, decisions: dict[str, str] | None = None) -> list[str]:
    """Copy debs into the site layer of `repo` (setting its signing when it
    has none) — the caller relayers. Returns lines to tell."""
    from mmw.core import layers as core
    layer = db.layer(repo)
    if layer is None or layer.signing == "upstream":
        if signing not in ("resign", "unsigned"):
            raise core.PackagesError(core.needs_signing(db.repo_state(repo).label if db.repo_state(repo)
                                                        else repo), usage=True)
    if signing is not None:
        set_site_signing(home, db, repo, signing=signing, key=key)
    suite, component, clashes = check_site_clashes(home, db, store, repo, paths, suite, component)
    notes = []
    folder = site_layer_dir(Path(home), repo)
    chosen = core.read_decisions(folder)
    for c in clashes:
        choice = (decisions or {}).get(c.filename, "keep")
        if choice == "cancel":
            raise core.PackagesError("cancelled — nothing was added")
        chosen[c.filename] = choice
        notes.append(f"{c.filename} ({c.package} {c.version}) clashes with upstream's — {choice}")
    outcome = core.AddOutcome(None)
    core._copy_in(core.collect_debs(paths), folder / suite / component, outcome)
    if clashes:
        core.write_decisions(folder, chosen)
    notes += [f"added {p.name} to {suite}/{component}" for p in outcome.copied]
    notes += [f"already there: {p.name}" for p in outcome.skipped]
    return notes


def remove_site_package(home: Path, repo: str, filename: str) -> list[Path]:
    from mmw.core import layers as core
    folder = site_layer_dir(Path(home), repo)
    found = ([p for p in disk_io.ops.rglob(folder, "*.deb") if p.name == filename]
             if disk_io.ops.is_dir(folder) else [])
    if not found:
        raise core.PackagesError(f"{filename} is not in {repo}'s local packages")
    for p in found:
        disk_io.ops.unlink(p)
    return found


def list_site_packages(home: Path, db) -> list[tuple[str, str, str]]:
    out = []
    for state in db.repo_states():
        folder = site_layer_dir(Path(home), state.repo)
        if disk_io.ops.is_dir(folder):
            out += [(state.label, f"{p.parent.parent.name}/{p.parent.name}", p.name)
                    for p in sorted(disk_io.ops.rglob(folder, "*.deb"))]
    return out


def set_site_signing(home: Path, db, repo: str, *, signing=None, key=None, contents=None,
                     drop_valid_until=None) -> None:
    from mmw.core import layers as core
    layer = db.layer(repo)
    signing = signing or (layer.signing if layer else "upstream")
    key = key if signing == "resign" and key else (layer.key if layer and signing == "resign" else None)
    contents = layer.contents if contents is None and layer else bool(contents)
    dvu = layer.drop_valid_until if drop_valid_until is None and layer else bool(drop_valid_until)
    if signing == "resign" and not key:
        raise core.PackagesError("re-signing needs a key — --resign KEY", usage=True)
    if dvu and signing != "resign":
        raise core.PackagesError("dropping Valid-Until needs re-signing (anything else serves it unsigned)",
                                 usage=True)
    if signing == "resign":
        core._check_key(Path(home), key)
    db.set_layer(repo, signing=signing, key=key, contents=contents, drop_valid_until=dvu)


def site_sources_file(home: Path, db, store, repo: str, fmt: str) -> str:
    from urllib.parse import urlparse

    from mmw.core import layers as core
    from mmw.core.apt_sources import render_deb822, render_one_line, AptEntry
    from mmw.providers.apt_local import SOURCES_FORMATS
    if fmt not in SOURCES_FORMATS:
        raise core.PackagesError(f"format must be one of {', '.join(SOURCES_FORMATS)}", usage=True)
    layer = db.layer(repo)
    if layer is None or layer.signing == "upstream":
        raise core.PackagesError(f"{repo} has no local packages: its clients use upstream's own sources "
                                 "file and key")
    base, _rows, published = _published_of(db, store, repo)
    url = _repo_url(db, repo, base)
    parsed = urlparse(url)
    keyring = f"/etc/apt/keyrings/{layer.key}.gpg"
    head = [f"# {db.repo_state(repo).label} as this site serves it — with local packages."]
    if layer.signing == "resign":
        head += ["# Signed by this site's key, not upstream's. Install it first:",
                 "#   sudo install -d -m 0755 /etc/apt/keyrings",
                 f"#   sudo curl -fsSL {parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/mmw-keys/"
                 f"{layer.key}.gpg -o {keyring}"]
    else:
        head += ["# UNSIGNED — this is unsafe: apt cannot tell these files came from you."]
    render = render_deb822 if fmt == "deb822" else render_one_line
    body = [render(AptEntry(url.rstrip("/"), suite, sorted(published[suite]), []),
                   signed_by=keyring if layer.signing == "resign" else None,
                   trusted=layer.signing == "unsigned") for suite in sorted(published)]
    return "\n".join(head) + "\n" + ("\n" if fmt == "deb822" else "").join(body)


def _repo_url(db, repo: str, base: str) -> str:
    """The address clients use: the upstream URL the bundle's settings name
    (DNS points it here), else one made from the base."""
    from mmw.core.config import normalize_url
    try:
        settings = json.loads(db.get_meta("settings") or "{}")
    except ValueError:
        settings = {}
    for m in settings.get("mirrors", []):
        if normalize_url(m.get("source") or "") == repo and m.get("source_url"):
            return m["source_url"]
    host, _, path = base.partition("/")
    if "_" in host:
        name, _, port = host.rpartition("_")
        host = f"{name}:{port}" if port.isdigit() else host
    return f"https://{host}/{path}"
