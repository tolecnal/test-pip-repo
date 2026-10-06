"""The check suite.

Every check is a small function registered with @check(...). It receives a Context,
returns a one-line detail string on success, and signals other outcomes by raising
Fail (the repository misbehaved) or Skip (the check does not apply here).

Phases run in the order listed in PHASES; checks run in registration order.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import builder, envs, simple, state
from .config import PKG_DIR, Config
from .util import Proc, Response, http, sha256_bytes, strip_ansi

PHASES = ("server", "publish", "install", "mirror", "security", "web")

CheckFunc = Callable[["Context"], str]

# A project page may be served directly or redirected to its normalised spelling.
_FOUND_OR_REDIRECT = (
    HTTPStatus.OK,
    HTTPStatus.MOVED_PERMANENTLY,
    HTTPStatus.FOUND,
    HTTPStatus.SEE_OTHER,
    HTTPStatus.TEMPORARY_REDIRECT,
    HTTPStatus.PERMANENT_REDIRECT,
)
# install_older_pin needs a previous release to reach for.
_VERSIONS_FOR_HISTORY = 2


class Fail(Exception):
    """The repository did not behave as required."""


class Skip(Exception):
    """This check does not apply to the current configuration or state."""


@dataclass
class CheckDef:
    """One registered check: what it is called, when it can run, and what it proves."""

    name: str
    phase: str
    func: CheckFunc
    description: str
    needs_build: bool = False
    needs_upload: bool = False
    needs_auth: bool = False
    fatal: bool = False  # if this fails, the rest of the suite cannot mean anything


REGISTRY: list[CheckDef] = []


def check(name: str, phase: str, description: str, *, needs_build: bool = False,
          needs_upload: bool = False, needs_auth: bool = False,
          fatal: bool = False) -> Callable[[CheckFunc], CheckFunc]:
    def wrap(func: CheckFunc) -> CheckFunc:
        REGISTRY.append(
            CheckDef(name, phase, func, description, needs_build, needs_upload, needs_auth, fatal)
        )
        return func

    return wrap


@dataclass
class Context:
    """Everything a check needs, plus the scratch space checks share with each other."""

    cfg: Config
    st: state.State
    record: state.Build | None = None  # the build under test
    share: dict[str, Any] = field(default_factory=dict)  # data passed between checks
    venvs: dict[str, envs.Venv] = field(default_factory=dict)
    tmp: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="pipcheck-")))

    # ------------------------------------------------------------------ helpers
    @property
    def build(self) -> state.Build:
        """The build under test.

        Checks declaring needs_build/needs_upload are only reached once the runner has
        confirmed there is one, so this never raises for them; it keeps the invariant in
        one place instead of spreading `assert` over every check.

        Returns:
            The build this run is testing.

        Raises:
            Fail: if a check that did not declare needs_build/needs_upload reaches for
                one anyway.
        """
        if self.record is None:
            raise Fail("nothing has been built yet -- run `pipcheck cycle`")
        return self.record

    def venv(self, name: str) -> envs.Venv:
        """Create a venv for an install test, reusing it if a previous check made it.

        Returns:
            The venv. It is torn down with the rest at the end of the run unless
            --keep-venvs was given.
        """
        if name not in self.venvs:
            self.venvs[name] = envs.create(self.cfg, f"test-{name}")
        return self.venvs[name]

    def pip_install(self, venv: envs.Venv, *specs: str, extra: tuple[str, ...] = ()) -> Proc:
        """Install from the internal index only, with every cache bypassed.

        Returns:
            The finished pip process. Caches are off so that a success proves the index
            served the files, not that something was already lying around locally.
        """
        return venv.pip(
            "install", "--no-cache-dir", *self.cfg.pip_index_args(), *extra, *specs, cfg=self.cfg
        )

    def provenance(self, venv: envs.Venv) -> dict[str, Any]:
        """Ask the installed package what it is.

        Returns:
            The build provenance the installed copy reports: its stamped version and
            build id, the version in its metadata, and which dependencies resolved.

        Raises:
            Fail: if the installed copy cannot run or answers with something that is
                not the expected JSON.
        """
        proc = venv.run_python("-m", "devpi_smoke.cli", "--json", cfg=self.cfg)
        if not proc.ok:
            raise Fail(f"installed package could not report its build info:\n{proc.tail(15)}")
        try:
            return json.loads(proc.output[proc.output.index("{") :])
        except (ValueError, json.JSONDecodeError) as exc:
            raise Fail(f"unparseable build info ({exc}):\n{proc.tail(10)}") from exc

    def assert_identity(
        self, venv: envs.Venv, version: str, build_id: str | None = None
    ) -> dict[str, Any]:
        """Verify an installed copy really is the release we asked for.

        Returns:
            The provenance it reported, for a check to quote in its summary.

        Raises:
            Fail: if the stamped version, the metadata version or the build id disagree
                with what was requested, or the declared dependency did not resolve --
                each of which means the index served something other than this release.
        """
        info = self.provenance(venv)
        problems = []
        if info.get("source_version") != version:
            problems.append(f"code says version {info.get('source_version')!r}, wanted {version!r}")
        if info.get("dist_version") != version:
            problems.append(
                f"metadata says version {info.get('dist_version')!r}, wanted {version!r}"
            )
        if build_id and info.get("build_id") != build_id:
            problems.append(
                f"build id {info.get('build_id')!r} != {build_id!r} -- the index served a "
                "different artifact than the one just uploaded (stale cache or overwrite?)"
            )
        if not info.get("six_version"):
            problems.append("dependency six did not resolve")
        if problems:
            raise Fail("; ".join(problems))
        return info

    def cleanup(self) -> None:
        for venv in self.venvs.values():
            envs.discard(self.cfg, venv)
        if not self.cfg.keep_venvs:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # convenience
    def get(self, url: str, *, auth: bool = True, accept: str | None = None) -> Response:
        return http(
            url,
            auth=self.cfg.auth if auth else None,
            accept=accept,
            timeout=self.cfg.http_timeout,
            verify_tls=self.cfg.tls,
        )


def _listed(ctx: Context) -> dict[str, list[simple.Link]]:
    """Read the versions the index currently lists for our package, fetched once a run.

    Returns:
        Each listed version mapped to its files, so checks can reason about history
        without fetching the simple page again.

    Raises:
        Fail: if the simple page answers with anything but 200 or 404.
    """
    if "versions" not in ctx.share:
        resp, links = simple.fetch(ctx.cfg, ctx.cfg.package)
        if resp.status not in {HTTPStatus.OK, HTTPStatus.NOT_FOUND}:
            raise Fail(f"simple page for {ctx.cfg.package} returned HTTP {resp.status}")
        ctx.share["links"] = links
        ctx.share["versions"] = simple.versions(links)
    return ctx.share["versions"]


def _volatile(ctx: Context) -> bool:
    """Read whether the target index allows releases to be replaced, fetched once a run.

    Returns:
        True if the index is volatile, so a re-release of the same version replaces it.

    Raises:
        Fail: if the index's JSON view cannot be read.
    """
    if "volatile" not in ctx.share:
        resp = ctx.get(ctx.cfg.index_url, accept="application/json")
        if resp.status != HTTPStatus.OK:
            raise Fail(f"index JSON view returned HTTP {resp.status}")
        result = resp.json().get("result", {})
        ctx.share["volatile"] = bool(result.get("volatile", True))
        ctx.share["bases"] = result.get("bases") or []
    return ctx.share["volatile"]


# =============================================================== phase: server


@check("server_status", "server", "devpi-server is up and reports its status",
       fatal=True)
def server_status(ctx: Context) -> str:
    resp = ctx.get(f"{ctx.cfg.base}/+status", accept="application/json")
    if resp.status != HTTPStatus.OK:
        raise Fail(f"GET /+status returned HTTP {resp.status}")
    result = resp.json().get("result", {})
    ctx.share["status"] = result
    versions = result.get("versioninfo") or {}
    bits = [f"devpi-server {versions.get('devpi-server', '?')}"]
    if versions.get("devpi-web"):
        bits.append(f"devpi-web {versions['devpi-web']}")
    else:
        bits.append("no devpi-web")
    role = str(result.get("role", "")).lower()
    if role:
        bits.append(f"role {role}")
    if result.get("serial") is not None:
        bits.append(f"serial {result['serial']}")
    if role == "replica":
        # On a replica, falling behind the primary is the failure mode that matters.
        if result.get("replica-in-sync-at") is None:
            raise Fail(
                "this replica has never reported being in sync with its primary "
                f"(master-url={result.get('master-url')})"
            )
        errors = result.get("replication-errors") or {}
        if errors:
            raise Fail(f"replica reports {len(errors)} replication error(s): {list(errors)[:3]}")
        bits.append("in sync")
    return ", ".join(bits)


@check("index_api", "server", "the target index advertises its upload and simple URLs")
def index_api(ctx: Context) -> str:
    resp = ctx.get(f"{ctx.cfg.index_url}/+api", accept="application/json")
    if resp.status == HTTPStatus.NOT_FOUND:
        raise Fail(
            f"index {ctx.cfg.index!r} does not exist on {ctx.cfg.base} "
            "(create it with: devpi index -c <index> bases=root/pypi)"
        )
    if resp.status != HTTPStatus.OK:
        raise Fail(f"GET {ctx.cfg.index_url}/+api returned HTTP {resp.status}")
    result = resp.json().get("result", {})
    # Let the rest of the suite use the server's own URLs rather than guessed ones.
    ignored = ctx.cfg.adopt_api(result)
    detail = f"simple={ctx.cfg.simple_url} upload={ctx.cfg.upload_url}"
    if ignored:
        detail += f"\nignored advertised URLs on another host: {', '.join(ignored)}"
    return detail


@check("index_config", "server", "the index is configured with a PyPI base to fall through to")
def index_config(ctx: Context) -> str:
    resp = ctx.get(ctx.cfg.index_url, accept="application/json")
    if resp.status != HTTPStatus.OK:
        raise Fail(f"index JSON view returned HTTP {resp.status}")
    result = resp.json().get("result", {})
    bases = result.get("bases") or []
    volatile = bool(result.get("volatile", True))
    ctx.share["volatile"] = volatile
    ctx.share["bases"] = bases
    if not bases:
        raise Fail(
            "index has no bases; it cannot serve public packages. "
            "Expected something like bases=root/pypi"
        )
    return f"type={result.get('type')} volatile={volatile} bases={','.join(bases)}"


@check("web_ui", "server", "devpi-web serves the browsable index page")
def web_ui(ctx: Context) -> str:
    root = ctx.get(f"{ctx.cfg.base}/", accept="text/html")
    page = ctx.get(ctx.cfg.index_url, accept="text/html")
    if page.status != HTTPStatus.OK:
        raise Fail(f"index page returned HTTP {page.status}")
    looks_like_web = "devpi" in page.text.lower() and "<html" in page.text.lower()
    if not looks_like_web:
        raise Fail(
            "index URL did not return an HTML page -- devpi-web may not be installed/enabled"
        )
    return f"root HTTP {root.status}, index page {len(page.body)} bytes of HTML"


# ============================================================== phase: publish


@check("release_listed", "publish", "the uploaded release appears on the simple index",
       needs_upload=True)
def release_listed(ctx: Context) -> str:
    version = ctx.build.version
    resp, links = simple.fetch(ctx.cfg, ctx.cfg.package)
    if resp.status != HTTPStatus.OK:
        raise Fail(f"simple page for {ctx.cfg.package} returned HTTP {resp.status}")
    ctx.share["links"] = links
    ctx.share["versions"] = simple.versions(links)
    mine = [link for link in links if link.version == version]
    if not mine:
        raise Fail(
            f"version {version} is absent from {simple.project_url(ctx.cfg, ctx.cfg.package)} "
            f"(index lists: {', '.join(sorted(ctx.share['versions'])) or 'nothing'})"
        )
    if not any(link.is_wheel for link in mine):
        raise Fail(f"no wheel listed for {version}, only {[link.filename for link in mine]}")
    if not any(link.is_sdist for link in mine):
        raise Fail(f"no sdist listed for {version}, only {[link.filename for link in mine]}")
    return (
        f"{len(mine)} file(s) for {version}; index holds "
        f"{len(ctx.share['versions'])} version(s) total"
    )


@check("name_normalisation", "publish", "the index honours PEP 503 name normalisation",
       needs_upload=True)
def name_normalisation(ctx: Context) -> str:
    """Ask for the project under every spelling PEP 503 says must work.

    Returns:
        The status each spelling answered with.

    Raises:
        Fail: if any spelling is neither served nor redirected, because pip would then
            fail to find the package for some of the ways people write its name.
    """
    base = ctx.cfg.simple_url.rstrip("/")
    variants = {"devpi_smoke", "DevPI.Smoke", simple.normalize(ctx.cfg.package)}
    statuses = {}
    for variant in sorted(variants):
        resp = ctx.get(f"{base}/{variant}/")
        statuses[variant] = resp.status
        if resp.status not in _FOUND_OR_REDIRECT:
            raise Fail(
                f"/+simple/{variant}/ returned HTTP {resp.status}; pip would fail to find it"
            )
    return ", ".join(f"{k}={v}" for k, v in statuses.items())


@check("artifact_integrity", "publish", "downloaded artifacts match the bytes that were uploaded",
       needs_upload=True)
def artifact_integrity(ctx: Context) -> str:
    version = ctx.build.version
    _listed(ctx)
    links = [link for link in ctx.share["links"] if link.version == version]
    if not links:
        raise Fail(f"the index lists no files at all for {version}")
    verified = []
    for link in links:
        resp = ctx.get(link.url)
        if resp.status != HTTPStatus.OK:
            raise Fail(f"downloading {link.filename} returned HTTP {resp.status}")
        got = sha256_bytes(resp.body)
        local = ctx.build.sha256.get(link.filename)
        if link.sha256 and got != link.sha256:
            raise Fail(
                f"{link.filename}: served bytes hash {got[:12]} but the index advertises "
                f"{link.sha256[:12]} -- pip will reject this file"
            )
        if local and got != local:
            raise Fail(
                f"{link.filename}: served bytes differ from the locally built artifact "
                f"({got[:12]} vs {local[:12]})"
            )
        verified.append(f"{link.filename} {got[:10]}")
    return "; ".join(verified)


@check("release_metadata", "publish", "the index serves correct metadata for the release",
       needs_upload=True)
def release_metadata(ctx: Context) -> str:
    version = ctx.build.version
    resp = simple.project_json(ctx.cfg, ctx.cfg.package)
    if resp.status != HTTPStatus.OK:
        raise Fail(f"JSON project view returned HTTP {resp.status}")
    result = resp.json().get("result", {})
    if version not in result:
        raise Fail(f"{version} missing from JSON view (has: {', '.join(sorted(result))})")
    meta = result[version]
    requires = meta.get("requires_dist") or []
    if not any(str(r).startswith("six") for r in requires):
        raise Fail(f"requires_dist lost the six dependency: {requires!r}")
    links = meta.get("+links") or []
    return f"{version}: {len(links)} link(s), requires_dist={requires}"


# ============================================================== phase: install


@check("install_wheel", "install", "a pinned wheel installs from the index and is the right build",
       needs_upload=True)
def install_wheel(ctx: Context) -> str:
    version = ctx.build.version
    venv = ctx.venv("wheel")
    proc = ctx.pip_install(venv, f"{ctx.cfg.package}=={version}", extra=("--only-binary", ":all:"))
    if not proc.ok:
        raise Fail(f"pip install {ctx.cfg.package}=={version} failed:\n{proc.tail(20)}")
    info = ctx.assert_identity(venv, version, ctx.build.build_id)
    script = venv.bin("devpi-smoke")
    if not script.exists():
        raise Fail("console script devpi-smoke was not installed (entry points lost?)")
    return f"wheel {version} build {info['build_id']}, six {info['six_version']}, script ok"


@check("install_sdist", "install", "the sdist installs and builds from source via the index",
       needs_upload=True)
def install_sdist(ctx: Context) -> str:
    version = ctx.build.version
    venv = ctx.venv("sdist")
    # --no-binary forces pip to fetch the sdist and resolve the build backend
    # (setuptools, wheel) through the index too.
    proc = ctx.pip_install(venv, f"{ctx.cfg.package}=={version}", extra=("--no-binary", ":all:"))
    if not proc.ok:
        raise Fail(
            f"sdist install failed (this also exercises build-backend resolution "
            f"through the index):\n{proc.tail(25)}"
        )
    info = ctx.assert_identity(venv, version, ctx.build.build_id)
    return f"sdist {version} built and installed, build {info['build_id']}"


@check("install_latest", "install",
       "an unpinned install resolves to the newest version on the index",
       needs_upload=True)
def install_latest(ctx: Context) -> str:
    expected = simple.latest(_listed(ctx))
    if not expected:
        raise Skip("the index lists no versions")
    venv = ctx.venv("latest")
    proc = ctx.pip_install(venv, ctx.cfg.package)
    if not proc.ok:
        raise Fail(f"unpinned install failed:\n{proc.tail(20)}")
    info = ctx.provenance(venv)
    got = info.get("dist_version")
    if got != expected:
        raise Fail(
            f"pip resolved {ctx.cfg.package} to {got}, but the newest version the index "
            f"lists is {expected} -- version ordering or index listing is wrong"
        )
    note = "" if expected == ctx.build.version else f" (not our build {ctx.build.version})"
    return f"resolved to {got}{note}"


@check("install_older_pin", "install", "older releases stay installable from history",
       needs_upload=True)
def install_older_pin(ctx: Context) -> str:
    listed = sorted(_listed(ctx), key=simple.version_key)
    if len(listed) < _VERSIONS_FOR_HISTORY:
        raise Skip("only one version on the index; bump and re-run to exercise history")
    previous = listed[-2]
    venv = ctx.venv("older")
    proc = ctx.pip_install(venv, f"{ctx.cfg.package}=={previous}")
    if not proc.ok:
        raise Fail(f"could not install older pin {previous}:\n{proc.tail(20)}")
    info = ctx.provenance(venv)
    if info.get("dist_version") != previous:
        raise Fail(f"asked for {previous}, got {info.get('dist_version')}")
    return f"older release {previous} still installable"


@check("install_extras", "install", "extras resolve through the index", needs_upload=True)
def install_extras(ctx: Context) -> str:
    version = ctx.build.version
    venv = ctx.venv("extras")
    proc = ctx.pip_install(venv, f"{ctx.cfg.package}[extra]=={version}")
    if not proc.ok:
        raise Fail(f"installing {ctx.cfg.package}[extra]=={version} failed:\n{proc.tail(20)}")
    info = ctx.assert_identity(venv, version, ctx.build.build_id)
    if not info.get("extra_idna"):
        raise Fail("extra 'extra' did not pull its dependency (idna missing)")
    return f"extra pulled idna {info['extra_idna']}"


@check("pip_download", "install", "pip can download the release and its dependencies",
       needs_upload=True)
def pip_download(ctx: Context) -> str:
    version = ctx.build.version
    dest = ctx.tmp / "download"
    dest.mkdir(parents=True, exist_ok=True)
    venv = ctx.venvs.get("wheel") or ctx.venv("wheel")
    proc = venv.pip(
        "download", "--no-cache-dir", *ctx.cfg.pip_index_args(),
        "--dest", str(dest), f"{ctx.cfg.package}=={version}", cfg=ctx.cfg,
    )
    if not proc.ok:
        raise Fail(f"pip download failed:\n{proc.tail(20)}")
    got = sorted(p.name for p in dest.iterdir())
    wanted = (ctx.cfg.package, ctx.cfg.package.replace("-", "_"))
    if not any(spelling in name for name in got for spelling in wanted):
        raise Fail(f"download produced no artifact for {ctx.cfg.package}: {got}")
    return f"downloaded {len(got)} file(s): {', '.join(got[:4])}"


@check("package_tests", "install", "the package's own test suite passes against the served copy",
       needs_upload=True)
def package_tests(ctx: Context) -> str:
    if not ctx.cfg.run_pytest:
        raise Skip("run_pytest is disabled in config")
    venv = ctx.venvs.get("wheel") or ctx.venv("wheel")
    install = ctx.pip_install(venv, "pytest")
    if not install.ok:
        raise Fail(
            "could not install pytest from the index -- the PyPI mirror cannot serve a "
            f"package with real dependencies:\n{install.tail(20)}"
        )
    proc = venv.run_python(
        "-m", "pytest", str(PKG_DIR / "tests"), "-q", "-p", "no:cacheprovider", cfg=ctx.cfg
    )
    if not proc.ok:
        raise Fail(f"pytest failed against the installed package:\n{proc.tail(25)}")
    summary = [
        out
        for out in proc.output.strip().splitlines()
        if "passed" in out or "failed" in out
    ]
    return summary[-1].strip() if summary else "pytest passed"


# =============================================================== phase: mirror


@check("mirror_public_package", "mirror", "public packages are proxied from the PyPI mirror")
def mirror_public_package(ctx: Context) -> str:
    probe = ctx.cfg.mirror_probe
    venv = ctx.venv("mirror")
    started = time.monotonic()
    proc = ctx.pip_install(venv, probe)
    first = time.monotonic() - started
    if not proc.ok:
        raise Fail(
            f"installing public package {probe!r} through the index failed -- "
            f"root/pypi mirroring or outbound network may be broken:\n{proc.tail(20)}"
        )
    show = venv.pip("show", probe, cfg=ctx.cfg)
    version = next(
        (row.split(":", 1)[1].strip()
         for row in show.output.splitlines() if row.startswith("Version:")),
        "?",
    )
    ctx.share["mirror_first"] = first
    return f"{probe} {version} proxied in {first:.1f}s"


@check("mirror_cache", "mirror", "the mirror serves a second fetch from its own cache")
def mirror_cache(ctx: Context) -> str:
    probe = ctx.cfg.mirror_probe
    if "mirror_first" not in ctx.share:
        raise Skip("first mirror fetch did not run")
    venv = ctx.venv("mirror2")
    started = time.monotonic()
    proc = ctx.pip_install(venv, probe)
    second = time.monotonic() - started
    if not proc.ok:
        raise Fail(f"second fetch of {probe!r} failed:\n{proc.tail(20)}")
    first = ctx.share["mirror_first"]
    return f"refetched in {second:.1f}s (first {first:.1f}s) -- cached copy served"


# ============================================================= phase: security


@check("anonymous_read", "security", "the index is readable the way pip clients expect")
def anonymous_read(ctx: Context) -> str:
    resp, _ = simple.fetch(ctx.cfg, ctx.cfg.package, auth=False)
    # 404 means the index is readable, the project just is not on it yet.
    allowed = resp.status in {HTTPStatus.OK, HTTPStatus.NOT_FOUND}
    if ctx.cfg.expect_anonymous_read and not allowed:
        raise Fail(
            f"anonymous read of the simple index returned HTTP {resp.status}; "
            "unauthenticated pip clients will fail"
        )
    if not ctx.cfg.expect_anonymous_read and allowed:
        raise Fail(
            f"the simple index is readable without credentials (HTTP {resp.status}) but "
            "expect_anonymous_read is false"
        )
    verdict = "readable" if allowed else "closed"
    return f"anonymous simple index is {verdict} (HTTP {resp.status}), as configured"


@check("overwrite_protection", "security",
       "re-releasing a version with different content behaves as the index promises",
       needs_upload=True, needs_auth=True)
def overwrite_protection(ctx: Context) -> str:
    """Try to replace a published release with different bytes under the same name.

    devpi accepts a byte-identical re-upload as a no-op, so that proves nothing. This
    rebuilds the same version with a fresh build id -- same filenames, different
    content -- which is what an accidental re-release looks like. A non-volatile index
    must refuse it; a volatile one must apply it completely, not partially.

    Returns:
        What the index did, and on what basis that was expected.

    Raises:
        Fail: if a published release was replaced where immutability was expected, if a
            refused upload changed the release anyway, or if an accepted replacement was
            only partially applied -- a stale cache still serving the old bytes.
    """
    expect = ctx.cfg.expect_overwrite_rejected
    if isinstance(expect, str):
        if expect.strip().lower() == "auto":
            volatile = _volatile(ctx)
            expect_reject, basis = not volatile, f"auto (volatile={volatile})"
        else:
            expect_reject, basis = expect.strip().lower() in {"1", "true", "yes"}, "configured"
    else:
        expect_reject, basis = expect, "configured"

    original = ctx.build
    # Build the variant of the version under test -- not of whatever pyproject says now.
    with builder.preserved_source():
        variant = builder.build_variant(ctx.cfg, ctx.tmp / "variant", original.version)
    proc = builder.upload_as(ctx.cfg, variant.files, ctx.cfg.user, ctx.cfg.password)
    rejected = not proc.ok
    served = _served_hashes(ctx, original.version)

    if rejected:
        changed = {
            name: digest
            for name, digest in served.items()
            if name in original.sha256 and digest and digest != original.sha256[name]
        }
        if changed:
            raise Fail(
                f"the upload was refused but the index no longer serves the original "
                f"bytes for {', '.join(changed)} -- a rejected upload must not alter a release"
            )
        if not expect_reject:
            return (
                f"re-release refused though {basis} expected it to be allowed: "
                f"{_first_error(proc)}"
            )
        return f"release protected: {_first_error(proc)}"

    # Accepted: the recorded build is no longer what the index holds, so adopt it.
    builder.adopt_variant(ctx.st, variant)
    ctx.record = variant
    stale = {
        name: digest
        for name, digest in served.items()
        if name in variant.sha256 and digest and digest != variant.sha256[name]
    }
    if expect_reject:
        raise Fail(
            f"the index let a published release be overwritten with different content "
            f"({basis} expected a rejection) -- releases on this index are not immutable"
        )
    if stale:
        raise Fail(
            f"the overwrite was accepted but the index still serves the old bytes for "
            f"{', '.join(stale)} -- partially applied release, likely a stale cache"
        )
    return f"volatile index replaced {original.version} ({basis}); new build {variant.build_id}"


def _served_hashes(ctx: Context, version: str) -> dict[str, str | None]:
    _, links = simple.fetch(ctx.cfg, ctx.cfg.package)
    return {link.filename: link.sha256 for link in links if link.version == version}


def _first_error(proc: Proc) -> str:
    """Return the most informative line of a failed twine run.

    twine --verbose ends with `ERROR HTTPError: <status> from <url>`; prefer that over
    the response body it also echoes.

    Returns:
        One line, stripped of colour and collapsed to fit a report.
    """
    lines = [
        re.sub(r"^(ERROR|WARNING|INFO)\s+", "", raw.strip())
        for raw in strip_ansi(proc.output).splitlines()
        if raw.strip()
    ]
    for pattern in (r"HTTPError", r"\b(4\d\d|5\d\d)\b", r"error", r"."):
        for line in lines:
            if re.search(pattern, line, re.IGNORECASE):
                return re.sub(r"\s+", " ", line)[:200]
    return "no output"


@check("upload_requires_auth", "security", "uploads are refused without valid credentials")
def upload_requires_auth(ctx: Context) -> str:
    """Upload a brand-new, throwaway project with bogus credentials.

    A fresh project name means a rejection cannot be mistaken for "that version
    already exists", and an unexpected success cannot clobber a real release.

    Returns:
        How the upload was refused.

    Raises:
        Fail: if the index accepted it, which means anonymous upload is open; the
            message names the stray project to remove.
        Skip: if the configuration says not to expect authenticated uploads.
    """
    if not ctx.cfg.expect_upload_auth:
        raise Skip("expect_upload_auth is disabled in config")
    name, archive = builder.make_probe_sdist(ctx.tmp / "probe")
    proc = builder.upload_as(
        ctx.cfg, [str(archive)], "pipcheck-nobody", "definitely-not-the-password"
    )
    if proc.ok:
        raise Fail(
            "the index accepted an upload from bogus credentials -- anonymous upload is "
            f"open. Remove the stray project {name!r} from {ctx.cfg.index}"
        )
    status = re.search(r"\b(401|403)\b", strip_ansi(proc.output))
    if not status:
        # Still a rejection, but not obviously an auth one; surface what happened.
        return f"upload refused, though not with 401/403: {_first_error(proc)}"
    return f"refused with HTTP {status.group(1)}"


# ================================================================== phase: web


@check("web_search", "web", "devpi-web search finds the uploaded release", needs_upload=True)
def web_search(ctx: Context) -> str:
    """Search for the release through devpi-web.

    Returns:
        How many attempts it took to appear; devpi-web indexes asynchronously, so this
        polls for up to search_wait seconds.

    Raises:
        Fail: if the release never appears, which means the indexer is stale or stopped.
        Skip: if the server has no /+search endpoint at all.
    """
    query = urllib.parse.urlencode({"query": ctx.cfg.package})
    url = f"{ctx.cfg.base}/+search?{query}"
    deadline = time.monotonic() + max(0, ctx.cfg.search_wait)
    attempts = 0
    last = None
    while True:
        attempts += 1
        resp = ctx.get(url, accept="text/html")
        last = resp.status
        listed = simple.normalize(ctx.cfg.package) in simple.normalize(resp.text)
        if resp.status == HTTPStatus.OK and listed:
            return f"found after {attempts} attempt(s)"
        if resp.status == HTTPStatus.NOT_FOUND:
            raise Skip("no /+search endpoint; devpi-web search may be disabled")
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise Fail(
        f"search did not list {ctx.cfg.package} within {ctx.cfg.search_wait}s "
        f"(last HTTP {last}); devpi-web's index may be stale or its indexer stopped"
    )
