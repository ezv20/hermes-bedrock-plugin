"""Repo-root conftest.

The plugin package ``bedrock-profile-manager/`` contains an ``__init__.py`` and
is imported by the test harness via an explicit path loader (see
``tests/test_resolver.py``). Without this guard, pytest ALSO auto-imports the
package during collection, producing a SECOND ``bpm_pkg`` module object and a
second ``metadata.ContextLengthUnknown`` class — which breaks ``pytest.raises``
identity checks. Ignore the package so only the test loader's import wins.
"""

collect_ignore_glob = ["bedrock-profile-manager/*"]
