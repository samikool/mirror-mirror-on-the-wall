# Deployment parity

Publish and bundle must produce the same deployable file tree unless the
user explicitly requests a difference. Published trees may be copied directly
to removable drives for offline deployment; include all deployment scripts,
container assets, configuration, signing tools, and TLS assets in both paths.
Use shared generation code and test parity, including TLS-enabled networks
and snapshot replay.

Publishing must not automatically create snapshots. Publish snapshots remain
manual; bundling current content automatically creates its snapshot.
