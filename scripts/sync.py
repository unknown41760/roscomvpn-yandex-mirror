#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(os.environ["GITHUB_WORKSPACE"])
ROUTING_SRC = Path(os.environ["ROUTING_SRC"])
GEOSITE_SRC = Path(os.environ["GEOSITE_SRC"])
V2FLY_SRC = Path(os.environ["V2FLY_SRC"])
GEOIP_SRC = Path(os.environ["GEOIP_SRC"])
V2FLY_GEOIP_SRC = Path(os.environ["V2FLY_GEOIP_SRC"])
WORK_DIR = Path(os.environ["WORK_DIR"])

HAPP_OUT = ROOT / "HAPP"
BUILD_OUT = ROOT / ".build"
SITE_OUT = ROOT / ".site"
STATE_FILE = ROOT / ".state.json"

# Fixes subscriptions that inject `geosite:yandex`.
EXTRA_CATEGORIES = ["yandex"]
# Fixes subscriptions that inject `geoip:ru` while retaining every category
# and CIDR from RoscomVPN's custom GeoIP database.
EXTRA_GEOIP_CATEGORIES = ["ru"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def public_base_url(repo: str) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/") + "/"

    owner, name = repo.split("/", 1)

    # GitHub user/org Pages repository is served at the domain root.
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"

    return f"https://{owner}.github.io/{name}/"


def parse_includes(path: Path) -> list[str]:
    includes: list[str] = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("include:"):
            continue

        value = line[len("include:"):].strip()
        value = value.split("@", 1)[0].strip()
        value = re.split(r"\s+", value, maxsplit=1)[0].strip()

        if value:
            includes.append(value)

    return includes


def add_v2fly_category_recursive(
    category: str,
    src_data: Path,
    dst_data: Path,
    seen: set[str],
    *,
    force_root: bool = False,
) -> None:
    if category in seen:
        return
    seen.add(category)

    src = src_data / category
    if not src.is_file():
        raise RuntimeError(f"v2fly category does not exist: {category}")

    dst = dst_data / category

    # Always install the requested root category. For dependencies, preserve a
    # RoscomVPN-customized file if one with the same name already exists.
    if force_root or not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Parse dependencies from V2Fly's source version.
    for child in parse_includes(src):
        add_v2fly_category_recursive(
            child,
            src_data,
            dst_data,
            seen,
            force_root=False,
        )


def build_augmented_geosite() -> tuple[Path, set[str]]:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)

    builder = WORK_DIR / "builder"
    outdir = WORK_DIR / "out"

    # Use the official domain-list-community compiler but replace its data
    # directory with RoscomVPN's optimized data.
    shutil.copytree(
        V2FLY_SRC,
        builder,
        ignore=shutil.ignore_patterns(".git"),
    )

    builder_data = builder / "data"
    shutil.rmtree(builder_data)
    shutil.copytree(GEOSITE_SRC / "data", builder_data)

    copied: set[str] = set()
    for category in EXTRA_CATEGORIES:
        add_v2fly_category_recursive(
            category,
            V2FLY_SRC / "data",
            builder_data,
            copied,
            force_root=True,
        )

    outdir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["go", "run", "./", f"-outputdir={outdir}"],
        cwd=builder,
        check=True,
    )

    built = outdir / "dlc.dat"
    if not built.is_file() or built.stat().st_size == 0:
        raise RuntimeError("V2Fly builder did not produce dlc.dat")

    BUILD_OUT.mkdir(parents=True, exist_ok=True)
    final = BUILD_OUT / "geosite.dat"
    shutil.copy2(built, final)

    return final, copied


