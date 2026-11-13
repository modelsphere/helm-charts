#!/usr/bin/env python3
"""Unit tests for hack/sync-upstream-charts.py. No network."""

import importlib.util
import io
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve().parent / "sync-upstream-charts.py"

# Register before exec. dataclasses looks the class's module up in sys.modules
# while the class body is still being executed; a bare spec load is not there yet.
_spec = importlib.util.spec_from_file_location("sync_upstream_charts", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["sync_upstream_charts"] = mod
_spec.loader.exec_module(mod)


def _release(tag, *, draft=False, prerelease=False, published="2020-01-01"):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published,
    }


class StableReleaseTest(unittest.TestCase):
    def test_skips_drafts_prerelease_flag_and_pre_suffixes(self):
        releases = [
            _release("9.9.9", draft=True),
            _release("9.9.8", prerelease=True),
            # Marked stable by GitHub, but the tag says otherwise.
            _release("0.7.4-rc10", prerelease=False),
            _release("0.3.0-beta9"),
            _release("v0.5.1-rc3"),
            _release("1.0.0-alpha.1"),
            _release("1.0.0-preview"),
            _release("1.0.0-SNAPSHOT"),
            _release("0.7.4rc1"),
            _release("nightly"),
            _release("swiss-chart-1.0"),
            _release("1.2"),
            _release("1.2.3.4"),
            # Older by date than 0.9.9 below, but a higher version.
            _release("0.10.0", published="2020-01-01"),
            _release("0.9.9", published="2026-11-01"),
            _release("v0.5.1", published="2019-01-01"),
        ]
        chosen = mod.select_stable_release(releases)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["tag_name"], "0.10.0")

    def test_leading_v_is_stable_and_orders_numerically(self):
        chosen = mod.select_stable_release(
            [
                _release("v0.9.0"),
                _release("v0.10.0"),
                _release("0.9.9"),
            ]
        )
        self.assertEqual(chosen["tag_name"], "v0.10.0")

    def test_none_stable_returns_none(self):
        releases = [
            _release("nightly"),
            _release("1.0.0-rc1", prerelease=False),
            _release("2.0.0", draft=True),
            _release("1.2.4", prerelease=True),
            _release("0.7.4rc1"),
        ]
        self.assertIsNone(mod.select_stable_release(releases))

    def test_build_metadata_is_still_stable(self):
        chosen = mod.select_stable_release([_release("1.2.3+build.5")])
        self.assertEqual(chosen["tag_name"], "1.2.3+build.5")


class VersionComparisonTest(unittest.TestCase):
    def test_v_prefix_and_quotes_normalize(self):
        self.assertTrue(mod.is_strictly_newer("v0.5.1", "0.5.0"))
        self.assertTrue(mod.is_strictly_newer("0.10.0", "0.9.9"))
        self.assertFalse(mod.is_strictly_newer("v0.5.1", "0.5.1"))
        self.assertFalse(mod.is_strictly_newer("0.5.1", '"0.5.1"'))
        self.assertFalse(mod.is_strictly_newer("'v0.5.1'", "0.5.1"))
        self.assertFalse(mod.is_strictly_newer("v0.5.0", "0.5.1"))
        # Build metadata does not make a version newer than itself.
        self.assertFalse(mod.is_strictly_newer("1.2.3+build.2", "1.2.3"))

    def test_unparseable_version_is_an_error(self):
        with self.assertRaises(mod.ChartError):
            mod.is_strictly_newer("nightly", "0.1.0")

    def test_release_sorts_after_same_number_prerelease(self):
        self.assertTrue(mod.is_strictly_newer("1.2.3", "1.2.3-rc1"))
        self.assertFalse(mod.is_strictly_newer("1.2.3-rc1", "1.2.3"))
        # Identifiers are not ordered; select_stable_release drops them.
        self.assertFalse(mod.is_strictly_newer("1.2.3-rc2", "1.2.3-beta.1"))


