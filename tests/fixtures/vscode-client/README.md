# Real-client fixtures

`ClientUpstream` builds a deterministic harmless `mmw-fixture.hello` VSIX and
synthetic desktop/server/CLI archives. Those archives exercise download paths,
retention and hashes; they are not runnable Microsoft editor/server builds.

The files in this directory were recorded on 2026-10-14 from the Microsoft
Marketplace for **GitHub.github-vscode-theme 6.3.4 and 6.3.5**.
`signed-gallery.json` is the original latest-only query response;
`signed-history.json` is the all-version response (flags 403). The 6.3.4
assets live in its version directory. Assets for the two selected releases
are stored under their asset type; other history versions are metadata only.
`payloads.json` records original URLs, byte counts and SHA-256 checksums. The
VSIX's upstream `Microsoft.VisualStudio.Services.VsixSha256` is checked too.
The VSIX and `.sigzip` payload are unmodified so the stock Microsoft client
can perform its own signature verification without upstream access.

The theme is MIT-licensed; its original license and copyright notice are in
`Microsoft.VisualStudio.Services.Content.License` and in the VSIX. Source:
[primer/github-vscode-theme](https://github.com/primer/github-vscode-theme).

Recording used the same API request as the sync provider: extension-name
filter 7, latest-only flags 914, and API version `3.0-preview.1`. The URLs in
`payloads.json` are the recording sources; no test fetches them online.