def run_geodata_tool(*args: str, capture_output: bool = False) -> str:
    command = [
        "go",
        "run",
        str(ROOT / "scripts" / "geodata_tool.go"),
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=V2FLY_SRC,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if result.returncode != 0:
        diagnostic = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        raise RuntimeError(
            f"geodata validation tool failed ({' '.join(args)}):\n{diagnostic}"
        )
    return result.stdout if capture_output else ""


def build_augmented_geoip() -> Path:
    base = GEOIP_SRC / "geoip.dat"
    source = V2FLY_GEOIP_SRC / "geoip.dat"
    for label, path in (("RoscomVPN", base), ("V2Fly", source)):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"{label} GeoIP release is missing {path}")

    BUILD_OUT.mkdir(parents=True, exist_ok=True)
    final = BUILD_OUT / "geoip.dat"

    # Currently there is one compatibility category. Invoking the merge tool
    # per category keeps this incremental if more are needed in the future.
    current_base = base
    for index, category in enumerate(EXTRA_GEOIP_CATEGORIES):
        output = (
            final
            if index == len(EXTRA_GEOIP_CATEGORIES) - 1
            else BUILD_OUT / f"geoip.merge-{index}.dat"
        )
        run_geodata_tool(
            "merge-geoip",
            "--base", str(current_base),
            "--source", str(source),
            "--output", str(output),
            "--category", category,
        )
        current_base = output

    if not final.is_file() or final.stat().st_size == 0:
        raise RuntimeError("GeoIP merge did not produce geoip.dat")
    return final


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_without_timestamp(obj: dict) -> str:
    clone = dict(obj)
    clone.pop("LastUpdated", None)
    return json.dumps(
        clone,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def geosite_cdn_url(repo: str) -> str:
    # Stable URL. The release branch contains only the latest generated binary.
    return f"https://cdn.jsdelivr.net/gh/{repo}@release/geosite.dat"


def geoip_cdn_url(repo: str) -> str:
    return f"https://cdn.jsdelivr.net/gh/{repo}@release/geoip.dat"


def patched_profile(src: Path, repo: str) -> dict:
    obj = load_json(src)

    # Keep the established suffix so existing compatibility profiles update in
    # place instead of creating a second profile when GeoIP support is added.
    name = str(obj.get("Name", src.stem))
    suffix = " + Yandex"
    if not name.endswith(suffix):
        obj["Name"] = name + suffix

    # Happ on iOS extracts only categories explicitly named in the routing
    # profile when chunk mode is enabled. Subscription-injected rules such as
    # geosite:yandex and geoip:ru are not visible to that extractor, so their
    # sections are omitted even though the downloaded full files contain them.
    # Our optimized geodata is small, making full-file mode safe on iOS.
    obj["UseChunkFiles"] = "false"
    obj["Geositeurl"] = geosite_cdn_url(repo)
    obj["Geoipurl"] = geoip_cdn_url(repo)
    return obj


def walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)


def referenced_categories(profiles: dict[str, dict]) -> tuple[set[str], set[str]]:
    geosite = {category.upper() for category in EXTRA_CATEGORIES}
    geoip = {category.upper() for category in EXTRA_GEOIP_CATEGORIES}

    pattern = re.compile(r"^(geosite|geoip):(!?[^@\s]+)", re.IGNORECASE)
    for profile in profiles.values():
        for value in walk_strings(profile):
            match = pattern.match(value.strip())
            if not match:
                continue
            kind, category = match.groups()
            category = category.lstrip("!").upper()
            if kind.lower() == "geosite":
                geosite.add(category)
            else:
                geoip.add(category)

    return geosite, geoip


def validate_geodata(
    geosite_file: Path,
    geoip_file: Path,
    required_geosite: set[str],
    required_geoip: set[str],
) -> dict:
    args = [
        "validate",
        "--geosite", str(geosite_file),
        "--geoip", str(geoip_file),
    ]
    for category in sorted(required_geosite):
        args.extend(("--geosite-category", category))
    for category in sorted(required_geoip):
        args.extend(("--geoip-category", category))

    output = run_geodata_tool(*args, capture_output=True)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"geodata validation returned invalid JSON: {output!r}"
        ) from exc