class TagChartVersionTest(unittest.TestCase):
    def test_v_prefix_and_quotes_match(self):
        mod.require_tag_matches_chart_version("owner/repo", "v0.5.1", "0.5.1")
        mod.require_tag_matches_chart_version("owner/repo", "0.7.3", '"0.7.3"')
        mod.require_tag_matches_chart_version("owner/repo", "'v0.7.3'", "0.7.3")

    def test_mismatch_names_repo_tag_and_version(self):
        with self.assertRaises(mod.ChartError) as caught:
            mod.require_tag_matches_chart_version("owner/repo", "v0.5.2", "0.5.1")
        self.assertEqual(
            str(caught.exception),
            "owner/repo release v0.5.2 has Chart.yaml version 0.5.1; "
            "the tag and chart version must match",
        )

    def test_unparseable_chart_version_is_a_mismatch(self):
        with self.assertRaises(mod.ChartError) as caught:
            mod.require_tag_matches_chart_version("owner/repo", "v1.2.3", "nightly")
        message = str(caught.exception)
        self.assertIn("owner/repo", message)
        self.assertIn("v1.2.3", message)
        self.assertIn("nightly", message)


class ImageRewriteTest(unittest.TestCase):
    def test_rewrites_exact_repository_only(self):
        text = "\n".join(
            [
                "repository: ghcr.io/modelsphere/swissd",
                'image: "ghcr.io/modelsphere/swissd:1.2.3"',
                "image: ghcr.io/modelsphere/swissd@sha256:abc",
                "catalog: ghcr.io/modelsphere/catalog-server",
                "other: ghcr.io/modelsphere/swissd-foo",
                "chart: oci://ghcr.io/modelsphere/charts",
                "oci-image: oci://ghcr.io/modelsphere/swissd:1.2.3",
                "sentence: uses ghcr.io/modelsphere/swissd).",
                "",
            ]
        )
        rewritten = mod.rewrite_images(
            text, [("ghcr.io/modelsphere/swissd", "4pdosc/swissd")]
        )
        self.assertIn("repository: 4pdosc/swissd\n", rewritten)
        self.assertIn('image: "4pdosc/swissd:1.2.3"', rewritten)
        self.assertIn("image: 4pdosc/swissd@sha256:abc", rewritten)
        self.assertIn("catalog: ghcr.io/modelsphere/catalog-server", rewritten)
        self.assertIn("other: ghcr.io/modelsphere/swissd-foo", rewritten)
        self.assertIn("chart: oci://ghcr.io/modelsphere/charts", rewritten)
        self.assertIn("oci-image: oci://ghcr.io/modelsphere/swissd:1.2.3", rewritten)
        self.assertIn("sentence: uses ghcr.io/modelsphere/swissd).", rewritten)
        self.assertNotIn("repository: ghcr.io/modelsphere/swissd", rewritten)


class PatchTest(unittest.TestCase):
    def test_literal_replacement(self):
        text = "image:\n  repository: ghcr.io/modelsphere/swissd\n  tag: \"\"\n"
        find = "image:\n  repository: ghcr.io/modelsphere/swissd\n"
        replace = "image:\n  repository: 4pdosc/swissd\n"
        self.assertEqual(
            mod.apply_patch(text, find, replace),
            "image:\n  repository: 4pdosc/swissd\n  tag: \"\"\n",
        )

    def test_regex_replacement_is_literal(self):
        text = 'tag: "0.5.1"\n'
        updated = mod.apply_patch(text, r'tag: "[^"]+"', 'tag: ""', regex=True)
        self.assertEqual(updated, 'tag: ""\n')

    def test_already_applied_is_ok(self):
        text = "repository: 4pdosc/swissd\n"
        updated = mod.apply_patch(
            text,
            "repository: ghcr.io/modelsphere/swissd\n",
            "repository: 4pdosc/swissd\n",
        )
        self.assertEqual(updated, text)
        updated = mod.apply_patch(text, r"repository: ghcr\.io/missing", text, regex=True)
        self.assertEqual(updated, text)

    def test_not_found_is_an_error(self):
        with self.assertRaises(mod.PatchError):
            mod.apply_patch("nothing here\n", "repository: ghcr.io/x\n", "repository: 4pdosc/x\n")
        with self.assertRaises(mod.PatchError):
            mod.apply_patch("nothing here\n", r"tag: \"[^\"]+\"", 'tag: ""', regex=True)


