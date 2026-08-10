# RoscomVPN routing mirror + geodata compatibility

This repository keeps Happ routing compatible with subscriptions that inject
`geosite:yandex` and `geoip:ru`, while continuously following the latest
RoscomVPN routing and geodata changes.

It also publishes a **stable GitHub Pages installer URL**, so the URL you give
to users does not change when the underlying Happ deeplink changes.

## Sources

The workflow follows:

- `hydraponique/roscomvpn-routing`
- `hydraponique/roscomvpn-geosite`
- `hydraponique/roscomvpn-geoip` (`release` branch)
- `v2fly/domain-list-community`
- `v2fly/geoip` (`release` branch)

RoscomVPN's geosite source remains the base. Only the missing V2Fly `yandex`
category and its recursive `include:` dependencies are added.

RoscomVPN's released `geoip.dat` remains the GeoIP base, including its custom
`direct`, `whitelist`, and `private` semantics. The standard V2Fly `RU` entry
is appended without replacing or modifying those RoscomVPN entries.

## What is generated

### Main branch

Generated Happ profiles:

- `HAPP/DEFAULT.JSON`
- `HAPP/DEFAULT.DEEPLINK`
- `HAPP/WHITELIST.JSON`
- `HAPP/WHITELIST.DEEPLINK`
- `HAPP/JSONSUB.JSON`
- `HAPP/JSONSUB.DEEPLINK`

The exact upstream profile set is mirrored, so files can appear/disappear as
RoscomVPN changes.

### Release branch

The workflow force-updates a small binary-only `release` branch:

- `geosite.dat`
- `geosite.dat.sha256`
- `geoip.dat`
- `geoip.dat.sha256`

Generated Happ profiles use these stable CDN URLs:

```text
https://cdn.jsdelivr.net/gh/OWNER/REPO@release/geosite.dat
https://cdn.jsdelivr.net/gh/OWNER/REPO@release/geoip.dat
```

### GitHub Pages

The workflow publishes a stable installer site.

For a repository named `OWNER/REPO`, the normal URLs are:

```text
https://OWNER.github.io/REPO/
https://OWNER.github.io/REPO/install/default/
https://OWNER.github.io/REPO/install/whitelist/
https://OWNER.github.io/REPO/install/jsonsub/
```

The `install/default/` URL stays the same. The page is regenerated with the
latest Happ deeplink every time the workflow runs.

Machine-readable endpoints are also published:

```text
https://OWNER.github.io/REPO/latest.json
https://OWNER.github.io/REPO/routing/default.json
https://OWNER.github.io/REPO/routing/default.txt
https://OWNER.github.io/REPO/panel/default-routing-header.txt
https://OWNER.github.io/REPO/panel/default-subscription-body.txt
https://OWNER.github.io/REPO/geosite.dat
https://OWNER.github.io/REPO/geosite.dat.sha256
https://OWNER.github.io/REPO/geoip.dat
https://OWNER.github.io/REPO/geoip.dat.sha256
```

`latest.json` is intended for scripts/panels. It contains both current geodata
SHA-256 hashes, upstream commit IDs, stable binary URLs, validated category
lists, and all profile endpoints.

## First-time setup

1. Create a GitHub repository and upload these files to its `main` branch.
2. In **Settings → Actions → General → Workflow permissions**, allow GitHub
   Actions to write repository contents if your repository/organization policy
   does not already allow it.
3. In **Settings → Pages**, choose **GitHub Actions** as the publishing source.
4. Run **Actions → Sync RoscomVPN geodata compatibility → Run workflow**.
5. Open the workflow's `deploy-pages` job. GitHub will show the deployed Pages
   URL.
6. Give users the permanent URL:

```text
https://OWNER.github.io/REPO/install/default/
```

They can bookmark the same URL permanently.

## Optional custom domain

If you use a custom Pages domain, create a repository Actions variable:

```text
PUBLIC_BASE_URL=https://routing.example.com/
```

Then run the workflow again.

The variable is only used when generating public links/manifests. Configure the
custom domain itself in GitHub Pages settings.

## Panel integration

Happ accepts routing deeplinks through a `routing` HTTP header or by putting the
deeplink into the subscription body.

This repository therefore exposes two ready-to-use current values:

```text
/panel/default-routing-header.txt
/panel/default-subscription-body.txt
```

Important: those are **stable URLs containing the current value**. A panel must
either be able to fetch that URL or have an updater/API hook that copies the
current value into its subscription template.

If your panel only stores a literal `routing` header value and never fetches a
remote source, GitHub Pages alone cannot change that value inside the panel.
For full zero-touch injection, add a panel-specific update step to the Action
using the panel's API.

`latest.json` is the recommended source for that updater.

## Why this fixes `geosite:yandex > EOF`

RoscomVPN intentionally minimizes its geosite data to categories required by
its own routing profile. If a separate subscription adds `geosite:yandex`, the
Xray core can fail because that standalone category is not present.

This mirror keeps RoscomVPN's custom categories and adds `yandex`, rather than
replacing RoscomVPN's geosite with a generic database.

The routing rule itself is not changed. If a subscription injects
`geosite:yandex`, it will now resolve against the augmented geosite database.

## Why this fixes `geoip:ru > EOF`

RoscomVPN's optimized `geoip.dat` intentionally exposes the custom categories
`direct`, `whitelist`, and `private`; it does not expose a country-code `RU`
entry. The upstream Happ profiles point at that custom file, and the previous
mirror did not change `Geoipurl`. Consequently, an additional subscription
rule using `geoip:ru` failed even though the GeoIP download was valid.

This mirror decodes RoscomVPN's released V2Ray protobuf, preserves all of its
entries, appends the `RU` entry from V2Fly's standard GeoIP release, and patches
every generated profile's `Geoipurl` to the mirror's stable release URL.

## Pre-publish validation

Before any release or profile is published, the workflow parses every
generated Happ profile and recursively collects all `geosite:*` and `geoip:*`
references. A small Go validator decodes both V2Ray protobuf files and requires
every referenced category to exist and contain rules. It always adds
`geosite:yandex` and `geoip:ru` to the requirements even when the current
upstream profiles do not mention the subscription-injected rules.

Missing, empty, corrupt, or protobuf-incompatible geodata fails the build with
the missing category names before the release branch, main branch, or Pages
artifact is updated.

## iOS and `UseChunkFiles`

Generated compatibility profiles set `UseChunkFiles` to `false`. Happ on iOS
uses chunk mode to extract only the geodata categories explicitly present in
the routing profile. Categories injected later by a subscription, including
`geosite:yandex` and `geoip:ru`, are therefore absent from the extracted file
even when the downloaded full database contains them.

Full-file mode avoids that mismatch. The RoscomVPN-optimized compatibility
files are small enough that this does not create the memory pressure which
chunk mode is intended to prevent on iOS.

## Update behavior

The Action runs hourly at minute 17 and can also be run manually.

It:

1. pulls all five upstream repositories/branches;
2. rebuilds the augmented geosite and GeoIP files;
3. validates all profile and compatibility categories;
4. compares both resulting SHA-256 hashes and profile content;
5. updates `release` only when geodata changes and `main` only when any
   material generated content changes;
6. rebuilds and deploys the permanent installer site on every successful run.

Happ may impose its own geo-file refresh interval. Re-importing/updating the
same named routing profile gives Happ the current `LastUpdated` value and URLs.
