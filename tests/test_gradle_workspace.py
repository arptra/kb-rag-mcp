import hashlib
import os
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from portal_agents import workspace


async def test_gradle_validates_task_arguments_and_checks_access(monkeypatch):
    docker = AsyncMock()
    monkeypatch.setattr(workspace, "_docker", docker)
    room = workspace.OfflineWorkspace("test", AsyncMock(return_value=False))
    run = room.gradle_tool().ainvoke
    assert "/tmp" in await run({"project_dir": "/tmp/../bad", "tasks": ["test"]})
    assert "задач" in await run({"project_dir": "/tmp/proj", "tasks": ["-p", "/etc"]})
    with pytest.raises(PermissionError):
        await run({"project_dir": "/tmp/proj", "tasks": [":service:test"]})
    docker.assert_not_awaited()


def cached_image():
    image = os.environ.get("PORTAL_AGENTS_TEST_GRADLE_IMAGE", "gradle:8.10.2-jdk17")
    if not shutil.which("docker") or subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, timeout=5,
    ).returncode:
        pytest.skip("No cached Gradle image on local Docker daemon")
    return image


async def test_groovy_wrapper_and_multimodule_build_in_offline_container():
    room = workspace.OfflineWorkspace(cached_image(), AsyncMock(return_value=True))
    shell = room.tool().ainvoke
    run = room.gradle_tool().ainvoke
    try:
        setup = await shell({"script": """
set -eu
mkdir -p /tmp/sample/app
cd /tmp/sample
echo "include 'app'" > settings.gradle
echo 'tasks.wrapper { validateDistributionUrl = false }' > build.gradle
echo 'plugins { id "java" }' > app/build.gradle
mkdir -p app/src/main/java
echo 'class Main {}' > app/src/main/java/Main.java
"""})
        assert setup.startswith("exit_code=0"), setup
        init = await run({"project_dir": "/tmp/sample", "tasks": ["wrapper", ":app:build"]})
        assert init.startswith("exit_code=0") and "BUILD SUCCESSFUL" in init, init
        # The Wrapper looks up its distribution by an MD5/base36 hash of the pinned URL.
        # Link the test image's own installed Gradle into that writable isolated cache.
        prop = await shell({"script": "cat /tmp/sample/gradle/wrapper/gradle-wrapper.properties"})
        url = next(line.partition("=")[2] for line in prop.splitlines()
                   if line.startswith("distributionUrl="))
        url = url.replace(r"\:", ":")
        dist = url.rsplit("/", 1)[-1][:-4]
        numeric = int(hashlib.md5(url.encode(), usedforsecurity=False).hexdigest(), 16)
        digits = ""
        while numeric:
            numeric, d = divmod(numeric, 36)
            digits = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + digits
        setup = await shell({"script": f"""
set -eu
cd /tmp/sample
cache=/tmp/.portal-gradle/wrapper/dists/{dist}/{digits}
mkdir -p "$cache"
ln -s "$GRADLE_HOME" "$cache/gradle-dist"
touch "$cache/{dist}.zip.ok"
chmod -x gradlew
echo 'class Main {{ invalid syntax }}' > app/src/main/java/Main.java
"""})
        assert setup.startswith("exit_code=0"), setup
        actual = await run({"project_dir": "/tmp/sample", "tasks": [":app:compileJava"]})
        assert actual.startswith("exit_code=1") and ":app:compileJava FAILED" in actual, actual
    finally:
        await room.close()


async def test_kotlin_dsl_task_in_offline_container():
    room = workspace.OfflineWorkspace(cached_image(), AsyncMock(return_value=True))
    try:
        setup = await room.tool().ainvoke({"script": """
mkdir /tmp/demo
cd /tmp/demo
echo 'rootProject.name = "demo"' > settings.gradle.kts
echo 'tasks.register("probe") { doLast { println("KOTLIN_TASK_OK") } }' > build.gradle.kts
"""})
        assert setup.startswith("exit_code=0")
        result = await room.gradle_tool().ainvoke({"project_dir": "/tmp/demo", "tasks": ["probe"]})
        assert result.startswith("exit_code=0") and "KOTLIN_TASK_OK" in result, result
    finally:
        await room.close()