class ChartYamlEditTest(unittest.TestCase):
    def test_preserves_comments_quotes_and_does_not_touch_dependencies(self):
        original = (ROOT / "charts/llmscaleoperator/Chart.yaml").read_text(encoding="utf-8")
        self.assertRegex(original, r"(?m)^version: \d+\.\d+\.\d+$")
        self.assertRegex(original, r'(?m)^appVersion: "\d+\.\d+\.\d+"$')
        comment = "appVersion names an image that EXISTS"
        self.assertIn(comment, original)
        updated = mod.set_chart_versions(original, "9.8.7", "9.8.7")
        self.assertRegex(updated, r"(?m)^version: 9\.8\.7$")
        self.assertRegex(updated, r'(?m)^appVersion: "9\.8\.7"$')
        self.assertIn(comment, updated)
        self.assertIn("description: |", updated)
        self.assertIn(
            "maintained here, because this is where charts are published from.",
            updated,
        )
        self.assertEqual(updated.count("\n"), original.count("\n"))

    def test_keeps_inline_comment_and_single_quotes(self):
        text = "name: x\nversion: '0.1.0'  # pin\nappVersion: \"0.1.0\"\n"
        updated = mod.set_chart_versions(text, "0.2.0", "0.2.0")
        self.assertEqual(
            updated,
            "name: x\nversion: '0.2.0'  # pin\nappVersion: \"0.2.0\"\n",
        )

    def test_indented_dependency_version_is_not_the_chart_version(self):
        text = 'version: 0.1.0\ndependencies:\n  - version: 0.6.3\nappVersion: "0.1.0"\n'
        updated = mod.set_chart_versions(text, "0.2.0", "0.2.0")
        self.assertIn("\n  - version: 0.6.3\n", updated)
        self.assertIn("version: 0.2.0\n", updated)
        self.assertNotIn("version: 0.1.0\n", updated)


class DependencyGuardTest(unittest.TestCase):
    def test_only_the_dependency_list_counts(self):
        same = textwrap.dedent(
            """\
            name: console
            version: 0.2.1
            dependencies:
              - name: swiss
                version: 0.6.3
                repository: oci://ghcr.io/modelsphere/charts
            """
        )
        self.assertFalse(mod.dependencies_differ(same, same.replace("0.2.1", "0.2.2")))
        self.assertTrue(mod.dependencies_differ(same, same.replace("0.6.3", "0.6.4")))
        no_deps = "name: swiss\nversion: 0.7.3\n"
        self.assertFalse(mod.dependencies_differ(no_deps, no_deps))
        self.assertTrue(mod.dependencies_differ(same, no_deps))