def make_deeplink(obj: dict) -> str:
    compact = json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    encoded = base64.b64encode(compact).decode("ascii")
    return "happ://routing/onadd/" + encoded


def write_profile_and_deeplink(filename: str, obj: dict) -> None:
    json_path = HAPP_OUT / filename
    json_path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    deeplink_path = HAPP_OUT / f"{Path(filename).stem}.DEEPLINK"
    deeplink_path.write_text(make_deeplink(obj) + "\n", encoding="utf-8")


def emit_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def profile_slug(filename: str) -> str:
    return Path(filename).stem.lower()


def install_html(profile_name: str, deeplink: str, base_url: str) -> str:
    safe_name = html.escape(profile_name)
    safe_link = html.escape(deeplink, quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex">
  <title>Install {safe_name} in Happ</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      max-width: 720px;
      margin: 0 auto;
      padding: 32px 18px;
      line-height: 1.5;
    }}
    .button {{
      display: inline-block;
      padding: 14px 18px;
      border-radius: 10px;
      background: #111;
      color: white;
      text-decoration: none;
      font-weight: 700;
    }}
    code {{
      overflow-wrap: anywhere;
    }}
    .muted {{
      opacity: .72;
    }}
  </style>
</head>
<body>
  <h1>{safe_name}</h1>
  <p>This permanent page always points to the latest generated Happ routing profile.</p>

  <p>
    <a class="button" id="install" href="{safe_link}">
      Open in Happ
    </a>
  </p>

  <p class="muted">
    If the app does not open automatically, tap <strong>Open in Happ</strong>.
  </p>

  <p>
    <a href="../../">Back to all profiles</a>
  </p>
