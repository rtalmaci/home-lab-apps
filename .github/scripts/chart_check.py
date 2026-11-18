#!/usr/bin/env python3
"""Chart Check: bump chart version, validate, report as commit status `chart-check`."""
import json, os, re, subprocess, sys, urllib.request

REPO, PR, BASE = os.environ["REPO"], os.environ["PR"], os.environ["BASE_REF"]
INDEX_URL = "https://rtalmaci.github.io/home-lab-apps/index.yaml"
UPSTREAM = {  # chart -> upstream GitHub repos whose release notes are scanned
    "radarr": ["Radarr/Radarr", "linuxserver/docker-radarr"],
    "sonarr": ["Sonarr/Sonarr", "linuxserver/docker-sonarr"],
    "prowlarr": ["Prowlarr/Prowlarr", "linuxserver/docker-prowlarr"],
    "bazarr": ["morpheus65535/bazarr"],
    "qbittorrent": ["qbittorrent/qBittorrent", "linuxserver/docker-qbittorrent"],
}
KEYWORDS = re.compile(
    r"(env(ironment)? var(iable)?s?|\bPUID\b|\bPGID\b|\bUID\b|\bGID\b|umask|\bports?\b|"
    r"command[- ]line|\bcli (arg|option)|\bvolumes?\b|\bmounts?\b|/config\b|/data\b|"
    r"data (dir|folder|path)|config(uration)? (key|file|option)|config\.xml|"
    r"health ?(check|endpoint)|/ping\b|base ?url|"
    r"breaking|no longer|requires? \.?net|user namespace|read-only|rootless|--[a-z][\w-]+)",
    re.I,
)


def run(*a, check=True):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(a)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def field(text, name):
    m = re.search(rf'^{name}:\s*"?([^"\r\n]+?)"?\s*$', text, re.M)
    return m.group(1) if m else None


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def published_versions(chart):
    try:
        txt = urllib.request.urlopen(INDEX_URL, timeout=30).read().decode()
    except Exception as e:
        print(f"warn: cannot fetch index ({e})")
        return None
    m = re.search(rf"^  {re.escape(chart)}:\n(.*?)(?=^  [A-Za-z]|\Z)", txt, re.M | re.S)
    if not m:
        return set()
    return set(re.findall(r'^    version: "?([^"\n]+)"?$', m.group(1), re.M))


def release_notes_flags(chart, old, new):
    flags = []
    for repo in UPSTREAM.get(chart, []):
        try:
            rels = json.loads(run("gh", "api", f"repos/{repo}/releases?per_page=100"))
        except Exception as e:
            flags.append(f"{repo}: could not fetch release notes ({e}) - needs human review")
            continue
        for r in rels:
            tag = r.get("tag_name", "")
            if not re.search(r"\d+\.\d+\.\d+", tag):
                continue
            if not (vt(old) < vt(tag) <= vt(new)):
                continue
            for line in (r.get("body") or "").splitlines():
                if KEYWORDS.search(line):
                    flags.append(f"{repo} {tag}: {line.strip()[:200]}")
    return flags


def set_status(sha, state, desc):
    run("gh", "api", f"repos/{REPO}/statuses/{sha}", "-f", f"state={state}",
        "-f", "context=chart-check", "-f", f"description={desc[:140]}")


def main():
    run("git", "fetch", "origin", BASE)
    base = f"origin/{BASE}"
    files = run("git", "diff", "--name-only", f"{base}...HEAD", "--", "charts/*/Chart.yaml").split()
    problems, notes, review = [], [], []

    # 1. bump chart version where appVersion changed and version did not
    for f in files:
        old = run("git", "show", f"{base}:{f}", check=False)
        new = open(f).read()
        if field(old, "appVersion") != field(new, "appVersion") and field(old, "version") == field(new, "version"):
            mm, patch = field(new, "version").rsplit(".", 1)
            nv = f"{mm}.{int(patch) + 1}"
            open(f, "w", newline="").write(re.sub(r"^version:.*$", f"version: {nv}", new, count=1, flags=re.M))
            notes.append(f"bumped {f} version -> {nv}")
    if run("git", "status", "--porcelain"):
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        run("git", "commit", "-am", "Bump chart version for appVersion update")
        run("git", "push")
    sha = run("git", "rev-parse", "HEAD")
    set_status(sha, "pending", "chart-check running")  # pending before validation so Renovate waits

    # 2. validate each changed chart
    for f in files:
        chart = f.split("/")[1]
        d = f"charts/{chart}"
        old = run("git", "show", f"{base}:{f}", check=False)
        new = open(f).read()
        old_app, new_app = field(old, "appVersion"), field(new, "appVersion")
        ver = field(new, "version")
        if old_app == new_app:
            notes.append(f"{chart}: appVersion unchanged, skipping app checks")
            continue
        # chart version must be higher than base and unpublished (release.yml uses skip_existing)
        if vt(ver) <= vt(field(old, "version") or "0.0.0"):
            problems.append(f"{chart}: chart version {ver} not higher than base")
        pub = published_versions(chart)
        if pub is not None and ver in pub:
            problems.append(f"{chart}: chart version {ver} already published; chart-releaser would skip it")
        # helm lint + template; image tag must equal appVersion and exist in the registry
        try:
            run("helm", "lint", d)
            rendered = run("helm", "template", chart, d)
            images = {i.split("@")[0] for i in re.findall(r'image:\s*"?([^\s"]+)"?', rendered)}
            app_images = [i for i in images if i.endswith(f":{new_app}")]
            if not app_images:
                problems.append(f"{chart}: rendered images {sorted(images)} do not use appVersion {new_app}")
            for i in app_images:
                if subprocess.run(["docker", "manifest", "inspect", i], capture_output=True).returncode:
                    problems.append(f"{chart}: image {i} not found in registry")
        except RuntimeError as e:
            problems.append(f"{chart}: helm failed: {e}")
        # major bumps: scan upstream release notes for chart-relevant changes
        if vt(new_app)[0] != vt(old_app)[0]:
            flags = release_notes_flags(chart, old_app, new_app)
            if flags:
                review.append((chart, old_app, new_app, flags))

    # 3. report
    if review:
        run("gh", "label", "create", "needs-chart-review", "--repo", REPO, "--color", "d93f0b",
            "--description", "Major app bump: release notes suggest the chart may need changes", check=False)
        run("gh", "pr", "edit", PR, "--repo", REPO, "--add-label", "needs-chart-review", check=False)
        body = "**chart-check: release notes suggest the chart may need changes**\n\n"
        for chart, o, n, flags in review:
            body += f"### {chart} {o} -> {n}\n" + "\n".join(f"- {x}" for x in flags[:40]) + "\n\n"
        body += "Review the chart, then push a commit or re-run the check."
        run("gh", "pr", "comment", PR, "--repo", REPO, "--body", body, check=False)
        problems.append("release notes flagged chart-relevant changes (label needs-chart-review)")
    else:
        run("gh", "pr", "edit", PR, "--repo", REPO, "--remove-label", "needs-chart-review", check=False)

    print("\n".join(notes))
    if problems:
        print("FAIL:\n" + "\n".join(problems))
        set_status(sha, "failure", "; ".join(problems))
        sys.exit(1)
    set_status(sha, "success", "chart bump validated")
    print("OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}")
        try:
            set_status(run("git", "rev-parse", "HEAD"), "failure", f"chart-check error: {e}")
        except Exception:
            pass
        sys.exit(1)