class ExtractTest(unittest.TestCase):
    def test_extracts_only_the_chart_directory(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(name, data):
                info = tarfile.TarInfo(name=name)
                payload = data.encode()
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            add("swiss-0.7.3/README.md", "nope\n")
            add("swiss-0.7.3/helm/swiss/Chart.yaml", "version: 0.7.3\n")
            add("swiss-0.7.3/helm/swiss/values.yaml", "image: x\n")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "chart"
            mod.extract_chart(buf.getvalue(), "helm/swiss", dest)
            self.assertEqual((dest / "Chart.yaml").read_text(encoding="utf-8"), "version: 0.7.3\n")
            self.assertEqual((dest / "values.yaml").read_text(encoding="utf-8"), "image: x\n")
            self.assertFalse((dest / "README.md").exists())

    def test_rejects_path_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"pwned\n"
            info = tarfile.TarInfo(name="swiss-0.0.1/helm/swiss/../../outside.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
            good = b"version: 0.0.1\n"
            info = tarfile.TarInfo(name="swiss-0.0.1/helm/swiss/Chart.yaml")
            info.size = len(good)
            tar.addfile(info, io.BytesIO(good))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            with self.assertRaises(mod.UnsafePath):
                mod.extract_chart(buf.getvalue(), "helm/swiss", dest)
            self.assertFalse((Path(tmp) / "outside.txt").exists())
            escaped = list(Path(tmp).rglob("outside.txt"))
            self.assertEqual(escaped, [])


class SyncTargetTest(unittest.TestCase):
    def test_tag_chart_version_mismatch_needs_attention_and_leaves_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "charts" / "demo"
            dest.mkdir(parents=True)
            original = 'name: demo\nversion: 0.5.0\nappVersion: "0.5.0"\n'
            (dest / "Chart.yaml").write_text(original, encoding="utf-8")
            (dest / "values.yaml").write_text("kept: true\n", encoding="utf-8")

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                payload = b'name: demo\nversion: 0.5.1\nappVersion: "9.9.9"\n'
                info = tarfile.TarInfo(name="demo-0.5.2/charts/demo/Chart.yaml")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
                values = b"changed: true\n"
                info = tarfile.TarInfo(name="demo-0.5.2/charts/demo/values.yaml")
                info.size = len(values)
                tar.addfile(info, io.BytesIO(values))

            target = {
                "name": "demo",
                "repo": "owner/repo",
                "path": "charts/demo",
                "dest": str(dest),
            }
            with (
                patch.object(mod, "list_releases", return_value=[_release("v0.5.2")]),
                patch.object(mod, "download_tarball", return_value=buf.getvalue()),
            ):
                result = mod.sync_target(target, check=False, skip_image_check=True)

            self.assertEqual(result.result, "needs attention")
            self.assertEqual(result.repo, "owner/repo")
            self.assertEqual(result.upstream, "v0.5.2")
            self.assertEqual(
                result.detail,
                "owner/repo release v0.5.2 has Chart.yaml version 0.5.1; "
                "the tag and chart version must match",
            )
            self.assertEqual((dest / "Chart.yaml").read_text(encoding="utf-8"), original)
            self.assertEqual((dest / "values.yaml").read_text(encoding="utf-8"), "kept: true\n")


class RenderedImageTest(unittest.TestCase):
    def test_matches_the_check_script_and_splits_the_repository(self):
        render = (
            '    image: "4pdosc/swissd:0.7.3"\n'
            "      - image: ghcr.io/modelsphere/catalog-server:0.1.0\n"
            "        image: 4pdosc/llm-operator@sha256:abc\n"
        )
        self.assertEqual(
            mod.rendered_images(render),
            [
                "4pdosc/llm-operator@sha256:abc",
                "4pdosc/swissd:0.7.3",
                "ghcr.io/modelsphere/catalog-server:0.1.0",
            ],
        )
        self.assertEqual(mod.image_repository("4pdosc/swissd:0.7.3"), "4pdosc/swissd")
        self.assertEqual(
            mod.image_repository("ghcr.io/modelsphere/catalog-server:0.1.0"),
            "ghcr.io/modelsphere/catalog-server",
        )
        self.assertEqual(
            mod.image_repository("4pdosc/llm-operator@sha256:abc"),
            "4pdosc/llm-operator",
        )
        self.assertEqual(mod.image_repository("localhost:5000/foo:tag"), "localhost:5000/foo")


class LinkHeaderTest(unittest.TestCase):
    def test_next_rel(self):
        header = (
            '<https://api.github.com/repos/a/b/releases?page=2>; rel="next", '
            '<https://api.github.com/repos/a/b/releases?page=3>; rel="last"'
        )
        self.assertEqual(
            mod.next_link(header),
            "https://api.github.com/repos/a/b/releases?page=2",
        )
        self.assertIsNone(mod.next_link('<https://api.github.com/x>; rel="last"'))
        self.assertIsNone(mod.next_link(None))


class RealConfigTest(unittest.TestCase):
    def test_every_target_has_repo_path_dest_and_the_dest_exists(self):
        import yaml

        data = yaml.safe_load((ROOT / "hack/sync-upstream-charts.yaml").read_text(encoding="utf-8"))
        charts = data["charts"]
        names = [chart["name"] for chart in charts]
        for expected in ("swiss", "console", "llmscaleoperator"):
            self.assertIn(expected, names)
        for chart in charts:
            for key in ("repo", "path", "dest"):
                self.assertTrue(chart.get(key), f"{chart.get('name')} missing {key}")
            self.assertTrue((ROOT / chart["dest"]).is_dir(), chart["dest"])

    def test_configured_patches_match_the_upstream_text(self):
        import yaml

        data = yaml.safe_load((ROOT / "hack/sync-upstream-charts.yaml").read_text(encoding="utf-8"))
        by_name = {chart["name"]: chart for chart in data["charts"]}

        swiss_upstream = "image:\n  repository: ghcr.io/modelsphere/swissd\n  tag: \"\"\n"
        patch = by_name["swiss"]["patches"][0]
        patched = mod.apply_patch(swiss_upstream, patch["find"], patch["replace"], regex=patch.get("regex", False))
        self.assertIn("repository: 4pdosc/swissd", patched)
        self.assertIn("Docker Hub mirror", patched)
        rewritten = mod.rewrite_images(
            patched + "repository: ghcr.io/modelsphere/catalog-server\n",
            [("ghcr.io/modelsphere/swissd", "4pdosc/swissd")],
        )
        self.assertIn("ghcr.io/modelsphere/swissd).", rewritten)
        self.assertIn("repository: ghcr.io/modelsphere/catalog-server", rewritten)
        self.assertNotIn("repository: ghcr.io/modelsphere/swissd\n", rewritten)

        console_upstream = (
            "image:\n"
            "  # The OCI chart (oci://ghcr.io/modelsphere/charts/console) uses this GHCR image. The\n"
            "  # copy in the modelsphere helm-charts repo uses 4pdosc/console on Docker Hub instead.\n"
            "  repository: ghcr.io/modelsphere/console\n"
        )
        patch = by_name["console"]["patches"][0]
        patched = mod.apply_patch(console_upstream, patch["find"], patch["replace"])
        self.assertIn("repository: 4pdosc/console", patched)
        self.assertIn("oci://ghcr.io/modelsphere/charts/console", patched)
        rewritten = mod.rewrite_images(
            patched, [("ghcr.io/modelsphere/console", "4pdosc/console")]
        )
        self.assertIn("oci://ghcr.io/modelsphere/charts/console", rewritten)
        self.assertIn("ghcr.io/modelsphere/console.", rewritten)
        self.assertNotIn("repository: ghcr.io/modelsphere/console", rewritten)

        llm_upstream = (
            "  image:\n"
            "    repository: 4pdosc/llm-operator\n"
            "    ## Image tag (defaults to Chart.appVersion if not set)\n"
            "    ##\n"
            '    tag: "0.5.1"\n'
            "    pullPolicy: IfNotPresent\n"
        )
        patch = by_name["llmscaleoperator"]["patches"][0]
        self.assertTrue(patch.get("regex"))
        patched = mod.apply_patch(llm_upstream, patch["find"], patch["replace"], regex=True)
        self.assertIn('tag: ""', patched)
        self.assertIn("Left empty on purpose", patched)
        self.assertNotIn('tag: "0.5.1"', patched)
        self.assertIn("pullPolicy: IfNotPresent", patched)
        again = mod.apply_patch(patched, patch["find"], patch["replace"], regex=True)
        self.assertEqual(again, patched)

        llm_ghcr = llm_upstream.replace(
            "repository: 4pdosc/llm-operator",
            "repository: ghcr.io/modelsphere/llm-operator",
        )
        switched = mod.apply_patch(llm_ghcr, patch["find"], patch["replace"], regex=True)
        self.assertIn("repository: 4pdosc/llm-operator", switched)
        self.assertIn('tag: ""', switched)
        self.assertNotIn("ghcr.io/modelsphere/llm-operator", switched)


if __name__ == "__main__":
    unittest.main()