</body>
</html>
"""


def index_html(profiles: list[dict], manifest_url: str) -> str:
    cards = []
    for p in profiles:
        cards.append(
            f"""
            <li>
              <strong>{html.escape(p["name"])}</strong><br>
              <a href="{html.escape(p["install_url"], quote=True)}">Install in Happ</a>
              · <a href="{html.escape(p["json_url"], quote=True)}">JSON</a>
              · <a href="{html.escape(p["deeplink_text_url"], quote=True)}">Deeplink text</a>
            </li>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RoscomVPN compatible Happ routing</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      max-width: 800px;
      margin: 0 auto;
      padding: 32px 18px;
      line-height: 1.5;
    }}
    li {{ margin: 18px 0; }}
    code {{ overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>RoscomVPN geodata compatibility</h1>
  <p>
    Latest RoscomVPN Happ routing profiles with compatible geodata containing
    <code>geosite:yandex</code> and <code>geoip:ru</code>.
  </p>

  <ul>
    {''.join(cards)}
  </ul>

  <p>
    Machine-readable update manifest:
    <a href="{html.escape(manifest_url, quote=True)}">latest.json</a>
  </p>
</body>
</html>
"""


def build_site(
    repo: str,
    base_url: str,
    geosite_file: Path,
    geosite_sha: str,
    geoip_file: Path,
    geoip_sha: str,
    state: dict,
) -> None:
    if SITE_OUT.exists():
        shutil.rmtree(SITE_OUT)

    SITE_OUT.mkdir(parents=True)

    # Also publish the binary directly on Pages as a stable fallback.
    shutil.copy2(geosite_file, SITE_OUT / "geosite.dat")
    (SITE_OUT / "geosite.dat.sha256").write_text(
        f"{geosite_sha}  geosite.dat\n",
        encoding="utf-8",
    )
    shutil.copy2(geoip_file, SITE_OUT / "geoip.dat")
    (SITE_OUT / "geoip.dat.sha256").write_text(
        f"{geoip_sha}  geoip.dat\n",
        encoding="utf-8",
    )

    profiles_meta: list[dict] = []

    for json_path in sorted(HAPP_OUT.glob("*.JSON")):
        obj = load_json(json_path)
        slug = profile_slug(json_path.name)
        deeplink = make_deeplink(obj)

        routing_dir = SITE_OUT / "routing"
        routing_dir.mkdir(exist_ok=True)

        # Current machine-readable profile and current deeplink.
        shutil.copy2(json_path, routing_dir / f"{slug}.json")
        (routing_dir / f"{slug}.txt").write_text(
            deeplink + "\n",
            encoding="utf-8",
        )

        # Ready-to-copy forms for panel integration.
        panel_dir = SITE_OUT / "panel"
        panel_dir.mkdir(exist_ok=True)
        (panel_dir / f"{slug}-routing-header.txt").write_text(
            f"routing: {deeplink}\n",
            encoding="utf-8",
        )
        (panel_dir / f"{slug}-subscription-body.txt").write_text(
            deeplink + "\n",
            encoding="utf-8",
        )

        install_dir = SITE_OUT / "install" / slug
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "index.html").write_text(
            install_html(
                str(obj.get("Name", slug)),
                deeplink,
                base_url,
            ),
            encoding="utf-8",
        )

        profiles_meta.append({
            "slug": slug,
            "name": str(obj.get("Name", slug)),
            "last_updated": str(obj.get("LastUpdated", "")),
            "install_url": f"{base_url}install/{slug}/",
            "json_url": f"{base_url}routing/{slug}.json",
            "deeplink_text_url": f"{base_url}routing/{slug}.txt",
            "routing_header_url": (
                f"{base_url}panel/{slug}-routing-header.txt"
            ),
            "subscription_body_url": (
                f"{base_url}panel/{slug}-subscription-body.txt"
            ),
        })

    manifest = {
        "generated_at": state.get("generated_at"),
        "repository": repo,
        "public_base_url": base_url,
        "geosite": {
            "sha256": geosite_sha,
            "pages_url": f"{base_url}geosite.dat",
            "cdn_url": geosite_cdn_url(repo),
        },
        "geoip": {
            "sha256": geoip_sha,
            "pages_url": f"{base_url}geoip.dat",
            "cdn_url": geoip_cdn_url(repo),
        },
        "upstream": {
            "routing_commit": state.get("routing_upstream_commit"),
            "geosite_commit": state.get("geosite_upstream_commit"),
            "v2fly_commit": state.get("v2fly_upstream_commit"),
            "geoip_commit": state.get("geoip_upstream_commit"),
            "v2fly_geoip_commit": state.get("v2fly_geoip_upstream_commit"),
        },
        "added_categories": state.get("added_categories", EXTRA_CATEGORIES),
        "added_geoip_categories": state.get(
            "added_geoip_categories",
            EXTRA_GEOIP_CATEGORIES,
        ),
        "validated_categories": state.get("validated_categories", {}),
        "profiles": profiles_meta,
    }

    (SITE_OUT / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    (SITE_OUT / "index.html").write_text(
        index_html(profiles_meta, f"{base_url}latest.json"),
        encoding="utf-8",
    )

    # Keep GitHub Pages/Jekyll from modifying generated files.
    (SITE_OUT / ".nojekyll").write_text("", encoding="utf-8")


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    base_url = public_base_url(repo)

    geosite_file, extra_files = build_augmented_geosite()
    geosite_sha = sha256_file(geosite_file)
    geoip_file = build_augmented_geoip()
    geoip_sha = sha256_file(geoip_file)

    source_jsons = sorted((ROUTING_SRC / "HAPP").glob("*.JSON"))
    if not source_jsons:
        raise RuntimeError("No HAPP/*.JSON profiles found upstream")

    desired: dict[str, dict] = {
        src.name: patched_profile(src, repo)
        for src in source_jsons
    }

    required_geosite, required_geoip = referenced_categories(desired)
    validate_geodata(
        geosite_file,
        geoip_file,
        required_geosite,
        required_geoip,
    )
    print(
        "Validated geosite categories:",
        ", ".join(sorted(required_geosite)),
    )
    print(
        "Validated GeoIP categories:",
        ", ".join(sorted(required_geoip)),
    )

    old_state: dict = {}
    if STATE_FILE.exists():
        try:
            old_state = load_json(STATE_FILE)
        except Exception:
            old_state = {}

    old_geosite_sha = old_state.get("geosite_sha256")
    old_geoip_sha = old_state.get("geoip_sha256")

    existing_json_names = (
        {p.name for p in HAPP_OUT.glob("*.JSON")}
        if HAPP_OUT.exists()
        else set()
    )
    desired_json_names = set(desired)

    configs_changed = existing_json_names != desired_json_names

    if not configs_changed:
        for name, obj in desired.items():
            current_path = HAPP_OUT / name

            if not current_path.exists():
                configs_changed = True
                break

            try:
                current = load_json(current_path)
            except Exception:
                configs_changed = True
                break

            if canonical_without_timestamp(current) != canonical_without_timestamp(obj):
                configs_changed = True
                break

    geosite_changed = geosite_sha != old_geosite_sha
    geoip_changed = geoip_sha != old_geoip_sha
    geodata_changed = geosite_changed or geoip_changed
    changed = configs_changed or geodata_changed

    now = str(int(time.time()))

    if changed:
        if HAPP_OUT.exists():
            shutil.rmtree(HAPP_OUT)
        HAPP_OUT.mkdir(parents=True)

        for name, obj in desired.items():
            obj["LastUpdated"] = now
            write_profile_and_deeplink(name, obj)

        state = {
            "generated_at": now,
            "routing_upstream_commit": git_head(ROUTING_SRC),
            "geosite_upstream_commit": git_head(GEOSITE_SRC),
            "v2fly_upstream_commit": git_head(V2FLY_SRC),
            "geoip_upstream_commit": git_head(GEOIP_SRC),
            "v2fly_geoip_upstream_commit": git_head(V2FLY_GEOIP_SRC),
            "geosite_sha256": geosite_sha,
            "geoip_sha256": geoip_sha,
            "added_categories": EXTRA_CATEGORIES,
            "added_geoip_categories": EXTRA_GEOIP_CATEGORIES,
            "v2fly_files_added": sorted(extra_files),
            "validated_categories": {
                "geosite": sorted(required_geosite),
                "geoip": sorted(required_geoip),
            },
        }

        STATE_FILE.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        print(f"Generated {len(desired)} Happ profiles.")
        print(f"Augmented geosite SHA-256: {geosite_sha}")
        print(f"Augmented GeoIP SHA-256: {geoip_sha}")
        print("Added V2Fly files:", ", ".join(sorted(extra_files)))
    else:
        print("No material routing/geodata changes detected.")
        state = old_state

        # First-time safety if generated files were manually omitted.
        if not HAPP_OUT.exists():
            raise RuntimeError(
                "No HAPP output exists although state says nothing changed"
            )

    # Pages gets rebuilt on every workflow run. Its public URLs remain stable
    # while its embedded deeplinks move to the newest generated profile.
    build_site(
        repo=repo,
        base_url=base_url,
        geosite_file=geosite_file,
        geosite_sha=geosite_sha,
        geoip_file=geoip_file,
        geoip_sha=geoip_sha,
        state=state,
    )

    emit_output("changed", "true" if changed else "false")
    emit_output("geodata_changed", "true" if geodata_changed else "false")
    emit_output("geosite_changed", "true" if geosite_changed else "false")
    emit_output("geoip_changed", "true" if geoip_changed else "false")
    emit_output("geosite_sha256", geosite_sha)
    emit_output("geoip_sha256", geoip_sha)
    emit_output("public_base_url", base_url)

    print(f"Permanent installer base: {base_url}")


if __name__ == "__main__":
    main()
